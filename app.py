#!/usr/bin/env python3
"""Jarvis as a desktop window: typing, voice and camera in one place.

Built on tkinter, which ships with Python, so the window adds no dependencies
of its own — the same reasoning as the rest of the project.

Threading rule, and the only one that matters here: the model, the microphone
and the camera all run on worker threads, and none of them touch a widget.
Everything they produce goes through a queue and is painted on the UI thread.

    python app.py
"""

import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import config, hardware, persona, tools  # noqa: E402
from core.brain import Brain  # noqa: E402
from core.memory import Memory  # noqa: E402

# A quiet, near-monochrome palette. One warm accent, used sparingly — on the
# send button, the active microphone, and nothing else.
BG = "#1a1a1a"
SURFACE = "#242424"
FIELD = "#2c2c2c"
LINE = "#343434"
INK = "#ececec"
DIM = "#8e8e8e"
FAINT = "#5e5e5e"
ACCENT = "#c8a45c"
ALERT = "#d97757"

COLUMN = 720            # readable measure; the conversation stays this wide


class JarvisApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(config.NAME)
        self.geometry("900x680")
        self.minsize(520, 400)
        self.configure(bg=BG)

        self.events: queue.Queue = queue.Queue()
        self.thinking = False
        self.streaming = False
        self._spin_job = None
        self._spin_i = 0

        # Voice, loaded lazily: importing it pulls in the speech stack, which
        # is slow and pointless for someone who only ever types.
        self.speaker = None
        self.listener = None
        self.voice_error = ""
        self.speak_replies = tk.BooleanVar(value=False)
        self.free_talk = False
        self._listening = False
        self._say_queue: queue.Queue = queue.Queue()
        self._say_buffer = ""

        self.brain = Brain()
        if not os.environ.get("JARVIS_MODEL"):
            self.brain.prefer_own_build()
        self.memory = Memory()
        self.memory.start_session()
        tools.bind_memory(self.memory)
        self.messages: list[dict] = []

        self._build()
        self._reset()
        self.after(50, self._pump)
        self.after(100, self._greet)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        self.f_body = tkfont.Font(family="Segoe UI", size=11)
        self.f_label = tkfont.Font(family="Segoe UI Semibold", size=9)
        self.f_note = tkfont.Font(family="Segoe UI", size=9)

        # -- header
        head = tk.Frame(self, bg=BG, height=44)
        head.pack(fill="x", side="top")
        head.pack_propagate(False)

        tk.Label(head, text=config.NAME, bg=BG, fg=INK,
                 font=("Segoe UI Semibold", 11)).pack(side="left", padx=(20, 10))

        self.state_dot = tk.Label(head, text="●", bg=BG, fg=FAINT,
                                  font=("Segoe UI", 8))
        self.state_dot.pack(side="left")
        self.state_text = tk.Label(head, text="starting", bg=BG, fg=DIM,
                                   font=self.f_note)
        self.state_text.pack(side="left", padx=(6, 0))

        self._flat_button(head, "Clear", self.on_clear).pack(side="right", padx=(0, 16))
        self._flat_button(head, "Memory", self.on_memory).pack(side="right", padx=6)

        tk.Frame(self, bg=LINE, height=1).pack(fill="x")

        # The composer is packed BEFORE the conversation, even though it sits
        # below it. Pack order is priority order: an expanding widget packed
        # first claims every remaining pixel and squeezes later ones off the
        # window entirely — which is exactly what it did.
        self._build_composer()

        # -- conversation
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True)

        self.view = tk.Text(
            wrap, bg=BG, fg=INK, font=self.f_body, wrap="word", relief="flat",
            padx=24, pady=24, spacing1=1, spacing2=3, spacing3=10,
            state="disabled", cursor="arrow", highlightthickness=0,
            selectbackground="#3a3a3a",
        )
        self.view.pack(fill="both", expand=True, side="left")
        self.view.bind("<Configure>", self._recentre)

        bar = tk.Scrollbar(wrap, command=self.view.yview, width=10,
                           bg=BG, troughcolor=BG, activebackground=FAINT,
                           relief="flat", borderwidth=0)
        bar.pack(side="right", fill="y")
        self.view.configure(yscrollcommand=bar.set)

        self.view.tag_configure("who", foreground=DIM, font=self.f_label,
                                spacing1=16, spacing3=4)
        self.view.tag_configure("msg", foreground=INK, spacing3=14)
        self.view.tag_configure("note", foreground=FAINT, font=self.f_note,
                                spacing1=2, spacing3=8)
        self.view.tag_configure("bad", foreground=ALERT, font=self.f_note,
                                spacing3=8)

    def _build_composer(self) -> None:
        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", side="bottom")
        tk.Frame(self, bg=LINE, height=1).pack(fill="x", side="bottom")

        centre = tk.Frame(foot, bg=BG)
        centre.pack(pady=(14, 6))
        self.centre = centre

        field = tk.Frame(centre, bg=FIELD, highlightbackground=LINE,
                         highlightthickness=1)
        field.pack()
        self.field = field

        self.entry = tk.Text(field, bg=FIELD, fg=INK, font=self.f_body,
                             relief="flat", height=1, width=52, wrap="word",
                             insertbackground=INK, padx=14, pady=11,
                             highlightthickness=0)
        self.entry.pack(side="left", fill="both", expand=True)
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Shift-Return>", lambda e: None)
        self.entry.bind("<KeyRelease>", self._grow_entry)
        self.entry.focus_set()

        self.b_mic = self._icon_button(field, "Talk", self.on_mic)
        self.b_mic.pack(side="left", padx=(0, 2), pady=6)
        self.b_cam = self._icon_button(field, "Look", self.on_look)
        self.b_cam.pack(side="left", padx=(0, 2), pady=6)

        self.b_send = tk.Label(field, text="↑", bg=ACCENT, fg=BG,
                               font=("Segoe UI", 12, "bold"), width=3,
                               cursor="hand2")
        self.b_send.pack(side="left", padx=(2, 6), pady=6)
        self.b_send.bind("<Button-1>", lambda e: self.on_send())

        row = tk.Frame(centre, bg=BG)
        row.pack(fill="x", pady=(8, 0))

        self.c_speak = tk.Checkbutton(
            row, text="Speak replies", variable=self.speak_replies,
            command=self.on_speak_toggle, bg=BG, fg=DIM, font=self.f_note,
            selectcolor=FIELD, activebackground=BG, activeforeground=INK,
            highlightthickness=0, borderwidth=0, cursor="hand2")
        self.c_speak.pack(side="left")

        self.b_free = tk.Label(row, text="Free talk: off", bg=BG, fg=DIM,
                               font=self.f_note, cursor="hand2")
        self.b_free.pack(side="left", padx=(16, 0))
        self.b_free.bind("<Button-1>", lambda e: self.on_free_toggle())

        self.hint = tk.Label(row, text="", bg=BG, fg=FAINT, font=self.f_note)
        self.hint.pack(side="right")

    def _flat_button(self, parent, text, command):
        b = tk.Label(parent, text=text, bg=BG, fg=DIM, font=self.f_note,
                     cursor="hand2", padx=8, pady=4)
        b.bind("<Button-1>", lambda e: command())
        b.bind("<Enter>", lambda e: b.configure(fg=INK))
        b.bind("<Leave>", lambda e: b.configure(fg=DIM))
        return b

    def _icon_button(self, parent, text, command):
        b = tk.Label(parent, text=text, bg=FIELD, fg=DIM, font=self.f_note,
                     cursor="hand2", padx=10, pady=6)
        b.bind("<Button-1>", lambda e: command())
        b.bind("<Enter>", lambda e: b.configure(fg=INK))
        b.bind("<Leave>", lambda e: b.configure(
            fg=ACCENT if (text == "Talk" and self._listening) else DIM))
        return b

    def _recentre(self, event=None) -> None:
        """Keep the conversation in a readable column, whatever the width."""
        width = self.view.winfo_width()
        pad = max(24, (width - COLUMN) // 2)
        self.view.configure(padx=pad)

    def _grow_entry(self, event=None) -> None:
        """Let the box grow to a few lines, like a real composer."""
        lines = int(self.entry.index("end-1c").split(".")[0])
        self.entry.configure(height=max(1, min(lines, 6)))

    # ------------------------------------------------------------- painting
    def write(self, text: str, tag: str = "msg") -> None:
        self.view.configure(state="normal")
        self.view.insert("end", text, tag)
        self.view.see("end")
        self.view.configure(state="disabled")

    def _reset(self) -> None:
        self.messages = [
            {"role": "system", "content": persona.build_system_prompt(self.memory)}
        ]

    def _greet(self) -> None:
        if not self.brain.is_up():
            self._state("Ollama not running", ALERT)
            self.write("Ollama isn't running, so I can't think yet.\n"
                       "Start it from the Start menu, or run  ollama serve\n", "bad")
            return
        if not self.brain.has_model():
            self._state("model missing", ALERT)
            self.write(f"The model {self.brain.model} isn't downloaded.\n"
                       f"Run  ollama pull {self.brain.model}\n", "bad")
            return
        self._state("ready", FAINT)
        self.hint.configure(text=f"{self.brain.model}")

    def _state(self, text: str, colour: str) -> None:
        self.state_text.configure(text=text)
        self.state_dot.configure(fg=colour)

    # -------------------------------------------------------------- sending
    def _on_return(self, event):
        if event.state & 0x0001:        # Shift held: newline, do not send
            return None
        self.on_send()
        return "break"

    def on_send(self) -> None:
        text = self.entry.get("1.0", "end").strip()
        if not text or self.thinking:
            return
        self.entry.delete("1.0", "end")
        self.entry.configure(height=1)
        self.ask(text)

    def ask(self, text: str) -> None:
        self.write("You\n", "who")
        self.write(f"{text}\n", "msg")
        self._busy(True, "thinking")

        self.messages[0] = {
            "role": "system", "content": persona.build_system_prompt(self.memory)
        }
        self.messages.append({"role": "user", "content": text})
        self.memory.log("user", text)
        threading.Thread(target=self._think, daemon=True).start()

    def _think(self) -> None:
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
            self.events.put(("finished", answer))

    def _pump(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if kind == "token":
                if not self.streaming:
                    self.write(f"{config.NAME}\n", "who")
                    self.streaming = True
                    self._busy(False)
                self.write(payload, "msg")
                self._buffer_speech(payload)
            elif kind == "tool_start":
                self.write(f"{_doing(payload)}\n", "note")
            elif kind == "error":
                self.write(f"{payload}\n", "bad")
            elif kind == "finished":
                self._busy(False)
                self.streaming = False
                self._flush_speech()
                self.write("\n")
                if self.free_talk:
                    self.after(400, self._listen_once_free)
            elif kind == "heard":
                self.entry.delete("1.0", "end")
                self.ask(payload)
            elif kind == "mic_state":
                self._paint_mic(payload)
            elif kind == "note":
                self.write(f"{payload}\n", "note")
            elif kind == "bad":
                self.write(f"{payload}\n", "bad")

        self.after(50, self._pump)

    def _busy(self, busy: bool, label: str = "") -> None:
        self.thinking = busy
        self.b_send.configure(bg=FAINT if busy else ACCENT)
        if busy:
            self._spin_i = 0
            self._spin(label)
        elif self._spin_job:
            self.after_cancel(self._spin_job)
            self._spin_job = None
            self._state("ready", FAINT)

    def _spin(self, label: str) -> None:
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._state(f"{frames[self._spin_i % len(frames)]} {label}", ACCENT)
        self._spin_i += 1
        self._spin_job = self.after(90, lambda: self._spin(label))

    # ---------------------------------------------------------------- voice
    def _ensure_voice(self, need_mic: bool) -> bool:
        """Load the speech stack on first use, not at startup."""
        from interface import voice as V

        if self.speaker is None:
            try:
                self.speaker = V.pick_speaker()
                threading.Thread(target=self._speech_worker, daemon=True).start()
            except Exception as exc:  # noqa: BLE001
                self.voice_error = str(exc)
                return False

        if need_mic and self.listener is None:
            try:
                self.listener = V.WhisperListener()
                # Warm it so the first phrase is not the slow one.
                self.listener.transcribe(
                    self.listener.np.zeros(16000, dtype="float32"))
            except V.ListenerUnavailable as exc:
                self.voice_error = (
                    f"Microphone unavailable ({exc}). Install it with:  "
                    "pip install -r requirements-voice.txt")
                return False
            except Exception as exc:  # noqa: BLE001
                self.voice_error = str(exc)
                return False
        return True

    def _speech_worker(self) -> None:
        while True:
            line = self._say_queue.get()
            if line is None:
                return
            try:
                self.speaker.speak(line)
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._say_queue.task_done()

    def _buffer_speech(self, fragment: str) -> None:
        """Speak sentence by sentence, so it starts talking before the end."""
        if not self.speak_replies.get() or self.speaker is None:
            return
        from interface.voice import _SENTENCE_END, speakable

        self._say_buffer += fragment
        while True:
            hit = _SENTENCE_END.search(self._say_buffer)
            if not hit:
                break
            sentence = self._say_buffer[:hit.start()]
            self._say_buffer = self._say_buffer[hit.end():]
            spoken = speakable(sentence)
            if spoken:
                self._say_queue.put(spoken)

    def _flush_speech(self) -> None:
        if not self.speak_replies.get() or self.speaker is None:
            self._say_buffer = ""
            return
        from interface.voice import speakable

        rest = speakable(self._say_buffer)
        self._say_buffer = ""
        if rest:
            self._say_queue.put(rest)

    def on_speak_toggle(self) -> None:
        if self.speak_replies.get():
            if not self._ensure_voice(need_mic=False):
                self.speak_replies.set(False)
                self.write(self.voice_error or "No speech engine available.", "bad")
                return
            self.write(f"Speaking replies with {self.speaker.name}.\n", "note")

    def _paint_mic(self, active: bool) -> None:
        self._listening = active
        self.b_mic.configure(fg=ACCENT if active else DIM,
                             text="Listening" if active else "Talk")

    def on_mic(self) -> None:
        """Push to talk: one utterance, then send it."""
        if self._listening or self.thinking:
            return
        if not self._ensure_voice(need_mic=True):
            self.write(self.voice_error, "bad")
            return
        threading.Thread(target=self._listen_once, daemon=True).start()

    def _listen_once(self, free: bool = False) -> None:
        self.events.put(("mic_state", True))
        try:
            self._say_queue.join()          # never record our own voice
            if free:
                self.listener.wait_for_sound()
            audio = self.listener.record()
            heard = self.listener.transcribe(audio).strip()
        except Exception as exc:  # noqa: BLE001
            self.events.put(("bad", f"Microphone failed: {exc}"))
            self.events.put(("mic_state", False))
            return
        finally:
            self.events.put(("mic_state", False))

        from interface.voice import is_noise

        if not heard or is_noise(heard):
            if free and self.free_talk:
                self.after(0, self._listen_once_free)
            return
        self.events.put(("heard", heard))

    def _listen_once_free(self) -> None:
        if not self.free_talk or self.thinking:
            return
        threading.Thread(target=self._listen_once, kwargs={"free": True},
                         daemon=True).start()

    def on_free_toggle(self) -> None:
        if not self.free_talk:
            if not self._ensure_voice(need_mic=True):
                self.write(self.voice_error, "bad")
                return
            self.free_talk = True
            self.b_free.configure(text="Free talk: on", fg=ACCENT)
            self.write("Free talk on — just speak. Click again to stop.\n", "note")
            self._listen_once_free()
        else:
            self.free_talk = False
            self.b_free.configure(text="Free talk: off", fg=DIM)
            self.write("Free talk off.\n", "note")

    # --------------------------------------------------------------- camera
    def on_look(self) -> None:
        if self.thinking:
            return
        self.ask("What can you see through the camera right now?")

    # ----------------------------------------------------------- housekeeping
    def on_memory(self) -> None:
        facts = self.memory.all_facts(limit=100)
        self.write("What I remember\n", "who")
        if not facts:
            self.write("Nothing yet.\n", "note")
        for fact in facts:
            self.write(f"{fact['text']}\n", "note")
        self.write("\n")

    def on_clear(self) -> None:
        self.view.configure(state="normal")
        self.view.delete("1.0", "end")
        self.view.configure(state="disabled")
        self._reset()
        self.write("Cleared. What I remember about you is kept.\n", "note")

    def destroy(self) -> None:
        self.free_talk = False
        try:
            self._say_queue.put(None)
            if self.speaker:
                self.speaker.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.memory.close()
        except Exception:  # noqa: BLE001
            pass
        super().destroy()


def _doing(call: dict) -> str:
    name = call.get("name", "")
    args = call.get("arguments", {})
    if name == "web_search":
        return f"Searching for \"{args.get('query', '')}\""
    if name == "fetch_page":
        return f"Reading {args.get('url', '')[:56]}"
    if name in ("look", "look_for"):
        return "Looking through the camera"
    if name == "remember":
        return "Saving that to memory"
    if name == "recall":
        return "Checking what I know"
    if name.startswith("home_"):
        return "Talking to the house"
    if name.startswith("speak") or name == "speakers":
        return "Reaching the speakers"
    return f"Using {name}"


if __name__ == "__main__":
    JarvisApp().mainloop()
