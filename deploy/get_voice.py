#!/usr/bin/env python3
"""Download and audition a speaking voice for Jarvis.

The voices built into Windows and Raspberry Pi OS are serviceable but flat, and
on Windows they are American. Piper voices sound markedly better, are free, and
still run comfortably on a Pi.

    python deploy/get_voice.py                 # fetch the recommended voice
    python deploy/get_voice.py --list          # everything on offer
    python deploy/get_voice.py --voice en_GB-northern_english_male-medium
    python deploy/get_voice.py --try           # hear the installed voice

Listen to samples of all of them first at:
    https://rhasspy.github.io/piper-samples/

Voices land in data/voices/, which is not committed — they are tens of
megabytes and belong to their own authors.
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402

VOICES_DIR = config.DATA_DIR / "voices"

# A shortlist for an assistant, rather than all 100+ languages. British male
# voices first, since that is the register people mean by "like Jarvis".
SUGGESTED = [
    ("en_GB-alan-medium", "British male, measured and dry. The closest to a butler."),
    ("en_GB-alan-low", "The same voice, smaller and faster. Better on a Pi 4."),
    ("en_GB-northern_english_male-medium", "British male, northern, warmer."),
    ("en_GB-semaine-medium", "British, several speakers in one file."),
    ("en_GB-cori-high", "British female, crisp. The best quality of the set."),
    ("en_GB-jenny_dioco-medium", "British female, softer and quicker."),
    ("en_US-ryan-high", "American male, deep. Good if you dislike the accent."),
    ("en_US-amy-medium", "American female, neutral."),
]

DEFAULT = "en_GB-alan-medium"


def installed() -> list[str]:
    if not VOICES_DIR.exists():
        return []
    return sorted(p.stem for p in VOICES_DIR.glob("*.onnx"))


def show_list() -> None:
    have = installed()
    print("\nSuggested voices  (hear them: https://rhasspy.github.io/piper-samples/)\n")
    for name, note in SUGGESTED:
        mark = "installed" if name in have else ""
        print(f"  {name:<38} {note}")
        if mark:
            print(f"  {'':<38} [{mark}]")
    print("\nAny voice from the Piper collection works; pass its full name to --voice.")
    if have:
        print(f"\nCurrently installed: {', '.join(have)}")


def download(name: str) -> Path:
    from piper.download_voices import download_voice

    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    target = VOICES_DIR / f"{name}.onnx"

    if target.exists():
        print(f"Already have {name}.")
        return target

    print(f"Downloading {name} (usually 20-70 MB)...")
    download_voice(name, VOICES_DIR)
    if not target.exists():
        raise SystemExit(f"Download finished but {target} is missing.")
    print(f"Saved to {target}")
    return target


def audition(name: str, text: str = "") -> None:
    """Say a line in the chosen voice so you can judge it."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from interface.voice import PiperSpeaker

    line = text or (
        "Good evening. All systems are running normally, and the house is quiet. "
        "Shall I bring you up to speed?"
    )
    speaker = PiperSpeaker(VOICES_DIR / f"{name}.onnx")
    print(f"\nSpeaking as {name}:\n  \"{line}\"\n")
    speaker.speak(line)
    speaker.close()


def write_choice(name: str) -> None:
    """Remember the choice so `python jarvis.py` picks it up with no flags."""
    marker = config.DATA_DIR / "voice.json"
    marker.write_text(json.dumps({"voice": name}, indent=1), encoding="utf-8")
    print(f"\nJarvis will now use {name}.")
    print("Change it any time with:  /voice   inside Jarvis")


def main() -> int:
    parser = argparse.ArgumentParser(description="Get a better voice for Jarvis.")
    parser.add_argument("--voice", default=DEFAULT, help=f"Voice name (default {DEFAULT}).")
    parser.add_argument("--list", action="store_true", help="Show suggested voices.")
    parser.add_argument("--try", dest="audition", action="store_true",
                        help="Speak a sample line and exit.")
    parser.add_argument("--say", default="", help="Custom line for --try.")
    parser.add_argument("--keep-current", action="store_true",
                        help="Download without making it the default.")
    args = parser.parse_args()

    if args.list:
        show_list()
        return 0

    try:
        import piper  # noqa: F401
    except ImportError:
        print("Piper is not installed. Run:  pip install piper-tts")
        return 1

    if args.audition and args.voice in installed():
        audition(args.voice, args.say)
        return 0

    download(args.voice)
    if not args.keep_current:
        write_choice(args.voice)
    audition(args.voice, args.say)
    return 0


if __name__ == "__main__":
    sys.exit(main())
