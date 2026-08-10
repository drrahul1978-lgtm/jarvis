"""Terminal front-end: coloured, streaming, and quiet about it."""

import os
import sys

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


class ConsoleInterface:
    def __init__(self):
        _force_utf8()
        self.colour = _supports_colour()
        self._mid_stream = False

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

    def stream(self, fragment: str) -> None:
        if not self._mid_stream:
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
