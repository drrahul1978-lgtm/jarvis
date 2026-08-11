"""Speak through Google Home, Nest and Chromecast speakers on the network.

This is local control over the Cast protocol. There is no Google account, no
cloud, and nothing leaves the house — the same mechanism your phone uses to
cast to a speaker.

What it cannot do, so it is not promised anywhere: make "Hey Google" talk to
Jarvis. Google does not allow third parties to replace the assistant on their
devices, and no amount of code here changes that. This is Jarvis talking *to*
the speakers, not answering *through* them.

Casting works by handing the device a URL to fetch, so speaking a sentence
means synthesising it, serving it briefly on the local network, and pointing
the speaker at it. The little server below exists only for that, holds audio in
memory, and serves nothing it was not explicitly given.
"""

import difflib
import io
import socket
import threading
import time
import unicodedata
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_devices: list = []
_browser = None
_discovered_at = 0.0
_lock = threading.Lock()

DISCOVERY_TTL = 300          # rescan at most every five minutes
_clips: dict[str, bytes] = {}
_server = None
_server_port = 0


class CastUnavailable(Exception):
    """PyChromecast is not installed."""


class CastError(Exception):
    """Something went wrong talking to a device."""


def _require():
    try:
        import pychromecast  # noqa: F401
    except ImportError as exc:
        raise CastUnavailable(
            "Casting needs PyChromecast. Install it with:  pip install PyChromecast"
        ) from exc
    return __import__("pychromecast")


# ---------------------------------------------------------------- discovery --


def discover(force: bool = False, timeout: int = 6) -> list:
    """Find Cast devices, remembering the result for a few minutes.

    Discovery takes several seconds, which is far too slow to repeat on every
    request when someone is talking to us.
    """
    global _devices, _browser, _discovered_at

    pychromecast = _require()
    with _lock:
        fresh = (time.time() - _discovered_at) < DISCOVERY_TTL
        if _devices and fresh and not force:
            return _devices

        if _browser is not None:
            try:
                _browser.stop_discovery()
            except Exception:  # noqa: BLE001
                pass

        found, browser = pychromecast.get_chromecasts(timeout=timeout)
        _devices, _browser, _discovered_at = list(found), browser, time.time()
        return _devices


def _norm(text: str) -> str:
    """Fold the curly apostrophes and accents that device names collect."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.replace("‘", "'").replace("’", "'").strip().lower()


def find(name: str):
    """Resolve 'kitchen' or 'family room' to an actual device."""
    devices = discover()
    if not devices:
        raise CastError("No Cast devices found on this network.")

    wanted = _norm(name)
    if not wanted:
        raise CastError("Which speaker?")

    names = {_norm(d.cast_info.friendly_name): d for d in devices}

    if wanted in names:
        return names[wanted]

    contains = [d for key, d in names.items() if wanted in key]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        listed = ", ".join(d.cast_info.friendly_name for d in contains[:6])
        raise CastError(f"{name!r} matches several speakers: {listed}. Which one?")

    close = difflib.get_close_matches(wanted, list(names), n=1, cutoff=0.55)
    if close:
        return names[close[0]]

    listed = ", ".join(d.cast_info.friendly_name for d in devices[:8])
    raise CastError(f"No speaker called {name!r}. Available: {listed}")


def listing() -> str:
    devices = discover()
    if not devices:
        return (
            "No Cast devices found. Google Home, Nest and Chromecast speakers "
            "appear here when they are on the same network."
        )
    lines = []
    for device in devices:
        info = device.cast_info
        kind = "group" if info.cast_type == "group" else info.model_name
        lines.append(f"  - {info.friendly_name} ({kind})")
    return "Speakers on the network:\n" + "\n".join(sorted(lines))


# ------------------------------------------------------------- audio server --


class _ClipHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        clip_id = self.path.lstrip("/").split("?")[0]
        audio = _clips.get(clip_id)
        if audio is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        try:
            self.wfile.write(audio)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _lan_ip() -> str:
    """This machine's address as the speakers will see it."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))     # no packets sent; just picks a route
        return probe.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        probe.close()


def _ensure_server() -> int:
    global _server, _server_port
    if _server is not None:
        return _server_port
    _server = ThreadingHTTPServer(("0.0.0.0", 0), _ClipHandler)
    _server_port = _server.server_address[1]
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    return _server_port


def _wav_bytes(text: str) -> tuple[bytes, float]:
    """Synthesise `text` to WAV. Returns (bytes, seconds)."""
    from interface.voice import chosen_piper_voice

    model = chosen_piper_voice()
    if not model:
        raise CastError(
            "No Piper voice installed, so there is nothing to send. "
            "Get one with:  python deploy/get_voice.py"
        )

    from piper import PiperVoice

    voice = PiperVoice.load(str(model))
    chunks = list(voice.synthesize(text))
    if not chunks:
        raise CastError("Nothing to say.")

    import numpy as np

    audio = np.concatenate([c.audio_int16_array for c in chunks])
    rate = chunks[0].sample_rate

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(audio.tobytes())
    return buffer.getvalue(), len(audio) / rate


def say(name: str, text: str, volume: float | None = None) -> str:
    """Speak a line through a named speaker, in Jarvis's own voice."""
    text = " ".join(text.split())
    if not text:
        return "Nothing to say."

    device = find(name)
    audio, seconds = _wav_bytes(text)

    clip_id = f"{uuid.uuid4().hex}.wav"
    _clips[clip_id] = audio
    port = _ensure_server()
    url = f"http://{_lan_ip()}:{port}/{clip_id}"

    try:
        device.wait(timeout=15)
        if volume is not None:
            device.set_volume(max(0.0, min(float(volume), 1.0)))
        device.media_controller.play_media(url, "audio/wav")
        device.media_controller.block_until_active(timeout=15)

        # Give it the length of the clip plus a little slack to fetch and play.
        deadline = time.time() + seconds + 12
        while time.time() < deadline:
            if device.media_controller.status.player_state in ("IDLE", "UNKNOWN"):
                if time.time() > deadline - seconds:
                    break
            time.sleep(0.3)
    except Exception as exc:  # noqa: BLE001
        raise CastError(f"Could not play on {device.cast_info.friendly_name}: {exc}") from exc
    finally:
        # Keep it around briefly in case the device re-requests, then drop it.
        threading.Timer(30.0, lambda: _clips.pop(clip_id, None)).start()

    return f"Said it on {device.cast_info.friendly_name}."


def set_volume(name: str, percent: int) -> str:
    device = find(name)
    level = max(0, min(int(percent), 100))
    device.wait(timeout=15)
    device.set_volume(level / 100.0)
    return f"{device.cast_info.friendly_name} volume set to {level}%."


def stop(name: str) -> str:
    device = find(name)
    device.wait(timeout=15)
    device.media_controller.stop()
    return f"Stopped {device.cast_info.friendly_name}."
