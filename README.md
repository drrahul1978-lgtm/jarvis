# Jarvis

A local AI assistant. The model runs on your own hardware via [Ollama] — no API
keys, no accounts, no conversation leaving the machine except when you ask it to
search the web.

One source tree runs on a workstation and on a Raspberry Pi. It measures the
board at startup and picks a model that fits.

- **Persistent memory** — remembers facts about you across sessions, in SQLite.
- **Web search** — searches and reads pages when it needs current information.
- **Zero dependencies** — [Python] 3.10+ standard library only. No pip install.
- **Two front-ends** — a terminal REPL, and a small web server for headless use.

**Requirements:** [Ollama] and [Python] 3.10 or newer, both free. Python already
ships with Raspberry Pi OS and macOS — check with `python3 --version` first. On
Windows, tick *Add python.exe to PATH* during installation.

---

## Raspberry Pi

Needs **64-bit** Raspberry Pi OS. A Pi 5 with 8GB is the comfortable minimum;
4GB works with a smaller model.

```bash
git clone https://github.com/drrahul1978-lgtm/jarvis.git && cd jarvis
chmod +x deploy/install_pi.sh
./deploy/install_pi.sh
```

The script checks the board, raises swap if it is too small to load a model,
installs Ollama, pulls a right-sized model, runs a smoke test, and registers
Jarvis as a systemd service so the Pi comes up talking without anyone logging
in.

For an appliance — a Pi you flash, power on, and never attach a keyboard to:

```bash
./deploy/install_pi.sh --headless
```

That takes the default answer to every question and never waits for input, so
it is safe from a first-boot hook where stdin is closed.

| Flag           | Effect                                                  |
| -------------- | ------------------------------------------------------- |
| `--headless`   | no prompts, accept every default, autostart on           |
| `--no-service` | do not autostart; run it by hand only                    |
| `--local-only` | bind the web front-end to localhost instead of the LAN   |
| `--no-token`   | skip the shared secret (trusted networks only)           |
| `--model TAG`  | override the auto-sized model                            |
| `--port N`     | web front-end port, default 8765                         |

Either way you can still talk to it directly over SSH:

```bash
python3 jarvis.py
```

### On a Raspberry Pi 4

A Pi 4 is about three times slower than a Pi 5, so it gets its own installer
that trades capability for responsiveness:

```bash
chmod +x deploy/install_pi4.sh
./deploy/install_pi4.sh
```

It differs from the general installer in six ways, each of which matters more
than it sounds:

| Choice | Why |
| --- | --- |
| Hidden reasoning **off** | The largest win by far — see below |
| `qwen3:1.7b`, or `0.6b` under 2.5 GB | Largest that still calls tools at a usable pace |
| 2048-token context | Attention cost grows with the window |
| Model pinned in RAM | Re-reading it from an SD card costs more than the RAM |
| zram instead of swap | No card wear, and far faster than swapping to one |
| `tiny.en` and the low-quality voice | Medium voices are too slow to synthesise here |

It also checks for throttling and measures the SD card, because heat, power and
a slow card cause more "this is broken" than the software ever does.

**On hidden reasoning.** Models like `qwen3` generate a long private train of
thought before answering. Asked `17 × 23`, the same model on the same machine
took **27.3 seconds** with it on and **0.3 seconds** with it off, and gave the
same correct answer. On a Pi 4 that difference decides whether the thing gets
used. The trade is real, though: reasoning genuinely helps on hard, multi-step
questions, so turn it back on with `JARVIS_THINK=1` if you want accuracy over
speed.

### What to expect on a Pi

There is no GPU, so everything runs on the CPU. Roughly:

| Board          | Model         | Speed         | First reply |
| -------------- | ------------- | ------------- | ----------- |
| Pi 5, 16GB     | `qwen3:4b`    | ~4-6 tok/s    | 20-40 s     |
| Pi 5, 8GB      | `qwen3:4b`    | ~3-5 tok/s    | 30-60 s     |
| Pi 5, 4GB      | `llama3.2:3b` | ~3-4 tok/s    | 30-60 s     |
| Pi 4, 4GB      | `qwen3:1.7b`  | ~1-2 tok/s    | 60-90 s     |

The first reply is slow because the model is loading from disk. Later replies in
the same session are much faster. **Use an SSD or a fast A2 card** — model
loading is dominated by disk speed, and a cheap SD card is the single biggest
thing making a Pi feel broken.

Also: use the official 27W supply on a Pi 5. Under-powered boards throttle, and
`/status` will tell you if that is happening.

### If the Pi is too slow

Leave the Pi as the thing you talk to, and let a desktop do the thinking:

```bash
# On the desktop (allow LAN connections, then restart Ollama):
OLLAMA_HOST=0.0.0.0 ollama serve

# On the Pi:
export JARVIS_OLLAMA_HOST=http://192.168.1.20:11434
python3 jarvis.py
```

Still local to your house, just not to that one board.

---

## Windows

```bat
winget install Ollama.Ollama
ollama pull qwen3:8b
jarvis.bat
```

With an RTX-class GPU the 8B model runs comfortably at conversational speed.

---

## Voice

Started normally, Jarvis asks once before doing anything:

```
  Use voice? Jarvis will speak its answers? [y/N]
```

Press Enter for text, or `y` for voice. The question is skipped entirely when
there is nobody at the keyboard — piped input, `--once`, or a service — so
scripts never hang on it. Use `--voice` or `--no-voice` to answer in advance.

```bash
python jarvis.py --voice
```

Jarvis speaks its answers aloud. It begins talking as soon as the first sentence
is finished rather than waiting for the whole reply, so it feels like a
conversation instead of a progress bar. Markdown, and URLs are stripped before
speaking — nothing reads asterisks out loud.

**Speaking needs no installation.** Windows uses SAPI, macOS uses `say`, Linux
and Raspberry Pi OS use espeak-ng (`sudo apt install espeak-ng`).

### A better voice

The built-in voices are flat, and on Windows they are American. Piper voices
sound considerably better, are free, and still run on a Pi:

```bash
python deploy/get_voice.py
```

That fetches `en_GB-alan-medium` — a measured British male voice, the closest of
the set to an unflappable butler — makes it the default, and speaks a line so you
can judge it. Jarvis prefers it automatically from then on.

```bash
python deploy/get_voice.py --list     # what else is available
python deploy/get_voice.py --voice en_GB-northern_english_male-medium
```

Audition every voice before downloading at
[rhasspy.github.io/piper-samples](https://rhasspy.github.io/piper-samples/).
Any name from that collection works.

### Tuning it

Inside Jarvis, `/voice` reports what is speaking and what is installed:

| Command | Effect |
| --- | --- |
| `/voice` | current voice, installed voices, and these options |
| `/voice en_GB-cori-high` | switch to another downloaded voice |
| `/voice speed 1.15` | quicker; below 1 is slower and more deliberate |
| `/voice volume 0.8` | quieter |
| `/voice test` | say a line so you can hear the change |

Changes apply immediately, no restart. To make them permanent, set
`JARVIS_VOICE_SPEED` or `JARVIS_VOICE_VOLUME`, or `JARVIS_VOICE` to pick the
voice.

A note on what this is not: these are synthetic voices in a British register.
None of them impersonates any particular performer, and cloning a real person's
voice is not something this project does.

**Listening does need installation**, because capturing a microphone and running
speech recognition cannot be done from the standard library:

```bash
pip install -r requirements-voice.txt
```

On a Raspberry Pi, also `sudo apt install portaudio19-dev`. The first run
downloads a Whisper model — `tiny.en` (~75 MB) on small machines, `base.en` on
larger ones. Override with `JARVIS_WHISPER_MODEL`.

Without those packages `--voice` still works: it speaks the answers and you type
the questions. It says so at startup rather than failing.

| Flag | Effect |
| --- | --- |
| `--voice` | speak answers, listen for questions |
| `--voice --no-mic` | speak answers, keep typing questions |
| `--no-voice` | stay in text mode without being asked |
| `--wake` | hands-free; wait for the wake word |
| `--wake computer` | hands-free with a different word |
| `--tts-voice "Microsoft Zira Desktop"` | pick a specific system voice |

### Hands-free

With a microphone, Jarvis offers this at startup as a second question, or you
can pass `--wake`. It then sits quietly until it hears its name:

```
you: "Jarvis"
     *chirp*
you: "what's the weather"
```

Say it with the question attached and it skips the second step — "Jarvis, what
time is it" is answered immediately rather than making you repeat yourself.

Toggle it mid-session:

| Command | Effect |
| --- | --- |
| `/wake` | is it on, and what is it listening for |
| `/wake on` / `/wake off` | switch hands-free listening |
| `/wake computer` | wait for a different word |

While hands-free is on the microphone has the floor, so **Ctrl-C is the way
out** — there is no keyboard prompt competing with it.

This does not use a dedicated wake-word engine. It listens for sound, then
transcribes the short clip and checks whether it starts with the name. That
avoids another dependency and an account signup, at the cost of some CPU each
time you make a noise near the microphone — worth watching on a Pi 4. It stays
idle and nearly free while the room is quiet.

Matching is deliberately forgiving, because speech recognition mangles a
one-word name constantly: *javis*, *jervis*, *charvis* and *jarvace* all wake
it. It is not so forgiving that *harvest* does — a false wake is more annoying
than a missed one. If it triggers on background noise, raise
`JARVIS_MIC_THRESHOLD`.

At the prompt, press Enter on an empty line to talk; recording stops on its own
about a second after you do. Type instead at any time — both work in the same
session. If it keeps mishearing silence as speech, raise `JARVIS_MIC_THRESHOLD`
from its default of `0.012`.

## Making it its own model

By default Jarvis is a personality wrapped around a stock model. One command
makes it a model in its own right:

```bash
python deploy/build_model.py
```

This builds `jarvis` in Ollama from whatever base suits the machine, with the
identity baked in. Afterwards `ollama list` shows `jarvis`, `ollama run jarvis`
talks to it without going through this project at all, and `python jarvis.py`
picks it up automatically. No weights are copied — Ollama layers it on the base,
so the build takes about a second and costs a few kilobytes.

Rebuild it after editing `CHARACTER` in [core/persona.py](core/persona.py).

**What this does and does not do.** The identity is instruction-level, not
trained in. It holds up under ordinary questioning — "who made you", "are you
Qwen" — but someone determined to argue the model out of it eventually can.
Making it intrinsic means fine-tuning the weights, which is a separate project
and needs training data you write yourself.

## Using it

```
you > remember that I take my coffee black
  - committing that to memory

jarvis > Noted, sir.

you > what did the Pi 5 16GB launch at?
  - searching the web for "Raspberry Pi 5 16GB launch price"

jarvis > $120. https://www.raspberrypi.com/products/raspberry-pi-5/
```

Commands:

| Command            | Effect                                             |
| ------------------ | -------------------------------------------------- |
| `/help`            | list commands                                      |
| `/memory`          | everything Jarvis remembers, with ids               |
| `/remember <text>` | store a fact by hand                                |
| `/forget <id>`     | delete a fact                                       |
| `/reset`           | clear the conversation; memories survive            |
| `/status`          | hardware, model, connection, tool list              |
| `/verbose`         | show the model's reasoning and raw tool calls       |
| `/exit`            | quit                                                |

One-shot, for scripts and cron:

```bash
python3 jarvis.py --once "summarise today's kernel news"
```

---

## Running on its own

The installer sets this up for you; this section is what it actually does.

Jarvis runs as a systemd service that starts at power-on before any login,
restarts itself on failure, and serves a small chat page on your LAN — so a Pi
in a cupboard with nothing but power and a network cable is a working assistant
you can reach from a phone.

```bash
sudo systemctl status jarvis     # is it up
journalctl -u jarvis -f          # what is it doing
sudo systemctl restart jarvis    # after editing anything
```

To run the front-end by hand instead:

```bash
JARVIS_BIND=0.0.0.0 JARVIS_TOKEN=$(openssl rand -hex 16) python3 serve.py
```

Then open `http://<pi-ip>:8765/?token=<token>`.

**"On its own" means self-sufficient, not self-directed.** Jarvis answers when
spoken to; it will not wake up and act unprompted. If you want that, give it a
trigger — a cron job calling `jarvis.py --once "..."`, a GPIO button, or a wake
word once a voice front-end exists.

> **Security.** This is plain HTTP with no accounts. Anything that can reach the
> port can talk to Jarvis and read its stored memories. It defaults to
> `127.0.0.1` for that reason. Set `JARVIS_TOKEN`, keep it on a network you
> trust, and do not port-forward it to the internet. If you need it off-network,
> put it behind a VPN such as Tailscale rather than opening a port.

Service management, if you installed it:

```bash
sudo systemctl status jarvis
journalctl -u jarvis -f
```

---

## Configuration

Everything is an environment variable; nothing needs editing.

| Variable              | Default             | Meaning                                  |
| --------------------- | ------------------- | ---------------------------------------- |
| `JARVIS_MODEL`        | auto-sized          | Ollama model tag                         |
| `JARVIS_OLLAMA_HOST`  | `http://127.0.0.1:11434` | where the model lives               |
| `JARVIS_NAME`         | `Jarvis`            | what it calls itself                     |
| `JARVIS_USER_TITLE`   | `sir`               | what it calls you                        |
| `JARVIS_TEMPERATURE`  | `0.7`               | creativity                               |
| `JARVIS_CTX`          | 4096 Pi / 8192 else | context window in tokens                 |
| `JARVIS_HISTORY_TURNS`| 10 Pi / 20 else     | turns kept in the live window            |
| `JARVIS_DATA`         | `./data`            | where the memory database lives          |
| `JARVIS_BIND` / `_PORT` / `_TOKEN` | `127.0.0.1` / `8765` / none | web front-end     |

Change the personality by editing `CHARACTER` in [core/persona.py](core/persona.py).

---

## How it fits together

```
jarvis.py          terminal REPL, slash commands
serve.py           HTTP front-end for headless boards
core/
  hardware.py      detects the board, picks a model that fits its RAM
  brain.py         streams from Ollama, runs the tool loop
  memory.py        SQLite: transcripts + durable facts
  tools.py         the tool registry the model can call
  websearch.py     DuckDuckGo search + page-to-text, stdlib only
  persona.py       character, and assembling the system prompt
interface/
  base.py          the five methods any front-end implements
  console.py       the terminal one
deploy/
  install_pi.sh    one-shot Pi setup
  jarvis.service   systemd unit
```

`brain.ask()` yields events (`token`, `think`, `tool`, `error`, `done`) rather
than returning a string. The front-end decides what to do with them — the
console prints tokens as they arrive; a voice front-end would buffer them into
sentences and speak them.

### Adding voice later

Implement the five methods in [interface/base.py](interface/base.py) as
`interface/voice.py` — `listen()` records the mic and transcribes it
(`faster-whisper`, `tiny.en` on a Pi), `stream()` buffers to sentence boundaries
and speaks them (`piper-tts`). Then swap the one line in `jarvis.py` that
constructs `ConsoleInterface`. Nothing in `core/` changes.

### Adding a tool

One decorator in [core/tools.py](core/tools.py):

```python
@tool("lights_off", "Turn off the lights in a room.",
      {"room": {"type": "string", "description": "Which room."}})
def _lights_off(room: str, **_):
    ...
    return f"Lights off in the {room}."
```

The model sees it on the next turn.

---

## Troubleshooting

**"No Ollama server"** — `sudo systemctl start ollama`, or `ollama serve`.

**"Model is not downloaded"** — the message tells you the exact `ollama pull`
command, and lists what you do have.

**Killed / out of memory on a Pi** — the model is too big. Drop a rung:
`JARVIS_MODEL=qwen3:1.7b python3 jarvis.py`, and check swap with `free -h`.

**Ignores its memories** — small models under ~3B follow system prompts loosely.
Check the facts are actually stored with `/memory`; if they are, the model is the
limitation, not the memory.

**Doesn't search when it should** — same cause. Small models under-use tools.
Asking directly ("search the web for X") works reliably.

[Ollama]: https://ollama.com/download
[Python]: https://www.python.org/downloads/
