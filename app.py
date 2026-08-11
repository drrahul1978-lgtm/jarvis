#!/usr/bin/env python3
"""Jarvis as a desktop window.

Built on tkinter, which ships with Python, so the app itself adds no
dependencies — the same reasoning as the rest of the project.

    python app.py

The model runs on a worker thread and hands results back through a queue, so
the window keeps repainting while it thinks. Touching widgets from that thread
would eventually corrupt the display, so nothing does: every update is
marshalled onto the UI thread.
"""

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import config, hardware, persona, tools  # noqa: E402
from core.brain import Brain  # noqa: E402
from core.memory import Memory  # noqa: E402

# Dark, low-contrast enough to sit in front of for a while.
BG = "#161819"
PANEL = "#1e2123"
INK = "#e6e4df"
DIM = "#8b8f92"
ACCENT = "#d3a04a"
USER = "#9ecf7d"
ERROR = "#e0765c"


class JarvisApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{config.NAME}")
        self.geometry("860x640")
        self.minsize(560, 420)
        self.configure(bg=BG)

        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self._spinner_job = None
        self._spinner_step = 0

        self.brain = Brain()
        if not __import__("os").environ.get("JARVIS_MODEL"):
            self.brain.prefer_own_build()
        self.memory = Memory()
        self.memory.start_session()
        tools.bind_memory(self.memory)
        self.messages: list[dict] = []

        self._build()
        self._reset_conversation()
        self.after(60, self._pump)
        self._check_connection()

    # -- layout ------------------------------------------------------------
    def _build(self) -> None:
        body = tkfont.Font(family="Segoe UI", size=11)
        mono = tkfont.Font(family="Consolas", size=10)

        bar = tk.Frame(self, bg=PANEL)
        bar.pack(fill="x", side="top")

        tk.Label(bar, text=config.NAME.upper(), bg=PANEL, fg=ACCENT,
                 font=("Consolas", 12, "bold"), padx=12, pady=8).pack(side="left")

        self.status = tk.Label(bar, text="starting...", bg=PANEL, fg=DIM,
                               font=("Segoe UI", 9), padx=8)
        self.status.pack(side="left")

        for label, command in (("Look", self.on_look),
                               ("Memory", self.on_memory),
                               ("Clear", self.on_clear)):
            tk.Button(bar, text=label, command=command, bg=PANEL, fg=INK,
                      activebackground=ACCENT, activeforeground=BG,
                      relief="flat", font=("Segoe UI", 9), padx=10,
                      cursor="hand2").pack(side="right", padx=(0, 8), pady=6)

        self.transcript = tk.Text(
            self, bg=BG, fg=INK, font=body, wrap="word", relief="flat",
            padx=18, pady=14, insertbackground=INK, spacing1=2, spacing3=6,
            state="disabled",
        )
        self.transcript.pack(fill="both", expand=True, side="top")

        self.transcript.tag_configure("you", foreground=USER,
                                      font=("Segoe UI", 11, "bold"), spacing1=10)
        self.transcript.tag_configure("jarvis", foreground=INK)
        self.transcript.tag_configure("name", foreground=ACCENT,
                                      font=("Consolas", 10, "bold"), spacing1=10)
        self.transcript.tag_configure("note", foreground=DIM, font=mono)
        self.transcript.tag_configure("bad", foreground=ERROR, font=mono)

        scroll = ttk.Scrollbar(self.transcript, command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

        entry_row = tk.Frame(self, bg=PANEL)
        entry_row.pack(fill="x", side="bottom")

        self.entry = tk.Entry(entry_row, bg=PANEL, fg=INK, font=body,
                              relief="flat", insertbackground=ACCENT)
        self.entry.pack(side="left", fill="x", expand=True, padx=(16, 8), pady=12)
        self.entry.bind("<Return>", lambda _event: self.on_send())
        self.entry.focus_set()

        self.send = tk.Button(entry_row, text="Send", command=self.on_send,
                              bg=ACCENT, fg=BG, relief="flat",
                              font=("Segoe UI", 10, "bold"), padx=18,
                              cursor="hand2", activebackground=INK)
        self.send.pack(side="right", padx=(0, 16), pady=10)

    # -- transcript --------------------------------------------------------
    def write(self, text: str, tag: str = "jarvis") -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text, tag)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _reset_conversation(self) -> None:
        self.messages = [
            {"role": "system", "content": persona.build_system_prompt(self.memory)}
        ]

    def _check_connection(self) -> None:
        if not self.brain.is_up():
            self.status.configure(text="Ollama not running", fg=ERROR)
            self.write(
                "Ollama is not running, so I cannot think.\n"
                "Start it from the Start menu, or run:  ollama serve\n\n", "bad")
            return
        if not self.brain.has_model():
            self.status.configure(text=f"model {self.brain.model} missing", fg=ERROR)
            self.write(f"The model {self.brain.model} is not downloaded.\n"
                       f"Run:  ollama pull {self.brain.model}\n\n", "bad")
            return
        self.status.configure(
            text=f"{self.brain.model} · {hardware.summary()}", fg=DIM)
        self.write("Ready.\n", "note")

    # -- actions -----------------------------------------------------------
    def on_send(self) -> None:
        text = self.entry.get().strip()
        if not text or self.busy:
            return
        self.entry.delete(0, "end")
        self.ask(text)

    def ask(self, text: str) -> None:
        self.write(f"\nyou\n", "you")
        self.write(f"{text}\n", "jarvis")
        self.set_busy(True, "thinking")

        self.messages[0] = {
            "role": "system", "content": persona.build_system_prompt(self.memory)
        }
        self.messages.append({"role": "user", "content": text})
        self.memory.log("user", text)

        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        """Runs off the UI thread. Communicates only through the queue."""
        answer = ""
        try:
            for kind, payload in self.brain.ask(self.messages):
                self.events.put((kind, payload))
                if kind == "done":
                    answer = payload
        except Exception as exc:  # noqa: BLE001
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            if answer:
                self.memory.log("assistant", answer)
            self.events.put(("finished", None))

    def _pump(self) -> None:
        """Drain worker events on the UI thread, where painting is legal."""
        started = False
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if kind == "token":
                if not started:
                    self.write(f"\n{config.NAME.lower()}\n", "name")
                    started = True
                self.write(payload, "jarvis")
                self.set_busy(False)
            elif kind == "tool_start":
                self.write(f"  · {_doing(payload)}\n", "note")
            elif kind == "error":
                self.write(f"\n{payload}\n", "bad")
            elif kind == "finished":
                self.set_busy(False)
                self.write("\n")

        self.after(60, self._pump)

    def set_busy(self, busy: bool, label: str = "") -> None:
        self.busy = busy
        self.send.configure(state="disabled" if busy else "normal")
        if busy:
            self._spinner_step = 0
            self._tick(label)
        elif self._spinner_job:
            self.after_cancel(self._spinner_job)
            self._spinner_job = None
            self.status.configure(
                text=f"{self.brain.model} · {hardware.summary()}", fg=DIM)

    def _tick(self, label: str) -> None:
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        frame = frames[self._spinner_step % len(frames)]
        self._spinner_step += 1
        self.status.configure(text=f"{frame} {label}", fg=ACCENT)
        self._spinner_job = self.after(90, lambda: self._tick(label))

    def on_look(self) -> None:
        if self.busy:
            return
        self.ask("What can you see through the camera right now?")

    def on_memory(self) -> None:
        facts = self.memory.all_facts(limit=100)
        self.write("\nWhat I remember\n", "name")
        if not facts:
            self.write("  nothing yet\n", "note")
        for fact in facts:
            self.write(f"  [{fact['id']}] {fact['text']}\n", "note")
        self.write("\n")

    def on_clear(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        self._reset_conversation()
        self.write("Conversation cleared. Memories kept.\n", "note")

    def destroy(self) -> None:
        try:
            self.memory.close()
        except Exception:  # noqa: BLE001
            pass
        super().destroy()


def _doing(call: dict) -> str:
    name = call.get("name", "")
    args = call.get("arguments", {})
    if name == "web_search":
        return f"searching for \"{args.get('query', '')}\""
    if name == "fetch_page":
        return f"reading {args.get('url', '')[:50]}"
    if name in ("look", "look_for"):
        return "looking through the camera"
    if name == "remember":
        return "saving that to memory"
    return f"using {name}"


if __name__ == "__main__":
    JarvisApp().mainloop()
