"""Voice front-end: speak the answers, and optionally listen for the questions.

This implements the same five methods as the console front-end (see base.py),
so nothing in core/ knows or cares which one is attached.

Two halves, deliberately independent:

* Speaking works with no installation at all. Windows has SAPI built in, macOS
  has `say`, and a Pi almost always has espeak-ng a package away. If a better
  engine (Piper) is present it is used instead.
* Listening needs real packages — you cannot capture a microphone or run
  speech recognition from the standard library. If they are missing, Jarvis
  says so once and falls back to typed input rather than refusing to start.

The important detail is that speech is buffered into sentences rather than
spoken word by word. Jarvis begins talking as soon as the first sentence is
complete, instead of waiting for the whole answer, which is the difference
between feeling responsive and feeling broken.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from core import config

# Sentence boundary: punctuation followed by whitespace, or end of buffer.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n{2,}")

# Things that are punctuation on screen but noise out loud.
_MARKDOWN = re.compile(r"(\*\*|\*|`{1,3}|_{1,2}|^#{1,6}\s*|^\s*[-*]\s+)", re.MULTILINE)
_URL = re.compile(r"https?://\S+")


def speakable(text: str) -> str:
    """Strip the markup a screen renders and a speaker should not read aloud."""
    text = _URL.sub("a link", text)
    text = _MARKDOWN.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------- speaking --


class Speaker:
    name = "none"

    def speak(self, text: str) -> None: ...
    def close(self) -> None: ...


class WindowsSpeaker(Speaker):
    """Windows SAPI, driven through one long-lived PowerShell process.

    Spawning PowerShell per sentence costs about a quarter second, which is
    audible as a stutter between sentences. Keeping one process alive and
    feeding it lines on stdin removes that.
    """

    name = "Windows SAPI"

    SCRIPT = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.Rate = {rate};"
        "try {{ $s.SelectVoice('{voice}') }} catch {{}};"
        "while (($l = [Console]::In.ReadLine()) -ne $null) "
        "{{ if ($l.Trim()) {{ $s.Speak($l) }} }}"
    )

    def __init__(self, voice: str = "", rate: int = 1):
        script = self.SCRIPT.format(rate=rate, voice=voice.replace("'", "''"))
        self.proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )

    def speak(self, text: str) -> None:
        if self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.write(text.replace("\n", " ") + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()


class PiperSpeaker(Speaker):
    """A neural voice. Better than anything the operating system ships with.

    Synthesised to an array and played through sounddevice rather than shelled
    out to an audio player, so it behaves the same on Windows and on a Pi.
    """

    name = "Piper"

    def __init__(self, model_path, speed: float = 1.0, volume: float = 1.0,
                 expressiveness: float | None = None):
        from piper import PiperVoice, SynthesisConfig
        import numpy as np
        import sounddevice as sd

        self.np = np
        self.sd = sd
        self.voice = PiperVoice.load(str(model_path))
        self.model_name = Path(model_path).stem

        # length_scale stretches each phoneme, so it is the inverse of speed:
        # 1.15 is a little slower and reads as more deliberate.
        self.config = SynthesisConfig(
            length_scale=(1.0 / speed) if speed else 1.0,
            volume=volume,
            noise_scale=expressiveness,
        )
        self.name = f"Piper ({self.model_name})"

    def speak(self, text: str) -> None:
        try:
            chunks = list(self.voice.synthesize(text, syn_config=self.config))
            if not chunks:
                return
            audio = self.np.concatenate([c.audio_int16_array for c in chunks])
            self.sd.play(audio, chunks[0].sample_rate)
            self.sd.wait()
        except Exception:  # noqa: BLE001 - a failed sentence must not end the turn
            pass

    def close(self) -> None:
        try:
            self.sd.stop()
        except Exception:  # noqa: BLE001
            pass


class CommandSpeaker(Speaker):
    """Any TTS binary that takes text on stdin or as an argument."""

    def __init__(self, name: str, argv: list[str], stdin: bool = True):
        self.name = name
        self.argv = argv
        self.stdin = stdin

    def speak(self, text: str) -> None:
        try:
            if self.stdin:
                subprocess.run(self.argv, input=text, text=True, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.run(self.argv + [text], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass


VOICES_DIR = config.DATA_DIR / "voices"


def chosen_piper_voice() -> Path | None:
    """The Piper voice to use, if one has been downloaded.

    Preference order: an explicit environment variable, then whatever
    deploy/get_voice.py last selected, then any single voice sitting in the
    voices folder.
    """
    explicit = os.environ.get("JARVIS_VOICE", "").strip()
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return candidate
        candidate = VOICES_DIR / f"{explicit}.onnx"
        if candidate.is_file():
            return candidate

    marker = config.DATA_DIR / "voice.json"
    if marker.is_file():
        try:
            name = json.loads(marker.read_text(encoding="utf-8")).get("voice", "")
            candidate = VOICES_DIR / f"{name}.onnx"
            if candidate.is_file():
                return candidate
        except (json.JSONDecodeError, OSError):
            pass

    if VOICES_DIR.is_dir():
        found = sorted(VOICES_DIR.glob("*.onnx"))
        if found:
            return found[0]
    return None


def pick_speaker(voice: str = "", rate: int = 1) -> Speaker:
    """Best available text-to-speech on this machine.

    A downloaded Piper voice wins: it sounds far better than the system engine
    and is the whole reason for get_voice.py. Everything else is a fallback so
    that voice mode still works on a machine where nothing was downloaded.
    """
    model = chosen_piper_voice()
    if model:
        try:
            return PiperSpeaker(
                model,
                speed=float(os.environ.get("JARVIS_VOICE_SPEED", "0.96")),
                volume=float(os.environ.get("JARVIS_VOICE_VOLUME", "1.0")),
            )
        except Exception:  # noqa: BLE001 - fall through to the system engine
            pass

    if os.name == "nt":
        try:
            return WindowsSpeaker(voice=voice, rate=rate)
        except Exception:  # noqa: BLE001
            pass
    if sys.platform == "darwin" and shutil.which("say"):
        return CommandSpeaker("macOS say", ["say"], stdin=False)
    if shutil.which("espeak-ng"):
        return CommandSpeaker("espeak-ng", ["espeak-ng", "-s", "160"])
    if shutil.which("espeak"):
        return CommandSpeaker("espeak", ["espeak", "-s", "160"])
    return Speaker()


# --------------------------------------------------------------- listening --


class ListenerUnavailable(Exception):
    """Microphone or speech recognition is not installed."""


class WhisperListener:
    """Record from the microphone until you stop talking, then transcribe."""

    name = "faster-whisper"

    def __init__(self, model_size: str = "", device_index=None):
        try:
            import numpy  # noqa: F401
            import sounddevice  # noqa: F401
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ListenerUnavailable(str(exc)) from exc

        import sounddevice as sd

        self.sd = sd
        self.np = __import__("numpy")
        self.device = device_index
        self.rate = 16000

        # tiny.en is the only sensible default on a Pi; a desktop can afford more.
        size = model_size or os.environ.get(
            "JARVIS_WHISPER_MODEL",
            "tiny.en" if _is_small_machine() else "base.en",
        )
        self.model = WhisperModel(size, device="cpu", compute_type="int8")
        self.size = size

    # How long a pause means "they have finished talking". This is dead air on
    # every single turn, and transcription itself takes about a fifth of a
    # second, so it dominates the wait. Long enough not to cut people off
    # mid-sentence, short enough not to feel broken.
    SILENCE = float(os.environ.get("JARVIS_SILENCE", "0.6"))

    def record(self, max_seconds: float = 20.0, silence_seconds: float = 0.0) -> "any":
        """Capture until the speaker has been quiet for `silence_seconds`."""
        silence_seconds = silence_seconds or self.SILENCE
        np, sd = self.np, self.sd
        block = int(self.rate * 0.1)
        frames, quiet, started = [], 0.0, False
        threshold = float(os.environ.get("JARVIS_MIC_THRESHOLD", "0.012"))

        with sd.InputStream(samplerate=self.rate, channels=1, dtype="float32",
                            blocksize=block, device=self.device) as stream:
            for _ in range(int(max_seconds / 0.1)):
                chunk, _overflow = stream.read(block)
                frames.append(chunk.copy())
                level = float(np.sqrt(np.mean(chunk ** 2)))

                if level > threshold:
                    started, quiet = True, 0.0
                elif started:
                    quiet += 0.1
                    if quiet >= silence_seconds:
                        break

        return np.concatenate(frames, axis=0).flatten() if frames else None

    def transcribe(self, audio) -> str:
        if audio is None or len(audio) == 0:
            return ""
        segments, _info = self.model.transcribe(
            audio, language="en", vad_filter=True, beam_size=1
        )
        return " ".join(s.text.strip() for s in segments).strip()

    def wait_for_sound(self, poll: float = 0.05, timeout: float = 0.0) -> bool:
        """Block until someone starts talking. False if `timeout` passed first.

        Transcribing continuously would peg a CPU core for nothing — on a Pi it
        would cook the board. Instead this watches the microphone's energy
        level, which is nearly free, and only wakes the recogniser when there
        is actually something to hear.
        """
        np, sd = self.np, self.sd
        block = int(self.rate * poll)
        threshold = float(os.environ.get("JARVIS_MIC_THRESHOLD", "0.012"))
        deadline = (time.monotonic() + timeout) if timeout else None

        with sd.InputStream(samplerate=self.rate, channels=1, dtype="float32",
                            blocksize=block, device=self.device) as stream:
            while True:
                chunk, _overflow = stream.read(block)
                if float(np.sqrt(np.mean(chunk ** 2))) > threshold:
                    return True
                if deadline and time.monotonic() > deadline:
                    return False

    def chirp(self, rising: bool = True) -> None:
        """A short tone, so you know it heard you without it saying anything."""
        np, sd = self.np, self.sd
        rate = 22050
        tones = (660, 990) if rising else (990, 660)
        parts = []
        for freq in tones:
            t = np.linspace(0, 0.07, int(rate * 0.07), endpoint=False)
            wave = 0.18 * np.sin(2 * np.pi * freq * t)
            wave *= np.minimum(1.0, np.linspace(0, 12, wave.size))       # fade in
            wave *= np.minimum(1.0, np.linspace(12, 0, wave.size))       # fade out
            parts.append(wave.astype(np.float32))
        try:
            sd.play(np.concatenate(parts), rate)
            sd.wait()
        except Exception:  # noqa: BLE001
            pass


# Whisper mishears a one-word name constantly, especially at the start of a
# clip. These are real transcriptions observed for "Jarvis", plus the polite
# prefixes people put in front of it.
_WAKE_PREFIXES = ("hey", "ok", "okay", "hi", "yo", "hello")
_WORD = re.compile(r"[a-z']+")


def detect_wake(text: str, word: str = "jarvis", threshold: float = 0.72):
    """Did they say the wake word, and what did they say after it?

    Returns (heard, remainder). The remainder matters: "Jarvis, what time is it"
    should ask the question straight away rather than making them say it twice.
    """
    import difflib

    tokens = _WORD.findall(text.lower())
    if not tokens:
        return False, ""

    target = word.lower()
    for i, token in enumerate(tokens):
        if token in _WAKE_PREFIXES:
            continue
        # Two ways to match, because one is not enough on its own.
        #
        # Similarity catches javis, jervis and charvis. It cannot be loosened
        # far enough to catch "jarvace" without also catching "harvest", which
        # scores identically — and a false wake is worse than a missed one.
        # So a shared opening also counts: words starting "jarv" are the name,
        # while "harvest" is not.
        opening = target[:4]
        if (
            difflib.SequenceMatcher(None, token, target).ratio() >= threshold
            or (len(opening) >= 4 and token.startswith(opening))
        ):
            remainder = " ".join(tokens[i + 1:]).strip()
            return True, remainder
        # Only the first couple of words can be the wake word; beyond that it
        # is speech that merely happens to mention the name.
        if i >= 2:
            break
    return False, ""


def _is_small_machine() -> bool:
    from core import hardware

    return hardware.is_raspberry_pi() or hardware.total_ram_gb() < 6


# ------------------------------------------------------------- the front-end --


class VoiceInterface:
    """Speaks answers aloud; listens if it can, reads typed input if it cannot."""

    def __init__(self, ui, speak_status: bool = False, listen: bool = True,
                 voice: str = "", rate: int = 1, wake: str = ""):
        self.ui = ui                      # console, for the visible transcript
        self.speaker = pick_speaker(voice=voice, rate=rate)
        self.speak_status = speak_status
        self.listener = None
        self.listen_error = ""
        self.wake = wake.strip().lower()

        if listen:
            try:
                self.listener = WhisperListener()
            except ListenerUnavailable as exc:
                self.listen_error = str(exc)

        self._buffer = ""
        self._queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._drain, daemon=True)
        self._worker.start()

        # After answering, keep listening this long for a follow-up before
        # requiring the wake word again — so a conversation stays a
        # conversation instead of a series of announcements.
        self.follow_up = float(os.environ.get("JARVIS_FOLLOW_UP", "7.0"))
        self._just_answered = False

        # The recogniser is slow on its very first phrase and instant after.
        # Paying that once at startup keeps it out of the first real question.
        if self.listener:
            try:
                self.listener.transcribe(self.listener.np.zeros(16000, dtype="float32"))
            except Exception:  # noqa: BLE001
                pass

    # -- speech queue: keeps sentences in order without blocking the model --
    def _drain(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                self._queue.task_done()
                return
            try:
                self.speaker.speak(text)
            finally:
                # Marked done either way, so wait_until_quiet can never hang on
                # a sentence that failed to synthesise.
                self._queue.task_done()

    def wait_until_quiet(self) -> None:
        """Block until everything queued has actually been spoken.

        Essential before opening the microphone again: without it Jarvis
        records its own voice and answers itself.
        """
        self._queue.join()

    def _enqueue(self, text: str) -> None:
        text = speakable(text)
        if text:
            self._queue.put(text)

    # -- Interface ---------------------------------------------------------
    def listen(self) -> str:
        if not self.listener:
            return self.ui.listen()
        if self.wake:
            return self.listen_for_wake()

        self.ui.status("Press Enter to speak, or type instead.")
        typed = self.ui.listen()
        if typed:
            return typed

        self.ui.status("Listening...")
        try:
            audio = self.listener.record()
            text = self.listener.transcribe(audio)
        except Exception as exc:  # noqa: BLE001
            self.ui.status(f"Microphone failed: {exc}", "warn")
            return self.ui.listen()

        if not text:
            self.ui.status("Didn't catch that.", "warn")
            return self.listen()

        self.ui.say_user(text)
        return text

    def listen_for_wake(self) -> str:
        """Hands-free: sit quietly until the wake word, then take the question.

        Ctrl-C leaves the loop, which is the only way out — there is no keyboard
        prompt to fall back to while the microphone has the floor.
        """
        # Straight after an answer, take a follow-up without the wake word.
        # This is what makes it feel like talking rather than dispatching.
        if self._just_answered and self.follow_up > 0:
            self._just_answered = False
            self.wait_until_quiet()      # never record our own voice
            question = self._catch_reply(self.follow_up)
            if question:
                return question
            self.ui.status(f'Say "{self.wake}" when you need me.')

        self.ui.status(f'Waiting for "{self.wake}". Ctrl-C to stop.')

        while True:
            try:
                self.listener.wait_for_sound()
                # A wake word is one word, so it needs less trailing silence
                # than a full question does.
                audio = self.listener.record(max_seconds=8.0, silence_seconds=0.45)
                heard = self.listener.transcribe(audio)
            except KeyboardInterrupt:
                return ""
            except Exception as exc:  # noqa: BLE001
                self.ui.status(f"Microphone failed: {exc}", "warn")
                return ""

            if not heard:
                continue

            woken, remainder = detect_wake(heard, self.wake)
            if not woken:
                continue

            # "Jarvis, what time is it" - the question came with the wake word,
            # so answer it rather than asking them to say it again.
            if remainder:
                self.listener.chirp()
                self.ui.say_user(remainder)
                return remainder

            # Just the name. Acknowledge, then listen for the actual question.
            self.listener.chirp()
            try:
                audio = self.listener.record()
                question = self.listener.transcribe(audio)
            except Exception as exc:  # noqa: BLE001
                self.ui.status(f"Microphone failed: {exc}", "warn")
                continue

            if not question:
                self.listener.chirp(rising=False)
                self.ui.status("Didn't catch that.", "warn")
                continue

            self.ui.say_user(question)
            return question

    def _catch_reply(self, window: float) -> str:
        """Listen for a short while without needing the wake word.

        Returns "" if the window passes quietly, which sends us back to sleep.
        """
        self.ui.status("Listening...")
        try:
            if not self.listener.wait_for_sound(timeout=window):
                return ""
            audio = self.listener.record()
            return self.listener.transcribe(audio).strip()
        except KeyboardInterrupt:
            return ""
        except Exception as exc:  # noqa: BLE001
            self.ui.status(f"Microphone failed: {exc}", "warn")
            return ""

    def stream(self, fragment: str) -> None:
        """Show every token, but speak only whole sentences."""
        self.ui.stream(fragment)
        self._buffer += fragment

        while True:
            match = _SENTENCE_END.search(self._buffer)
            if not match:
                break
            sentence, self._buffer = (
                self._buffer[: match.start()],
                self._buffer[match.end():],
            )
            self._enqueue(sentence)

    def end_stream(self) -> None:
        if self._buffer.strip():
            self._enqueue(self._buffer)
            self._buffer = ""
        self.ui.end_stream()
        # An answer just finished, so the next listen should stay open for a
        # follow-up rather than demanding the wake word again.
        self._just_answered = True

    def say(self, text: str) -> None:
        self.ui.say(text)
        self._enqueue(text)

    def status(self, text: str, level: str = "info") -> None:
        self.ui.status(text, level)
        if self.speak_status and level in ("warn", "error"):
            self._enqueue(text)

    def __getattr__(self, name: str):
        """Forward anything this layer does not override to the console beneath.

        The voice front-end decorates the console rather than replacing it, so
        console-only extras (banner, say_user) must still reach it. Without
        this, adding a method to ConsoleInterface silently breaks voice mode.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            ui = self.__dict__["ui"]
        except KeyError:  # accessed before __init__ finished
            raise AttributeError(name) from None
        return getattr(ui, name)

    # -- extras ------------------------------------------------------------
    def describe(self) -> str:
        if not self.listener:
            listening = "typed input"
        elif self.wake:
            listening = f'hands-free, wake word "{self.wake}" ({self.listener.size})'
        else:
            listening = f"microphone via {self.listener.name} ({self.listener.size})"
        return f"voice: {self.speaker.name} out, {listening} in"

    def close(self) -> None:
        self._queue.put(None)
        self._worker.join(timeout=10)
        self.speaker.close()
