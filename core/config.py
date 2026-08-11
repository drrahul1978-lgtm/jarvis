"""Central configuration for Jarvis.

Every value can be overridden with an environment variable so you can retune
without editing code, e.g.  set JARVIS_MODEL=qwen3:14b
"""

import os
from pathlib import Path

from . import hardware

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("JARVIS_DATA", ROOT / "data"))

# --- Brain ---------------------------------------------------------------
# Point this at another machine to make a Pi a thin client for a desktop GPU,
# e.g.  JARVIS_OLLAMA_HOST=http://192.168.1.20:11434
OLLAMA_HOST = os.environ.get("JARVIS_OLLAMA_HOST", "http://127.0.0.1:11434")

# No explicit model? Size one to the hardware we actually booted on.
SUGGESTED_MODEL, MODEL_REASON = hardware.suggest_model()
MODEL = os.environ.get("JARVIS_MODEL") or SUGGESTED_MODEL

TEMPERATURE = float(os.environ.get("JARVIS_TEMPERATURE", "0.7"))
# A Pi pays for context in seconds, so keep the window tighter there.
CONTEXT_TOKENS = int(
    os.environ.get("JARVIS_CTX", "4096" if hardware.is_raspberry_pi() else "8192")
)

# How many tool round-trips Jarvis may take before it must answer.
MAX_TOOL_HOPS = int(os.environ.get("JARVIS_MAX_TOOL_HOPS", "6"))

# Reasoning models emit a hidden train of thought before answering. It buys
# accuracy on hard questions and costs a great many tokens — which on a CPU-only
# board is the difference between a pause and an eternity. Off by default on a
# Pi, on everywhere else. Set JARVIS_THINK to "1" or "0" to decide for yourself.
_think = os.environ.get("JARVIS_THINK", "").strip()
THINK = (_think == "1") if _think in ("0", "1") else not hardware.is_raspberry_pi()

# Ceiling on a single reply. Stops a small model looping forever on a slow
# board; -1 means no limit.
MAX_TOKENS = int(
    os.environ.get("JARVIS_MAX_TOKENS", "512" if hardware.is_raspberry_pi() else "-1")
)

# --- Memory --------------------------------------------------------------
DB_PATH = DATA_DIR / "jarvis.db"
# Turns of the current conversation kept in the live context window.
HISTORY_TURNS = int(
    os.environ.get("JARVIS_HISTORY_TURNS", "10" if hardware.is_raspberry_pi() else "20")
)
# Long-term facts injected into every system prompt.
FACTS_IN_PROMPT = int(os.environ.get("JARVIS_FACTS_IN_PROMPT", "30"))

# --- Web -----------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HTTP_TIMEOUT = int(os.environ.get("JARVIS_HTTP_TIMEOUT", "20"))
MAX_PAGE_CHARS = int(os.environ.get("JARVIS_MAX_PAGE_CHARS", "6000"))

# --- Vision --------------------------------------------------------------
# Sized to the machine, like the language model. A Pi 4 can only really manage
# the nano detector, and even then a few frames a second; a desktop has room
# for the small one, which is noticeably better at partly hidden and distant
# objects. Override with JARVIS_VISION_MODEL=yolo11m.pt for better still.
_pi = hardware.is_raspberry_pi()
VISION_MODEL = os.environ.get(
    "JARVIS_VISION_MODEL",
    "yolo11n.pt" if (_pi or hardware.total_ram_gb() < 6) else "yolo11s.pt",
)
CAMERA_INDEX = int(os.environ.get("JARVIS_CAMERA", "0"))
# A lower bar finds more, at the cost of the occasional confident mistake.
VISION_CONFIDENCE = float(os.environ.get("JARVIS_VISION_CONFIDENCE", "0.35"))
# Webcams return dark frames until exposure settles, so throw the first few away.
CAMERA_WARMUP = int(os.environ.get("JARVIS_CAMERA_WARMUP", "6"))

# The live view shows video at one rate and re-detects at another. On a Pi the
# detector cannot keep up with the camera, and tying them together would make
# the picture stutter rather than the boxes lag — the worse of the two.
CAMERA_FPS = int(os.environ.get("JARVIS_CAMERA_FPS", "6" if _pi else "15"))
DETECT_FPS = float(os.environ.get("JARVIS_DETECT_FPS", "1.5" if _pi else "10"))

# For anything outside the detector's eighty classes. A vision-capable model
# has no fixed list, so it can name things the fast detector never could — at
# seconds per look rather than milliseconds.
# moondream, not a larger vision model: qwen2.5vl:3b returned a row of "@"
# characters in under a second for every prompt and every endpoint against
# Ollama 0.32, on a frame verified to be a valid JPEG. Broken pairings like
# that are worth pinning away from rather than debugging.
VISION_LLM = os.environ.get("JARVIS_VISION_LLM", "moondream")

# --- Persona -------------------------------------------------------------
NAME = os.environ.get("JARVIS_NAME", "Jarvis")
USER_TITLE = os.environ.get("JARVIS_USER_TITLE", "sir")

DATA_DIR.mkdir(parents=True, exist_ok=True)
