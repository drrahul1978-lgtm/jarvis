"""The tools Jarvis can reach for, and the registry that exposes them.

Each tool is declared once: a JSON schema the model sees, and a Python callable
that runs it. `dispatch` is deliberately forgiving — a model that hallucinates a
tool name or drops an argument gets a useful error string back rather than
crashing the session.
"""

import inspect
import platform
import shutil
import subprocess
from datetime import datetime

from . import hardware, websearch

_REGISTRY: dict[str, dict] = {}


def tool(name: str, description: str, parameters: dict, required: list[str] | None = None):
    """Register a function as a model-callable tool."""

    def wrap(fn):
        _REGISTRY[name] = {
            "fn": fn,
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": parameters,
                        "required": required or list(parameters.keys()),
                    },
                },
            },
        }
        return fn

    return wrap


# --- web -----------------------------------------------------------------


@tool(
    "web_search",
    "Search the live web. Use for current events, prices, releases, documentation, "
    "or any fact you are not certain of. Prefer this over guessing.",
    {
        "query": {"type": "string", "description": "The search query."},
        "max_results": {
            "type": "integer",
            "description": "How many results to return, 1-10. Default 5.",
        },
    },
    required=["query"],
)
def _web_search(query: str, max_results: int = 5, **_):
    return websearch.search(query, max_results=max(1, min(int(max_results), 10)))


@tool(
    "fetch_page",
    "Read the full text of a web page by URL. Use after web_search when a snippet "
    "is not enough to answer properly.",
    {"url": {"type": "string", "description": "The full URL to read."}},
)
def _fetch_page(url: str, **_):
    return websearch.fetch_page(url)


# --- memory --------------------------------------------------------------
# These close over the live Memory instance, bound in `bind_memory`.

_memory = None


def bind_memory(memory) -> None:
    global _memory
    _memory = memory


@tool(
    "remember",
    "Store a durable fact about the user or their world so it survives across "
    "sessions. Use for names, preferences, projects, relationships and standing "
    "instructions. Do not use for one-off task details.",
    {
        "fact": {
            "type": "string",
            "description": "A single self-contained fact, written in the third person, "
            "e.g. 'The user's dog is called Rufus.'",
        }
    },
)
def _remember(fact: str, **_):
    return _memory.remember(fact) if _memory else "Memory unavailable."


@tool(
    "recall",
    "Search your long-term memory for what you know about a topic. Use when the "
    "user refers to something from a past conversation.",
    {"query": {"type": "string", "description": "What to look for."}},
)
def _recall(query: str, **_):
    if not _memory:
        return "Memory unavailable."
    hits = _memory.search_facts(query, limit=10)
    if hits:
        return "\n".join(f"- {h['text']}" for h in hits)

    # Keyword overlap missed. The fact table is small, so rather than a bare
    # "nothing found" — which invites the model to claim it knows nothing —
    # hand back what is stored and let it judge relevance itself.
    recent = _memory.all_facts(limit=15)
    if not recent:
        return "Long-term memory is empty."
    listing = "\n".join(f"- {f['text']}" for f in recent)
    return (
        f"No direct match for {query!r}. Everything currently stored:\n{listing}"
    )


# --- local -------------------------------------------------------------------


@tool(
    "get_datetime",
    "The current local date and time. Use before any date arithmetic.",
    {},
    required=[],
)
def _get_datetime(**_):
    now = datetime.now()
    return now.strftime("%A, %d %B %Y, %H:%M:%S %Z").strip()


@tool(
    "system_status",
    "Health of the machine Jarvis is running on: memory, disk, uptime, and CPU "
    "temperature where available. Useful on a Raspberry Pi.",
    {},
    required=[],
)
def _system_status(**_):
    lines = [f"Host: {hardware.board_name()}", f"Python: {platform.python_version()}"]
    lines.append(f"RAM: {hardware.total_ram_gb():.1f} GB total")

    try:
        usage = shutil.disk_usage("/" if platform.system() != "Windows" else "C:\\")
        lines.append(
            f"Disk: {usage.free / 1024**3:.1f} GB free of {usage.total / 1024**3:.1f} GB"
        )
    except OSError:
        pass

    # Pi-specific readings; silently skipped elsewhere.
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            lines.append(f"CPU temp: {int(fh.read().strip()) / 1000:.1f} C")
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/uptime") as fh:
            lines.append(f"Uptime: {float(fh.read().split()[0]) / 3600:.1f} hours")
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3
        )
        if out.returncode == 0 and out.stdout.strip() != "throttled=0x0":
            lines.append(f"WARNING - power/thermal throttling: {out.stdout.strip()}")
    except Exception:  # noqa: BLE001
        pass

    return "\n".join(lines)


# --- registry access -----------------------------------------------------


def schemas() -> list[dict]:
    return [entry["schema"] for entry in _REGISTRY.values()]


def names() -> list[str]:
    return list(_REGISTRY)


def dispatch(name: str, arguments: dict) -> str:
    """Run a tool by name. Always returns a string for the model to read."""
    entry = _REGISTRY.get(name)
    if not entry:
        return f"No such tool {name!r}. Available: {', '.join(_REGISTRY)}."

    if not isinstance(arguments, dict):
        return f"Tool {name} expects an object of arguments, got {type(arguments).__name__}."

    try:
        return str(entry["fn"](**arguments))
    except TypeError as exc:
        sig = inspect.signature(entry["fn"])
        return f"Bad arguments for {name} ({exc}). Expected: {sig}."
    except Exception as exc:  # noqa: BLE001 - the model should see the failure
        return f"Tool {name} failed: {type(exc).__name__}: {exc}"
