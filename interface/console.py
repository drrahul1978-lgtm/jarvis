"""Terminal front-end: coloured, streaming, and quiet about it."""

import os
import sys
import threading
import time

from core import config

_ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
}


def _force_utf8() -> None:
    """Model output is full of dashes and quotes that cp1252 cannot encode.

    Without this, piping Jarvis on Windows dies with UnicodeEncodeError partway
    through an answer. Harmless everywhere else.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _supports_colour() -> bool:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        # Turn on VT processing; on Windows 10+ this is all it takes.
        try:
            import ctypes

            handle = ctypes.windll.kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:  # noqa: BLE001
            return False
    return True


class Spinner:
    """A live 'still working' marker on one rewritten line.

    Without it, the gap between asking and the first word looks identical to a
    hang. It also names what is happening, so a long pause spent fetching a web
    page does not read as the model being slow.
    """

    BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    ASCII = "|/-\\"

    def __init__(self, paint, fancy: bool = True):
        self._paint = paint                    # colouring callback
        self._frames = self.BRAILLE if fancy else self.ASCII
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._label = ""
        self._width = 0

    def start(self, label: str) -> None:
        if self._thread and self._thread.is_alive():
            self._label = label                # relabel without restarting
            return
        self._label = label
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        i = 0
        while not self._stop.wait(0.09):
            frame = self._frames[i % len(self._frames)]
            line = f"  {frame} {self._label}"
            self._width = max(self._width, len(line))
            sys.stdout.write("\r" + self._paint(line, "dim") + " " * 4)
            sys.stdout.flush()
            i += 1

    def stop(self) -> None:
        if not (self._thread and self._thread.is_alive()):
            return
        self._stop.set()
        self._thread.join(timeout=1)
        sys.stdout.write("\r" + " " * (self._width + 6) + "\r")
        sys.stdout.flush()
        self._width = 0


class ConsoleInterface:
    def __init__(self):
        _force_utf8()
        self.colour = _supports_colour()
        self._mid_stream = False
        # Animation only makes sense on a terminal; piped output would fill
        # with thousands of spinner frames.
        self._live = sys.stdout.isatty()
        self._spinner = Spinner(self._c, fancy=self.colour)

    # -- painting ---------------------------------------------------------
    def _c(self, text: str, *styles: str) -> str:
        if not self.colour:
            return text
        prefix = "".join(_ANSI[s] for s in styles if s in _ANSI)
        return f"{prefix}{text}{_ANSI['reset']}"

    # -- Interface --------------------------------------------------------
    def listen(self) -> str:
        try:
            return input(self._c("\nyou > ", "bold", "green")).strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    def busy(self, label: str) -> None:
        """Show that something is happening, and say what."""
        if self._live and not self._mid_stream:
            self._spinner.start(label)

    def idle(self) -> None:
        """Clear the marker. Safe to call when nothing is spinning."""
        self._spinner.stop()

    def stream(self, fragment: str) -> None:
        if not self._mid_stream:
            self.idle()          # first token: the waiting is over
            sys.stdout.write(self._c(f"\n{config.NAME.lower()} > ", "bold", "cyan"))
            self._mid_stream = True
        sys.stdout.write(fragment)
        sys.stdout.flush()

    def end_stream(self) -> None:
        if self._mid_stream:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._mid_stream = False

    def say(self, text: str) -> None:
        self.end_stream()
        print(self._c(f"\n{config.NAME.lower()} > ", "bold", "cyan") + text)

    def say_user(self, text: str) -> None:
        """Echo something the user said out loud, so the transcript still reads."""
        self.end_stream()
        print(self._c("you > ", "bold", "green") + text)

    def status(self, text: str, level: str = "info") -> None:
        was_streaming = self._mid_stream
        self.idle()
        self.end_stream()
        style = {"info": ("dim",), "warn": ("yellow",), "error": ("red",)}.get(
            level, ("dim",)
        )
        print(self._c(f"  - {text}", *style))
        # A status line during an answer shouldn't swallow the prefix afterwards.
        self._mid_stream = False if was_streaming else self._mid_stream

    # -- extras the console alone offers ----------------------------------
    def banner(self, lines: list[str]) -> None:
        print()
        print(self._c(f"  {config.NAME.upper()}", "bold", "cyan"))
        for line in lines:
            print(self._c(f"  {line}", "dim"))
        print()
