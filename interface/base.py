"""The contract every Jarvis front-end implements.

Text today, speech later. `jarvis.py` only ever touches these five methods, so
adding a voice front-end means writing one more class here — Whisper in
`listen`, Piper in `say`/`stream` — and changing a single line in the launcher.
Nothing in core/ needs to know which one is attached.
"""

from typing import Protocol


class Interface(Protocol):
    def listen(self) -> str:
        """Block until the user says something. Return "" to mean 'quit'."""

    def stream(self, fragment: str) -> None:
        """Emit part of an answer as it is produced."""

    def end_stream(self) -> None:
        """Called once the answer is complete."""

    def say(self, text: str) -> None:
        """Emit a complete message that did not come from the model."""

    def status(self, text: str, level: str = "info") -> None:
        """Out-of-band notice: tool activity, warnings, errors."""
