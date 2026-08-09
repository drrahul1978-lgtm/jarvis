"""Figure out what machine Jarvis woke up on, and pick a model that fits.

The same source tree is meant to run on a workstation with a discrete GPU and
on a Raspberry Pi. Rather than making you keep two configs, Jarvis measures the
box at startup and chooses a model it can actually hold in memory.
"""

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

# (ollama tag, approximate q4 weight size in GB, note)
# Ordered strongest first. Sizes are the download, not the runtime footprint;
# context and KV cache add roughly another 40%, which OVERHEAD accounts for.
MODEL_LADDER = [
    ("qwen3:8b", 5.2, "8B - full-strength reasoning and tool use"),
    ("qwen3:4b", 2.6, "4B - strong reasoning at a workable pace on CPU"),
    ("llama3.2:3b", 2.0, "3B - dependable tool calling in a small footprint"),
    ("qwen3:1.7b", 1.4, "1.7B - minimum viable; expect terse, literal answers"),
]

OVERHEAD = 1.4


def total_ram_gb() -> float:
    """Total system RAM in GB, on Linux/macOS/Windows, without psutil."""
    # Linux (including Raspberry Pi OS)
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        text = meminfo.read_text()
        match = re.search(r"MemTotal:\s+(\d+)\s+kB", text)
        if match:
            return int(match.group(1)) / (1024 * 1024)

    # Windows
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return status.ullTotalPhys / (1024 ** 3)
        except Exception:  # noqa: BLE001
            pass

    # macOS / BSD
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0:
            return int(out.stdout.strip()) / (1024 ** 3)
    except Exception:  # noqa: BLE001
        pass

    return 4.0  # conservative guess


def is_raspberry_pi() -> bool:
    model = Path("/proc/device-tree/model")
    if model.exists():
        try:
            return "raspberry pi" in model.read_text(errors="ignore").lower()
        except OSError:
            return False
    return False


def board_name() -> str:
    model = Path("/proc/device-tree/model")
    if model.exists():
        try:
            return model.read_text(errors="ignore").strip("\x00 \n")
        except OSError:
            pass
    return f"{platform.system()} {platform.machine()}"


def has_nvidia_gpu() -> bool:
    return shutil.which("nvidia-smi") is not None


def pi_generation() -> int:
    """Which Pi is this? 5, 4, 3... or 0 if this is not a Pi at all."""
    if not is_raspberry_pi():
        return 0
    name = board_name().lower()
    # "Raspberry Pi 500" and "Compute Module 5" are Pi 5 class silicon.
    if "pi 5" in name or "pi 500" in name or "compute module 5" in name:
        return 5
    match = re.search(r"raspberry pi (\d+)", name)
    return int(match.group(1)) if match else 4


def suggest_model() -> tuple[str, str]:
    """Return (model_tag, reason) appropriate for this machine.

    On a Pi, RAM is not the only limit — it is often not even the binding one.
    A 16GB Pi 5 has room for an 8B model but runs it at roughly two tokens a
    second, which is unusable for conversation. So the ladder is capped by how
    fast the board's CPU is, then filtered by what its memory can actually hold.
    """
    ram = total_ram_gb()
    pi = pi_generation()

    if pi:
        # No GPU offload, and the OS wants its share.
        budget = ram * 0.70
        # Pi 5 tops out at 4B for a usable pace; Pi 4 and older at 1.7B.
        ceiling = 2.6 if pi >= 5 else 1.4
        ladder = [entry for entry in MODEL_LADDER if entry[1] <= ceiling]
    else:
        budget = ram * 0.90
        ladder = MODEL_LADDER

    for tag, weight, note in ladder:
        if budget >= weight * OVERHEAD:
            return tag, note

    smallest = ladder[-1] if ladder else MODEL_LADDER[-1]
    return smallest[0], smallest[2]


def summary() -> str:
    ram = total_ram_gb()
    bits = [board_name(), f"{ram:.1f} GB RAM"]
    if has_nvidia_gpu():
        bits.append("NVIDIA GPU")
    elif is_raspberry_pi():
        bits.append("CPU only")
    return " | ".join(bits)
