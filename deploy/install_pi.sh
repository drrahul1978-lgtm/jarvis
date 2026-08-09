#!/usr/bin/env bash
# Set Jarvis up on a Raspberry Pi (64-bit Raspberry Pi OS / Debian).
#
#   chmod +x deploy/install_pi.sh
#   ./deploy/install_pi.sh                 # interactive
#   ./deploy/install_pi.sh --headless      # appliance: no prompts, autostart
#
# Installs Ollama, pulls a model sized to this board, and registers Jarvis as a
# systemd service so the Pi comes up talking without anyone logging in.

set -euo pipefail

JARVIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$JARVIS_DIR"

HEADLESS=0
WANT_SERVICE=""       # empty = ask; 1 = yes; 0 = no
WANT_TOKEN=""
BIND_ADDR="0.0.0.0"
PORT="8765"

usage() {
  cat <<'USAGE'
Usage: ./deploy/install_pi.sh [options]

  --headless, -y     Assume the default answer to every prompt and never wait
                     for input. Suitable for a first-boot script or an image.
  --service          Install the autostart service (default).
  --no-service       Do not install the service; run Jarvis by hand only.
  --no-token         Do not generate a shared secret for the web front-end.
                     Only sensible on a network you fully control.
  --local-only       Bind the web front-end to 127.0.0.1 instead of the LAN.
  --model TAG        Use this Ollama model instead of the auto-sized one.
  --port N           Port for the web front-end (default 8765).
  -h, --help         This text.
USAGE
}

while (( $# )); do
  case "$1" in
    --headless|-y) HEADLESS=1 ;;
    --service)     WANT_SERVICE=1 ;;
    --no-service)  WANT_SERVICE=0 ;;
    --no-token)    WANT_TOKEN=0 ;;
    --local-only)  BIND_ADDR="127.0.0.1" ;;
    --model)       shift; JARVIS_MODEL="${1:-}" ;;
    --port)        shift; PORT="${1:-8765}" ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
  shift
done

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$1"; }

# ask <prompt> <default Y|N> -> exit status 0 for yes.
# In headless mode it takes the default without blocking, so this script is
# safe to run from a first-boot hook where stdin is closed.
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

# --- sanity checks --------------------------------------------------------
say "Checking the board"

ARCH="$(uname -m)"
if [[ "$ARCH" != "aarch64" ]]; then
  warn "Architecture is $ARCH, not aarch64."
  warn "Ollama needs 64-bit Raspberry Pi OS. A 32-bit install will not work."
  ask "Continue anyway?" N || exit 1
fi

RAM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
MODEL_NAME="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
echo "  Board: $MODEL_NAME"
echo "  RAM:   ${RAM_MB} MB"

if (( RAM_MB < 3500 )); then
  warn "Under 4GB of RAM. Only the smallest models will fit, and they will be slow."
fi

python3 --version >/dev/null 2>&1 || { echo "python3 is required."; exit 1; }
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if (( PY_MINOR < 10 )); then
  echo "Python 3.10+ required (found 3.$PY_MINOR). Try: sudo apt install python3.11"
  exit 1
fi
echo "  Python: $(python3 --version)"

# --- swap -----------------------------------------------------------------
# Model loading spikes memory. Without swap headroom the OOM killer wins.
SWAP_MB=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
if (( SWAP_MB < 1024 && RAM_MB < 8000 )); then
  say "Swap is only ${SWAP_MB} MB, which risks the OOM killer during model load"
  if ask "Raise swap to 2GB?" Y; then
    sudo dphys-swapfile swapoff
    sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile
    sudo dphys-swapfile setup
    sudo dphys-swapfile swapon
    echo "  Swap raised to 2GB."
  fi
fi

# --- ollama ---------------------------------------------------------------
if command -v ollama >/dev/null 2>&1; then
  say "Ollama already installed: $(ollama --version 2>/dev/null | head -1)"
else
  say "Installing Ollama"
  echo "  This downloads and runs the official installer from https://ollama.com/install.sh"
  if ! ask "Proceed?" Y; then
    echo "  Skipped. Install Ollama yourself, then re-run this script."
    exit 1
  fi
  curl -fsSL https://ollama.com/install.sh | sh
fi

sudo systemctl enable --now ollama 2>/dev/null || true

say "Waiting for the Ollama service"
for _ in $(seq 1 30); do
  curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
  sleep 1
done
curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 \
  || { echo "Ollama did not come up. Check: systemctl status ollama"; exit 1; }
echo "  Up."

# --- model ----------------------------------------------------------------
MODEL="${JARVIS_MODEL:-$(python3 -c 'from core import hardware; print(hardware.suggest_model()[0])')}"
say "Pulling model: $MODEL"
echo "  Sized to this board. Override with: --model <tag>"
echo "  This is a multi-gigabyte download."
ollama pull "$MODEL"

# --- smoke test -----------------------------------------------------------
say "Smoke test"
python3 jarvis.py --once "Reply with exactly: Jarvis online." --model "$MODEL"

# --- service --------------------------------------------------------------
say "Autostart service"
echo "  Jarvis starts at power-on, before anyone logs in, and serves a small"
echo "  chat page you can open from a laptop or phone on your network."

INSTALL_SERVICE=0
if [[ "$WANT_SERVICE" == "1" ]]; then
  INSTALL_SERVICE=1
elif [[ "$WANT_SERVICE" == "0" ]]; then
  echo "  Skipping (--no-service)."
elif ask "Install it so Jarvis runs on its own?" Y; then
  INSTALL_SERVICE=1
fi

if (( INSTALL_SERVICE )); then
  TOKEN=""
  if [[ "$BIND_ADDR" != "127.0.0.1" ]]; then
    warn "The web front-end is plain HTTP with no accounts."
    warn "Anyone who can reach port $PORT can talk to Jarvis and read its memories."
    if [[ "$WANT_TOKEN" != "0" ]] && ask "Protect it with a random shared token?" Y; then
      TOKEN="$(head -c 18 /dev/urandom | base64 | tr -d '/+=')"
    fi
  fi

  sudo cp deploy/jarvis.service /etc/systemd/system/jarvis.service
  sudo sed -i \
    -e "s|__JARVIS_DIR__|$JARVIS_DIR|g" \
    -e "s|__USER__|$USER|g" \
    -e "s|__MODEL__|$MODEL|g" \
    -e "s|^Environment=JARVIS_BIND=.*|Environment=JARVIS_BIND=$BIND_ADDR|" \
    -e "s|^Environment=JARVIS_PORT=.*|Environment=JARVIS_PORT=$PORT|" \
    /etc/systemd/system/jarvis.service

  if [[ -n "$TOKEN" ]]; then
    sudo sed -i "s|^#Environment=JARVIS_TOKEN=.*|Environment=JARVIS_TOKEN=$TOKEN|" \
      /etc/systemd/system/jarvis.service
  fi

  sudo systemctl daemon-reload
  sudo systemctl enable jarvis
  sudo systemctl restart jarvis

  # Give it a moment, then confirm it actually came up rather than claiming so.
  sleep 3
  if systemctl is-active --quiet jarvis; then
    echo "  Service is running, and will restart itself on boot or failure."
  else
    warn "Service did not start. Check: journalctl -u jarvis -n 50"
  fi

  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  IP="${IP:-<pi-ip>}"
  if [[ "$BIND_ADDR" == "127.0.0.1" ]]; then IP="127.0.0.1"; fi
  echo "  Open:  http://${IP}:${PORT}/${TOKEN:+?token=$TOKEN}"
  if [[ -n "$TOKEN" ]]; then
    echo "  Token: $TOKEN"
    echo "         (stored in /etc/systemd/system/jarvis.service)"
  fi
fi

say "Done"
cat <<EOF
  Talk to Jarvis:   cd $JARVIS_DIR && python3 jarvis.py
  One-shot:         python3 jarvis.py --once "what is the weather in Delhi"
  Model in use:     $MODEL
  Service:          sudo systemctl status jarvis
                    journalctl -u jarvis -f

  A Pi runs this on CPU alone, so expect a few tokens per second and a slow
  first reply while the model loads.
EOF
