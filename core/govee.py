"""Control Govee lights directly over the local network.

Govee devices expose a small UDP API on the LAN, so this needs no account, no
API key, and no internet — which suits a project whose whole point is running
on your own hardware. Discovery is a multicast probe; control is a single UDP
packet per command.

The catch, and it is the first thing to check when nothing appears: LAN control
is off by default and has to be enabled per device in the Govee Home app, under
the device's settings. Older models do not support it at all, and those need
the cloud API instead.
"""

import json
import socket
import struct
import threading
import time

MULTICAST_GROUP = "239.255.255.250"
SCAN_PORT = 4001        # where devices listen for the scan request
LISTEN_PORT = 4002      # where they reply
CONTROL_PORT = 4003     # where they take commands

_devices: dict[str, dict] = {}
_scanned_at = 0.0
_lock = threading.Lock()
DISCOVERY_TTL = 300


class GoveeError(Exception):
    """No device, or it would not answer."""


def _send_scan(sock: socket.socket) -> None:
    message = json.dumps(
        {"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}
    ).encode()
    sock.sendto(message, (MULTICAST_GROUP, SCAN_PORT))


def discover(timeout: float = 3.0, force: bool = False) -> dict[str, dict]:
    """Find Govee devices with LAN control switched on.

    Results are cached: discovery costs seconds, which is far too slow to
    repeat while somebody is waiting for a light to come on.
    """
    global _scanned_at

    with _lock:
        if _devices and not force and (time.time() - _scanned_at) < DISCOVERY_TTL:
            return _devices

        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("0.0.0.0", LISTEN_PORT))
            # Join the multicast group on every interface we can, because a
            # machine with several adapters will otherwise listen on the wrong
            # one and quietly find nothing.
            membership = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP),
                                     socket.INADDR_ANY)
            listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            listener.settimeout(0.4)

            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            _send_scan(sender)

            found: dict[str, dict] = {}
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw, addr = listener.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    payload = json.loads(raw.decode("utf-8", errors="replace"))
                    data = payload.get("msg", {}).get("data", {})
                except (json.JSONDecodeError, AttributeError):
                    continue
                if not data.get("device"):
                    continue
                found[data["device"]] = {
                    "device": data["device"],
                    "model": data.get("sku") or data.get("model") or "Govee",
                    "ip": data.get("ip") or addr[0],
                }

            sender.close()
            if found:
                _devices.clear()
                _devices.update(found)
                _scanned_at = time.time()
            return _devices
        except OSError as exc:
            raise GoveeError(
                f"Could not listen for Govee devices ({exc}). Another program may "
                f"be using UDP port {LISTEN_PORT}."
            ) from exc
        finally:
            listener.close()


def _command(ip: str, payload: dict) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(json.dumps(payload).encode(), (ip, CONTROL_PORT))
    except OSError as exc:
        raise GoveeError(f"Could not reach the light at {ip}: {exc}") from exc
    finally:
        sock.close()


def _label(info: dict) -> str:
    return f"{info['model']} at {info['ip']}"


def resolve(name: str) -> dict:
    """Match however someone refers to a light: model, address, or 'the govee'."""
    devices = discover()
    if not devices:
        raise GoveeError(
            "No Govee devices answered. LAN control has to be switched on for "
            "each device in the Govee Home app, under the device's settings. "
            "Some older models do not support it at all."
        )

    wanted = (name or "").strip().lower()
    if not wanted or wanted in ("govee", "the govee", "light", "strip", "lights"):
        if len(devices) == 1:
            return next(iter(devices.values()))
        listed = ", ".join(_label(d) for d in devices.values())
        raise GoveeError(f"Which one? {listed}")

    for info in devices.values():
        if wanted in info["model"].lower() or wanted == info["ip"] \
                or wanted in info["device"].lower():
            return info

    listed = ", ".join(_label(d) for d in devices.values())
    raise GoveeError(f"No Govee device matching {name!r}. Found: {listed}")


def listing() -> str:
    try:
        devices = discover()
    except GoveeError as exc:
        return str(exc)
    if not devices:
        return (
            "No Govee devices found. LAN control must be enabled for each device "
            "in the Govee Home app (device settings → LAN Control). Older models "
            "do not support it."
        )
    lines = [f"  - {_label(info)}" for info in devices.values()]
    return "Govee devices on the network:\n" + "\n".join(sorted(lines))


def turn(name: str, on: bool) -> str:
    info = resolve(name)
    _command(info["ip"], {"msg": {"cmd": "turn", "data": {"value": 1 if on else 0}}})
    return f"{_label(info)} turned {'on' if on else 'off'}."


def brightness(name: str, percent: int) -> str:
    info = resolve(name)
    level = max(0, min(int(percent), 100))
    _command(info["ip"], {"msg": {"cmd": "brightness", "data": {"value": level}}})
    return f"{_label(info)} set to {level}%."


# Names people actually use, rather than hex codes.
COLOURS = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
    "white": (255, 255, 255), "warm white": (255, 180, 107),
    "cool white": (200, 220, 255), "yellow": (255, 255, 0),
    "orange": (255, 140, 0), "purple": (150, 0, 255), "violet": (150, 0, 255),
    "pink": (255, 105, 180), "cyan": (0, 255, 255), "teal": (0, 180, 180),
    "magenta": (255, 0, 255), "lime": (150, 255, 0), "amber": (255, 190, 60),
}


def colour(name: str, shade: str) -> str:
    info = resolve(name)
    key = shade.strip().lower()

    if key in COLOURS:
        red, green, blue = COLOURS[key]
    elif key.startswith("#") and len(key) == 7:
        try:
            red, green, blue = (int(key[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            return f"{shade!r} is not a colour I recognise."
    else:
        import difflib

        close = difflib.get_close_matches(key, list(COLOURS), n=1, cutoff=0.7)
        if not close:
            return (
                f"I do not know the colour {shade!r}. Try one of: "
                + ", ".join(sorted(COLOURS))
            )
        red, green, blue = COLOURS[close[0]]
        key = close[0]

    _command(info["ip"], {
        "msg": {
            "cmd": "colorwc",
            "data": {"color": {"r": red, "g": green, "b": blue},
                     "colorTemInKelvin": 0},
        }
    })
    return f"{_label(info)} set to {key}."
