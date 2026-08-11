#!/usr/bin/env python3
"""Jarvis - a local AI assistant.

Runs entirely on your own hardware against a local Ollama server. Same source
tree on a workstation or a Raspberry Pi; it sizes itself to the machine.

    python jarvis.py                 interactive
    python jarvis.py --once "..."    one question, then exit
    python jarvis.py --verbose       show reasoning and tool calls
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import config, hardware, persona, tools  # noqa: E402
from core.brain import Brain  # noqa: E402
from core.memory import Memory  # noqa: E402
from interface.console import ConsoleInterface  # noqa: E402

HELP = """
  /help              this list
  /memory            everything Jarvis remembers about you
  /remember <text>   store a fact by hand
  /forget <id>       drop fact number <id>
  /reset             clear the current conversation (memories survive)
  /voice             show or change the speaking voice (voice mode only)
  /wake [on|off|word]  hands-free listening (voice mode only)
  /home              connect to Home Assistant, or /home forget
  /status            hardware, model, and connection
  /verbose           toggle showing reasoning and tool calls
  /exit              quit  (Ctrl-C and Ctrl-D also work)
"""


class Jarvis:
    def __init__(self, ui, brain, memory, verbose=False):
        self.ui, self.brain, self.memory = ui, brain, memory
        self.verbose = verbose
        self.messages: list[dict] = []
        self.reset(carry_over=True, announce=False)

    # -- conversation state ----------------------------------------------
    def reset(self, carry_over: bool = False, announce: bool = True) -> None:
        self.messages = [{"role": "system", "content": persona.build_system_prompt(self.memory)}]
        if carry_over:
            for msg in self.memory.carry_over(limit=6):
                if msg["role"] in ("user", "assistant"):
                    self.messages.append(msg)
        if announce:
            self.ui.status("Conversation cleared. Long-term memory untouched.")

    def _refresh_system_prompt(self) -> None:
        """Facts may have been added mid-conversation; keep the prompt current."""
        self.messages[0] = {
            "role": "system",
            "content": persona.build_system_prompt(self.memory),
        }

    # -- one turn ----------------------------------------------------------
    def turn(self, text: str) -> None:
        self._refresh_system_prompt()
        self.messages.append({"role": "user", "content": text})
        self.memory.log("user", text)

        answer = ""
        self.ui.busy("thinking")
        for kind, payload in self.brain.ask(self.messages):
            if kind == "token":
                self.ui.stream(payload)
            elif kind == "think" and self.verbose:
                self.ui.status(payload.strip()[:200], "info")
            elif kind == "tool_start":
                self.ui.busy(_working_on(payload))
            elif kind == "tool":
                # Back to waiting on the model until it speaks or calls again.
                self.ui.busy("thinking")
                if self.verbose:
                    self.ui.status(
                        f"{payload['name']}({payload['arguments']}) -> "
                        f"{payload['result'][:160]}"
                    )
                else:
                    self.ui.status(_describe(payload))
            elif kind == "error":
                self.ui.idle()
                self.ui.status(payload, "error")
                return
            elif kind == "done":
                answer = payload

        self.ui.idle()
        self.ui.end_stream()
        if answer:
            self.memory.log("assistant", answer)

        # Keep the live window bounded; the database keeps the full record.
        limit = 1 + config.HISTORY_TURNS * 2
        if len(self.messages) > limit:
            self.messages = [self.messages[0]] + self.messages[-(limit - 1):]

    # -- slash commands ----------------------------------------------------
    def command(self, line: str) -> bool:
        """Returns False if Jarvis should shut down."""
        head, _, rest = line.partition(" ")
        rest = rest.strip()

        if head in ("/exit", "/quit"):
            return False
        elif head == "/help":
            self.ui.say(HELP.strip())
        elif head == "/reset":
            self.reset()
        elif head == "/verbose":
            self.verbose = not self.verbose
            self.ui.status(f"Verbose {'on' if self.verbose else 'off'}.")
        elif head == "/memory":
            facts = self.memory.all_facts(limit=200)
            stats = self.memory.stats()
            if facts:
                body = "\n".join(f"  [{f['id']}] {f['text']}" for f in facts)
            else:
                body = "  (nothing yet)"
            self.ui.say(
                f"I remember {stats['facts']} fact(s) across "
                f"{stats['sessions']} session(s):\n{body}"
            )
        elif head == "/remember":
            self.ui.status(self.memory.remember(rest) if rest else "Remember what?")
        elif head == "/forget":
            if rest.isdigit():
                self.ui.status(self.memory.forget(int(rest)))
            else:
                self.ui.status("Usage: /forget <id>  (see /memory)", "warn")
        elif head == "/voice":
            self.voice_command(rest)
        elif head == "/wake":
            self.wake_command(rest)
        elif head == "/home":
            self.home_command(rest)
        elif head == "/status":
            online = self.brain.is_up()
            self.ui.say(
                f"Hardware : {hardware.summary()}\n"
                f"Model    : {self.brain.model}\n"
                f"Ollama   : {self.brain.host} "
                f"[{'online' if online else 'OFFLINE'}]\n"
                f"Tools    : {', '.join(tools.names())}\n"
                f"Memory   : {self.memory.stats()}"
            )
        else:
            self.ui.status(f"Unknown command {head}. Try /help.", "warn")
        return True

    # -- voice tuning ------------------------------------------------------
    def voice_command(self, rest: str) -> None:
        """`/voice` on its own reports; with arguments it switches or tunes."""
        if not hasattr(self.ui, "speaker"):
            self.ui.status("Not in voice mode. Restart and answer yes, or use --voice.")
            return

        from interface import voice as voice_mod

        available = sorted(p.stem for p in voice_mod.VOICES_DIR.glob("*.onnx")) \
            if voice_mod.VOICES_DIR.is_dir() else []

        if not rest:
            lines = [f"Speaking with : {self.ui.speaker.name}"]
            lines.append(f"Downloaded    : {', '.join(available) or 'none'}")
            lines.append("")
            lines.append("  /voice <name>        switch voice")
            lines.append("  /voice speed 1.1     faster; below 1 is slower")
            lines.append("  /voice test          say a line so you can judge it")
            lines.append("")
            lines.append("  More voices:  python deploy/get_voice.py --list")
            self.ui.say("\n".join(lines))
            return

        word, _, value = rest.partition(" ")
        word, value = word.lower(), value.strip()

        if word == "test":
            self.ui.say(value or "The quick brown fox jumps over the lazy dog, sir.")
            return

        if word in ("speed", "volume"):
            try:
                number = float(value)
            except ValueError:
                self.ui.status(f"Usage: /voice {word} 1.1", "warn")
                return
            if not self._retune(**{word: number}):
                self.ui.status("This engine has no adjustable settings.", "warn")
            return

        if word in available:
            if self._retune(model=voice_mod.VOICES_DIR / f"{word}.onnx"):
                self.ui.say(f"Switched to {word}.")
            return

        self.ui.status(
            f"Unknown voice {word!r}. Downloaded: {', '.join(available) or 'none'}. "
            "Get more with: python deploy/get_voice.py --list",
            "warn",
        )

    def home_command(self, rest: str) -> None:
        """Connect Jarvis to Home Assistant, or report and undo the connection.

        The token is read with getpass and written straight to disk. It is never
        put through the conversation, because every message the user types is
        written to the transcript database — a token typed at Jarvis would sit
        there in plaintext, and in every backup of it.
        """
        import getpass

        from core import homeassistant as ha

        word = rest.strip().lower()

        if word in ("forget", "disconnect", "remove"):
            self.ui.status("Disconnected." if ha.forget() else "Was not connected.")
            return

        if not word and ha.is_configured():
            try:
                where = ha.load()["url"]
                self.ui.say(
                    f"Connected to Home Assistant at {where}.\n"
                    f"  {ha.summarise()[:400]}\n\n"
                    "  /home again      reconnect with a new token\n"
                    "  /home forget     remove the connection"
                )
            except ha.HomeAssistantError as exc:
                self.ui.status(f"Configured, but not reachable: {exc}", "warn")
            return

        if not sys.stdin.isatty():
            self.ui.status("Run /home from a terminal — it needs a hidden prompt.", "warn")
            return

        self.ui.say(
            "Connecting to Home Assistant.\n\n"
            "  You need two things:\n"
            "   1. Its address on your network, e.g. 192.168.1.50\n"
            "   2. A long-lived access token\n\n"
            "  To get the token: open Home Assistant, click your name at the\n"
            "  bottom-left, choose the Security tab, scroll to the bottom and\n"
            "  create a long-lived access token.\n\n"
            "  The token is written straight to disk and never appears in this\n"
            "  conversation or its history. Press Enter alone to cancel."
        )

        try:
            raw = input("\n  Address: ").strip()
            if not raw:
                self.ui.status("Cancelled.")
                return
            url = ha.normalise_url(raw)

            # getpass keeps it off the screen and out of the transcript.
            token = getpass.getpass(f"  Token for {url} (hidden): ").strip()
            if not token:
                self.ui.status("Cancelled.")
                return
        except (EOFError, KeyboardInterrupt):
            print()
            self.ui.status("Cancelled.")
            return

        self.ui.status(f"Checking {url}...")
        try:
            who = ha.check(url, token)
        except ha.HomeAssistantError as exc:
            self.ui.status(str(exc), "error")
            self.ui.status("Nothing was saved. Try /home again.", "warn")
            return

        ha.save(url, token)
        self.ui.say(f"Connected to {who}.")
        try:
            self.ui.status(f"Found: {len(ha.states())} entities.")
        except ha.HomeAssistantError:
            pass
        self.ui.say(
            "You can now ask me to turn things on and off, dim lights, set the "
            "thermostat, or tell you what is on."
        )

    def wake_command(self, rest: str) -> None:
        """Turn hands-free listening on or off, or change the word it waits for."""
        if not hasattr(self.ui, "wake"):
            self.ui.status("Not in voice mode. Restart and answer yes, or use --voice.")
            return
        if not getattr(self.ui, "listener", None):
            self.ui.status(
                "No microphone, so there is nothing to wake. Install it with: "
                "pip install -r requirements-voice.txt",
                "warn",
            )
            return

        word = rest.strip().lower()

        if not word:
            if self.ui.wake:
                self.ui.say(
                    f'Hands-free is on, waiting for "{self.ui.wake}".\n'
                    "  /wake off            back to press-Enter-to-talk\n"
                    "  /wake <word>         wait for a different word"
                )
            else:
                self.ui.say(
                    "Hands-free is off; press Enter to talk.\n"
                    f"  /wake on             wait for \"{config.NAME.lower()}\"\n"
                    "  /wake <word>         wait for a different word"
                )
            return

        if word in ("off", "no", "stop", "disable"):
            self.ui.wake = ""
            self.ui.free = False
            self.ui.status("Hands-free off. Press Enter to talk.")
            return

        if word in ("free", "talk", "always"):
            self.ui.free = True
            self.ui.wake = ""
            self.ui.status("Free talk on. No wake word — just speak.")
            return

        if word in ("on", "yes", "enable"):
            self.ui.free = False
            word = config.NAME.lower()

        if len(word.split()) > 1:
            self.ui.status("Pick a single word — shorter is easier to hear.", "warn")
            return

        self.ui.wake = word
        self.ui.status(f'Hands-free on. Say "{word}" to get my attention.')

    def _retune(self, model=None, speed=None, volume=None) -> bool:
        """Rebuild the speaker with new settings, keeping the current voice."""
        from interface.voice import PiperSpeaker

        current = self.ui.speaker
        if not isinstance(current, PiperSpeaker):
            return False

        path = model or (getattr(current, "model_name", None) and
                         __import__("interface.voice", fromlist=["voice"]).VOICES_DIR
                         / f"{current.model_name}.onnx")
        if not path:
            return False

        # length_scale is the inverse of speed, so recover speed to adjust it.
        old_speed = 1.0 / (current.config.length_scale or 1.0)
        try:
            new = PiperSpeaker(
                path,
                speed=speed if speed is not None else old_speed,
                volume=volume if volume is not None else (current.config.volume or 1.0),
            )
        except Exception as exc:  # noqa: BLE001
            self.ui.status(f"Could not load that voice: {exc}", "warn")
            return False

        current.close()
        self.ui.speaker = new
        if speed is not None:
            self.ui.status(f"Speed set to {speed}.")
        if volume is not None:
            self.ui.status(f"Volume set to {volume}.")
        return True

    # -- loop --------------------------------------------------------------
    def run(self) -> None:
        while True:
            text = self.ui.listen()
            if not text:
                break
            if text.startswith("/"):
                if not self.command(text):
                    break
                continue
            self.turn(text)

        self.ui.say("Standing by.")


def _working_on(call: dict) -> str:
    """Present tense, for the marker shown while a tool is still running."""
    name, args = call["name"], call.get("arguments", {})
    if name == "web_search":
        return f"searching the web for \"{args.get('query', '')}\""
    if name == "fetch_page":
        return f"reading {args.get('url', '')[:60]}"
    if name == "remember":
        return "saving that to memory"
    if name == "recall":
        return "checking what I know"
    if name == "system_status":
        return "checking the system"
    return f"running {name}"


def _describe(call: dict) -> str:
    """A short, human line about a tool call, for non-verbose mode."""
    name, args = call["name"], call["arguments"]
    if name == "web_search":
        return f"searching the web for \"{args.get('query', '')}\""
    if name == "fetch_page":
        return f"reading {args.get('url', '')}"
    if name == "remember":
        return "committing that to memory"
    if name == "recall":
        return "checking what I know"
    return f"using {name}"


def ask_yes_no(ui, question: str, default: bool = False) -> bool:
    """A single startup question. Anything unexpected keeps the safe default."""
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"\n  {question}? {hint} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not answer:
        return default
    return answer[0] == "y"


def preflight(ui, brain) -> bool:
    """Verify Ollama is reachable and the model is present; explain if not."""
    if not brain.is_up():
        ui.status(f"No Ollama server at {brain.host}.", "error")
        ui.status("Start it with:  ollama serve", "warn")
        ui.status("Not installed?  see README.md", "warn")
        return False

    if not brain.has_model():
        installed = brain.installed_models()
        ui.status(f"Model '{brain.model}' is not downloaded.", "error")
        ui.status(f"Get it with:  ollama pull {brain.model}", "warn")
        if installed:
            ui.status(f"Already have: {', '.join(installed)}", "warn")
            ui.status(f"Or use one:   python jarvis.py --model {installed[0]}", "warn")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis, a local AI assistant.")
    parser.add_argument("--model", help=f"Ollama model tag (default: {config.MODEL})")
    parser.add_argument("--host", help=f"Ollama server (default: {config.OLLAMA_HOST})")
    parser.add_argument("--once", metavar="PROMPT", help="Ask one question and exit.")
    parser.add_argument(
        "--verbose", action="store_true", help="Show reasoning and tool calls."
    )
    parser.add_argument(
        "--voice", action="store_true", help="Speak answers aloud and listen for input."
    )
    parser.add_argument(
        "--no-mic", action="store_true",
        help="With --voice: speak answers, but keep typing the questions.",
    )
    parser.add_argument(
        "--no-voice", action="store_true",
        help="Stay in text mode without being asked.",
    )
    parser.add_argument(
        "--wake", nargs="?", const=config.NAME.lower(), default="",
        metavar="WORD",
        help=f"Hands-free: wait for a wake word (default '{config.NAME.lower()}').",
    )
    parser.add_argument(
        "--free", action="store_true",
        help="Free talk: no wake word, no key. Just speak.",
    )
    parser.add_argument("--tts-voice", default="", help="Name of the system voice to use.")
    args = parser.parse_args()

    ui = ConsoleInterface()
    console = ui
    voice = None
    brain = Brain(model=args.model, host=args.host)

    # An explicit choice always wins; otherwise prefer Jarvis's own build over
    # the stock base model it was made from.
    if not args.model and not os.environ.get("JARVIS_MODEL"):
        brain.prefer_own_build()
    memory = Memory()
    memory.start_session()
    tools.bind_memory(memory)

    if not args.once:
        ui.banner(
            [
                hardware.summary(),
                f"model {brain.model} via {brain.host}",
                "/help for commands",
            ]
        )

    if not preflight(ui, brain):
        memory.close()
        return 1

    # Voice is offered rather than assumed: asked once at startup, unless a
    # flag already settles it or there is nobody at the keyboard to answer.
    want_voice = args.voice or bool(args.wake) or args.free
    if not want_voice and not args.no_voice and not args.once and sys.stdin.isatty():
        want_voice = ask_yes_no(console, "Use voice? Jarvis will speak its answers")

    wake = args.wake
    if want_voice and not wake and not args.free and not args.no_mic \
            and sys.stdin.isatty():
        # Defaults to yes: pressing Enter before every sentence is the thing
        # that stops voice mode feeling like conversation.
        if ask_yes_no(
            console,
            f'Hands-free? Say "{config.NAME}" once, then just talk',
            default=True,
        ):
            wake = config.NAME.lower()

    if want_voice:
        from interface.voice import VoiceInterface

        console.status("Starting voice (first run loads a speech model)...")
        voice = VoiceInterface(
            console, listen=not args.no_mic, voice=args.tts_voice, wake=wake,
            free=args.free,
        )
        ui = voice
        ui.status(voice.describe())
        if voice.listen_error:
            ui.status(
                "Microphone unavailable, so questions stay typed. Install it with: "
                "pip install -r requirements-voice.txt",
                "warn",
            )

    agent = Jarvis(ui, brain, memory, verbose=args.verbose)
    try:
        if args.once:
            agent.turn(args.once)
        else:
            agent.run()
    except KeyboardInterrupt:
        ui.say("Standing by.")
    finally:
        memory.close()
        if voice:
            voice.close()  # let the last sentence finish before the process exits
    return 0


if __name__ == "__main__":
    sys.exit(main())
