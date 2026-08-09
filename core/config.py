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

# --- Persona -------------------------------------------------------------
NAME = os.environ.get("JARVIS_NAME", "Jarvis")
USER_TITLE = os.environ.get("JARVIS_USER_TITLE", "sir")

DATA_DIR.mkdir(parents=True, exist_ok=True)
