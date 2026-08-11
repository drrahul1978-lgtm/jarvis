"""Talk to a Home Assistant instance on the local network.

Home Assistant exposes a plain REST API, so this needs no library: a bearer
token and urllib are enough.

The credential is stored in data/home_assistant.json, which is inside the
git-ignored data directory. It is deliberately never passed through the
conversation, because every message is written to the transcript database — a
token typed at Jarvis would be stored in plaintext and sit in every backup.
"""

import difflib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import config

CONFIG_PATH = config.DATA_DIR / "home_assistant.json"

# Entities that respond to a simple on/off, and the service that does it.
SWITCHABLE = {
    "light", "switch", "fan", "input_boolean", "automation", "script",
    "media_player", "climate", "cover", "humidifier", "siren",
}

# Domains worth showing when someone asks what is in the house. Excludes the
# hundreds of diagnostic entities a typical install accumulates.
INTERESTING = SWITCHABLE | {"sensor", "binary_sensor", "person", "lock", "camera"}


class NotConfigured(Exception):
    """No Home Assistant connection has been set up yet."""


class HomeAssistantError(Exception):
    """The instance rejected or failed the request."""


def load() -> dict:
    if not CONFIG_PATH.is_file():
        raise NotConfigured(
            "Home Assistant is not connected yet. Set it up inside Jarvis with "
            "the /home command."
        )
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise NotConfigured(f"Could not read {CONFIG_PATH}: {exc}") from exc


def save(url: str, token: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps({"url": url.rstrip("/"), "token": token}, indent=1),
        encoding="utf-8",
    )
    # Best effort on POSIX; Windows inherits the user's directory permissions.
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass


def is_configured() -> bool:
    return CONFIG_PATH.is_file()


def forget() -> bool:
    if CONFIG_PATH.is_file():
        CONFIG_PATH.unlink()
        return True
    return False


def _call(path: str, payload: dict | None = None, timeout: int = 15):
    settings = load()
    url = f"{settings['url']}/api/{path.lstrip('/')}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {settings['token']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
        return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise HomeAssistantError(
                "Home Assistant rejected the token. It may have been revoked. "
                "Reconnect with /home."
            ) from exc
        raise HomeAssistantError(f"Home Assistant returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise HomeAssistantError(
            f"Could not reach Home Assistant at {settings['url']} ({exc.reason})."
        ) from exc
    except json.JSONDecodeError as exc:
        raise HomeAssistantError("Home Assistant sent a reply we could not read.") from exc


def check(url: str, token: str) -> str:
    """Verify a URL and token before saving them. Returns the version string."""
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/config",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise HomeAssistantError("That token was rejected.") from exc
        raise HomeAssistantError(f"HTTP {exc.code} from {url}.") from exc
    except urllib.error.URLError as exc:
        raise HomeAssistantError(f"Could not reach {url} ({exc.reason}).") from exc
    except json.JSONDecodeError as exc:
        raise HomeAssistantError(
            f"{url} answered, but not like Home Assistant. Check the address."
        ) from exc

    name = data.get("location_name", "Home")
    version = data.get("version", "unknown")
    return f"{name} (Home Assistant {version})"


def states() -> list[dict]:
    result = _call("states")
    return result if isinstance(result, list) else []


def _friendly(entity: dict) -> str:
    return entity.get("attributes", {}).get("friendly_name") or entity["entity_id"]


def resolve(name: str, domains: set[str] | None = None) -> dict:
    """Find the entity someone means by 'the kitchen light'.

    People do not say `light.kitchen_ceiling_2`, so match on the friendly name,
    then fall back to fuzzy matching. Raises if the guess would be ambiguous.
    """
    wanted = name.strip().lower()
    candidates = [
        e for e in states()
        if not domains or e["entity_id"].split(".")[0] in domains
    ]
    if not candidates:
        raise HomeAssistantError("No matching devices found in Home Assistant.")

    exact = [e for e in candidates if _friendly(e).lower() == wanted
             or e["entity_id"].lower() == wanted]
    if exact:
        return exact[0]

    contains = [e for e in candidates if wanted in _friendly(e).lower()]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        names = ", ".join(_friendly(e) for e in contains[:6])
        raise HomeAssistantError(
            f"{name!r} matches several things: {names}. Which one?"
        )

    names = {_friendly(e).lower(): e for e in candidates}
    close = difflib.get_close_matches(wanted, list(names), n=1, cutoff=0.6)
    if close:
        return names[close[0]]

    raise HomeAssistantError(
        f"Nothing in Home Assistant is called {name!r}."
    )


def summarise(kind: str = "") -> str:
    """A readable inventory of the house."""
    entities = states()
    if not entities:
        return "Home Assistant returned no devices."

    wanted = kind.strip().lower()
    grouped: dict[str, list[str]] = {}
    for entity in entities:
        domain = entity["entity_id"].split(".")[0]
        if domain not in INTERESTING:
            continue
        if wanted and wanted not in domain and wanted not in _friendly(entity).lower():
            continue
        grouped.setdefault(domain, []).append(
            f"{_friendly(entity)} [{entity.get('state', '?')}]"
        )

    if not grouped:
        return f"Nothing matching {kind!r}." if kind else "No usable devices found."

    lines = []
    for domain in sorted(grouped):
        items = sorted(grouped[domain])
        shown = items[:20]
        lines.append(f"{domain} ({len(items)}):")
        lines.extend(f"  - {item}" for item in shown)
        if len(items) > len(shown):
            lines.append(f"  ... and {len(items) - len(shown)} more")
    return "\n".join(lines)


def state_of(name: str) -> str:
    entity = resolve(name)
    attributes = entity.get("attributes", {})
    unit = attributes.get("unit_of_measurement", "")
    value = f"{entity.get('state', 'unknown')}{(' ' + unit) if unit else ''}"
    extra = []
    for key in ("brightness", "current_temperature", "temperature", "battery_level"):
        if key in attributes:
            extra.append(f"{key.replace('_', ' ')}: {attributes[key]}")
    detail = f" ({', '.join(extra)})" if extra else ""
    return f"{_friendly(entity)} is {value}{detail}"


def control(name: str, action: str) -> str:
    """Turn something on, off, or toggle it."""
    action = action.strip().lower()
    if action not in ("on", "off", "toggle"):
        return f"Unknown action {action!r}. Use on, off, or toggle."

    entity = resolve(name, domains=SWITCHABLE)
    entity_id = entity["entity_id"]
    domain = entity_id.split(".")[0]
    service = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}[action]

    _call(f"services/{domain}/{service}", {"entity_id": entity_id})

    verb = {"on": "on", "off": "off", "toggle": "toggled"}[action]
    return f"{_friendly(entity)} is now {verb}."


def set_brightness(name: str, percent: int) -> str:
    entity = resolve(name, domains={"light"})
    percent = max(0, min(int(percent), 100))
    if percent == 0:
        return control(name, "off")
    _call(
        "services/light/turn_on",
        {"entity_id": entity["entity_id"], "brightness_pct": percent},
    )
    return f"{_friendly(entity)} set to {percent}%."


def set_temperature(name: str, degrees: float) -> str:
    entity = resolve(name, domains={"climate"})
    _call(
        "services/climate/set_temperature",
        {"entity_id": entity["entity_id"], "temperature": float(degrees)},
    )
    return f"{_friendly(entity)} set to {degrees} degrees."


def normalise_url(raw: str) -> str:
    """Accept what people actually type: an IP, a hostname, with or without port."""
    raw = raw.strip().rstrip("/")
    if not raw:
        return ""
    if not re.match(r"^https?://", raw):
        raw = "http://" + raw
    parsed = urllib.parse.urlparse(raw)
    if not parsed.port and parsed.scheme == "http":
        raw = f"{raw}:8123"          # Home Assistant's default
    return raw
