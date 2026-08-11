#!/usr/bin/env bash
# Jarvis on a Raspberry Pi 4, tuned hard.
#
#   chmod +x deploy/install_pi4.sh
#   ./deploy/install_pi4.sh              # interactive
#   ./deploy/install_pi4.sh --headless   # no prompts, autostart
#
# A Pi 4 is roughly three times slower than a Pi 5 and has no GPU, so the
# general installer produces something technically working and miserable to
# use. This one trades capability for responsiveness at every decision:
#
#   * the smallest model that can still call tools
#   * hidden "thinking" turned off, which is the single biggest win
#   * a short context window, because attention cost grows with it
#   * the model pinned in RAM, so it is never re-read from a slow SD card
#   * zram instead of swap, because swapping to an SD card is agony
#   * the small Whisper and the low-quality voice
#
# Expect two to four words a second. That is what the hardware gives.

set -euo pipefail

JARVIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$JARVIS_DIR"

HEADLESS=0
WANT_SERVICE=""
WANT_VOICE=""
MODEL_OVERRIDE="${JARVIS_MODEL:-}"

usage() {
  cat <<'USAGE'
Usage: ./deploy/install_pi4.sh [options]

  --headless, -y   Accept every default, never wait for input.
  --service        Install the autostart service (default).
  --no-service     Do not autostart.
  --voice          Install voice support (adds ~200 MB and some CPU cost).
  --no-voice       Skip voice.
  --model TAG      Override the chosen model.
  -h, --help       This text.
USAGE
}

while (( $# )); do
  case "$1" in
    --headless|-y) HEADLESS=1 ;;
    --service)     WANT_SERVICE=1 ;;
    --no-service)  WANT_SERVICE=0 ;;
    --voice)       WANT_VOICE=1 ;;
    --no-voice)    WANT_VOICE=0 ;;
    --model)       shift; MODEL_OVERRIDE="${1:-}" ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
  shift
done

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$1"; }
note() { printf '  %s\n' "$1"; }

ask() {
  local prompt="$1" default="$2" reply=""
  if (( HEADLESS )); then
    printf '  %s -> %s (headless)\n' "$prompt" "$default"
    [[ "${default^^}" == "Y" ]]
    return
  fi
  local hint="[y/N]"
  if [[ "${default^^}" == "Y" ]]; then hint="[Y/n]"; fi
  read -rp "  $prompt $hint " reply || true
  reply="${reply:-$default}"
  [[ "${reply,,}" == "y" ]]
}

# --- the board ------------------------------------------------------------
say "Checking the board"

BOARD="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
RAM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
ARCH="$(uname -m)"

note "Board : $BOARD"
note "RAM   : ${RAM_MB} MB"
note "Arch  : $ARCH"

if [[ "$ARCH" != "aarch64" ]]; then
  warn "This is $ARCH, not aarch64. Ollama needs 64-bit Raspberry Pi OS."
  warn "A 32-bit install cannot run this at all. Reflash with the 64-bit image."
  ask "Continue anyway?" N || exit 1
fi

if [[ "$BOARD" != *"Pi 4"* && "$BOARD" != *"Pi 400"* ]]; then
  warn "This script is tuned for a Pi 4. Detected: $BOARD"
  if [[ "$BOARD" == *"Pi 5"* ]]; then
    warn "On a Pi 5 use ./deploy/install_pi.sh instead — it will be much better."
  fi
  ask "Carry on with Pi 4 settings?" N || exit 1
fi

if (( RAM_MB < 1800 )); then
  warn "Under 2 GB of RAM. Even the smallest model will struggle here."
  ask "Continue?" N || exit 1
fi

# --- cooling and power ----------------------------------------------------
# A throttled Pi 4 halves its own speed, and people blame the software.
say "Checking power and temperature"
if command -v vcgencmd >/dev/null 2>&1; then
  TEMP="$(vcgencmd measure_temp 2>/dev/null | tr -dc '0-9.' || echo 0)"
  THROTTLED="$(vcgencmd get_throttled 2>/dev/null || echo 'throttled=0x0')"
  note "Temperature : ${TEMP}C"
  note "Throttling  : $THROTTLED"

  if [[ "$THROTTLED" != "throttled=0x0" ]]; then
    warn "This board is being throttled — for power, heat, or both."
    warn "Expect roughly half speed until it is fixed."
    warn "Use the official 3A supply, and fit a heatsink or fan."
  fi
  if (( $(printf '%.0f' "${TEMP:-0}") > 70 )); then
    warn "Already ${TEMP}C at idle. Under load it will throttle. Add cooling."
  fi
else
  note "vcgencmd not present, skipping."
fi

# --- storage --------------------------------------------------------------
# Model load time is dominated by read speed. A slow card is the usual reason
# a Pi feels broken, and it is invisible unless you measure it.
say "Checking storage speed"
ROOT_SRC="$(findmnt -no SOURCE / 2>/dev/null || echo '')"
if [[ "$ROOT_SRC" == *mmcblk* ]]; then
  note "Running from an SD card."
  if command -v hdparm >/dev/null 2>&1; then
    SPEED="$(sudo hdparm -t "$ROOT_SRC" 2>/dev/null | grep -o '[0-9.]* MB/sec' | head -1 || echo '')"
    note "Read speed : ${SPEED:-unknown}"
    SPEED_NUM="${SPEED%% *}"
    if [[ -n "$SPEED_NUM" ]] && (( $(printf '%.0f' "$SPEED_NUM") < 40 )); then
      warn "Under 40 MB/s. The first reply after each idle period will crawl."
      warn "A USB SSD is the single best upgrade you can make here."
    fi
  fi
else
  note "Running from $ROOT_SRC — good, not an SD card."
fi

# --- memory ---------------------------------------------------------------
# zram compresses pages in RAM. On a Pi it beats a swapfile outright: no card
# wear, and decompression is far faster than an SD card read.
say "Setting up zram instead of SD-card swap"
if ask "Use zram? Recommended — it avoids swapping to the card" Y; then
  sudo apt-get install -y zram-tools >/dev/null 2>&1 || warn "Could not install zram-tools."
  if [[ -f /etc/default/zramswap ]]; then
    sudo sed -i 's/^#\?ALGO=.*/ALGO=zstd/' /etc/default/zramswap
    sudo sed -i "s/^#\?PERCENT=.*/PERCENT=50/" /etc/default/zramswap
    sudo systemctl restart zramswap 2>/dev/null || true
    note "zram on, using zstd at 50% of RAM."
  fi
  # Discourage the kernel from touching the card while zram is available.
  if ! grep -q '^vm.swappiness' /etc/sysctl.conf 2>/dev/null; then
    echo 'vm.swappiness=100' | sudo tee -a /etc/sysctl.conf >/dev/null
    sudo sysctl -q vm.swappiness=100 || true
  fi
fi

# --- ollama ---------------------------------------------------------------
say "Installing Ollama"
if command -v ollama >/dev/null 2>&1; then
  note "Already installed: $(ollama --version 2>/dev/null | head -1)"
else
  note "Downloads and runs the official installer from https://ollama.com/install.sh"
  if ! ask "Proceed?" Y; then
    echo "  Skipped. Install Ollama yourself and re-run."
    exit 1
  fi
  curl -fsSL https://ollama.com/install.sh | sh
fi

# Pin the model in memory. Re-reading it from an SD card after every idle
# timeout is the difference between a two second reply and a ninety second one.
say "Keeping the model resident in RAM"
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/pi4.conf >/dev/null <<'CONF'
[Service]
# Never unload: reloading from an SD card costs more than the RAM does.
Environment="OLLAMA_KEEP_ALIVE=-1"
# One model, one request at a time. Four slow cores cannot share.
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
CONF
sudo systemctl daemon-reload
sudo systemctl enable --now ollama >/dev/null 2>&1 || true
sudo systemctl restart ollama || true

say "Waiting for Ollama"
for _ in $(seq 1 45); do
  curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
  sleep 1
done
curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 \
  || { echo "Ollama did not start. Check: systemctl status ollama"; exit 1; }
note "Up."

# --- model ----------------------------------------------------------------
if (( RAM_MB < 2600 )); then
  MODEL="${MODEL_OVERRIDE:-qwen3:0.6b}"
  note "Under 2.5 GB of RAM, so choosing the very smallest model."
else
  MODEL="${MODEL_OVERRIDE:-qwen3:1.7b}"
fi

say "Downloading $MODEL"
note "The largest model a Pi 4 can run at a usable pace while still calling tools."
ollama pull "$MODEL"

# --- the tuned build ------------------------------------------------------
say "Building a Pi 4 profile"
export JARVIS_MODEL="$MODEL"
export JARVIS_CTX=2048
export JARVIS_THINK=0
python3 deploy/build_model.py --base "$MODEL" --name jarvis

cat > .env.pi4 <<ENVFILE
# Pi 4 profile, written by deploy/install_pi4.sh.
# Loaded by the service; for a manual run:  set -a; . ./.env.pi4; set +a
JARVIS_MODEL=jarvis
JARVIS_CTX=2048
JARVIS_THINK=0
JARVIS_MAX_TOKENS=400
JARVIS_HISTORY_TURNS=6
JARVIS_FACTS_IN_PROMPT=15
JARVIS_TEMPERATURE=0.6
JARVIS_WHISPER_MODEL=tiny.en
ENVFILE
note "Wrote .env.pi4"

# --- voice ----------------------------------------------------------------
if [[ -z "$WANT_VOICE" ]]; then
  say "Voice"
  note "Speaking costs little. Listening runs speech recognition on the CPU,"
  note "which on a Pi 4 takes a few seconds per phrase."
  if ask "Install voice support?" N; then WANT_VOICE=1; else WANT_VOICE=0; fi
fi

if [[ "$WANT_VOICE" == "1" ]]; then
  say "Installing voice"
  sudo apt-get install -y espeak-ng portaudio19-dev >/dev/null 2>&1 || \
    warn "Could not install audio packages."
  python3 -m pip install --break-system-packages -r requirements-voice.txt || \
    warn "Voice packages failed; text mode still works."
  # The low-quality voice, deliberately: medium is too slow to synthesise here.
  python3 deploy/get_voice.py --voice en_GB-alan-low || \
    warn "Voice download failed; espeak-ng will be used instead."
fi

# --- smoke test -----------------------------------------------------------
say "Smoke test"
set -a; . ./.env.pi4; set +a
START=$(date +%s)
python3 jarvis.py --once "Reply with exactly: Jarvis online." --no-voice
ELAPSED=$(( $(date +%s) - START ))
note "Took ${ELAPSED}s including loading the model."

# --- service --------------------------------------------------------------
say "Autostart"
INSTALL_SERVICE=0
if [[ "$WANT_SERVICE" == "1" ]]; then INSTALL_SERVICE=1
elif [[ "$WANT_SERVICE" == "0" ]]; then note "Skipping."
elif ask "Start Jarvis automatically at power-on?" Y; then INSTALL_SERVICE=1
fi

if (( INSTALL_SERVICE )); then
  TOKEN=""
  warn "The web page is plain HTTP with no accounts."
  if ask "Protect it with a random token?" Y; then
    TOKEN="$(head -c 18 /dev/urandom | base64 | tr -d '/+=')"
  fi

  sudo cp deploy/jarvis.service /etc/systemd/system/jarvis.service
  sudo sed -i \
    -e "s|__JARVIS_DIR__|$JARVIS_DIR|g" \
    -e "s|__USER__|$USER|g" \
    -e "s|__MODEL__|jarvis|g" \
    /etc/systemd/system/jarvis.service

  # Feed the tuned profile to the service too.
  sudo sed -i "/^Environment=PYTHONUNBUFFERED=1/a EnvironmentFile=$JARVIS_DIR/.env.pi4" \
    /etc/systemd/system/jarvis.service
  if [[ -n "$TOKEN" ]]; then
    sudo sed -i "s|^#Environment=JARVIS_TOKEN=.*|Environment=JARVIS_TOKEN=$TOKEN|" \
      /etc/systemd/system/jarvis.service
  fi

  sudo systemctl daemon-reload
  sudo systemctl enable jarvis
  sudo systemctl restart jarvis
  sleep 4
  if systemctl is-active --quiet jarvis; then
    note "Running, and will come back after a reboot."
  else
    warn "Did not start. Check: journalctl -u jarvis -n 50"
  fi

  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  note "Open: http://${IP:-<pi-ip>}:8765/${TOKEN:+?token=$TOKEN}"
  if [[ -n "$TOKEN" ]]; then note "Token: $TOKEN"; fi
fi

say "Done"
cat <<EOF
  Model      : $MODEL, no hidden reasoning, 2048-token context
  Talk to it : cd $JARVIS_DIR && set -a && . ./.env.pi4 && set +a && python3 jarvis.py
  Service    : sudo systemctl status jarvis
  Logs       : journalctl -u jarvis -f

  What to expect on a Pi 4: two to four words a second, and a couple of
  seconds before it starts. The model now stays in RAM, so only the first
  reply after a reboot is slow.

  If it feels slower than that, the cause is almost always heat, power, or a
  slow SD card rather than the software. Check with:  /status  inside Jarvis.
EOF
