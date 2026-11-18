#!/usr/bin/env bash
#
# PiBlock - start the DNS filter in the background.
#
#   ./start.sh            (asks for sudo automatically when port 53 is used)
#   sudo ./start.sh       (always works)
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONFIG="${PIBLOCK_CONFIG:-$SCRIPT_DIR/config.yaml}"
PID_FILE="$SCRIPT_DIR/piblock.pid"
LOG_DIR="$SCRIPT_DIR/logs"
OUT_LOG="$LOG_DIR/piblock.out"
VENV="$SCRIPT_DIR/.venv"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

info() { printf '[piblock] %s\n' "$*"; }
warn() { printf '[piblock] warning: %s\n' "$*" >&2; }
die()  { printf '[piblock] error: %s\n' "$*" >&2; exit 1; }

ELEVATED=0
if [ "${1:-}" = "--elevated" ]; then
    ELEVATED=1
fi

# ---------------------------------------------------------------- already up?
if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    case "$OLD_PID" in
        ''|*[!0-9]*) OLD_PID="" ;;
    esac
    if [ -n "$OLD_PID" ] && [ -r "/proc/$OLD_PID/cmdline" ]; then
        OLD_CMDLINE="$(tr '\0' ' ' < "/proc/$OLD_PID/cmdline" 2>/dev/null || true)"
        case "$OLD_CMDLINE" in
            *main.py*)
                info "PiBlock is already running (pid $OLD_PID). Use ./status.sh or ./stop.sh."
                exit 0
                ;;
        esac
    fi
    info "removing stale pid file"
    rm -f "$PID_FILE"
fi

# ---------------------------------------------------------------- config file
if [ ! -f "$CONFIG" ]; then
    die "config file not found: $CONFIG (restore config.yaml or set PIBLOCK_CONFIG)"
fi
CONFIG="$(cd -- "$(dirname -- "$CONFIG")" && pwd)/$(basename -- "$CONFIG")"

# ---------------------------------------------------------------- interpreter
PYTHON_BIN="${PIBLOCK_PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 || true)"
fi
[ -n "$PYTHON_BIN" ] || die "python3 not found. Install it with: sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Python 3.9 or newer is required (found $("$PYTHON_BIN" -V 2>&1))"

# ---------------------------------------------------------------- virtualenv
if [ ! -x "$VENV/bin/python" ]; then
    info "creating Python virtual environment in .venv ..."
    "$PYTHON_BIN" -m venv "$VENV" \
        || die "could not create .venv. On Raspberry Pi OS run: sudo apt install -y python3-venv"
fi
VPY="$VENV/bin/python"

if [ -f "$REQUIREMENTS" ]; then
    WANT_HASH="$(sha256sum "$REQUIREMENTS" | awk '{print $1}')"
    HAVE_HASH="$(cat "$VENV/.requirements.sha256" 2>/dev/null || true)"
    if [ "$WANT_HASH" != "$HAVE_HASH" ] || ! "$VPY" -c 'import dns, yaml' >/dev/null 2>&1; then
        info "installing Python dependencies (dnspython, PyYAML) ..."
        "$VPY" -m pip install --quiet --disable-pip-version-check --no-input -r "$REQUIREMENTS" \
            || die "could not install Python dependencies. Check the network or see README.md troubleshooting"
        printf '%s\n' "$WANT_HASH" > "$VENV/.requirements.sha256"
    fi
fi

# ---------------------------------------------------------------- validate
info "checking configuration ..."
"$VPY" "$SCRIPT_DIR/main.py" --check-config --config "$CONFIG" \
    || die "configuration is invalid; fix config.yaml and try again"

LISTEN_ADDR="$("$VPY" -c 'import sys,yaml;print((yaml.safe_load(open(sys.argv[1],encoding="utf-8")) or {}).get("listen","0.0.0.0"))' "$CONFIG")"
LISTEN_PORT="$("$VPY" -c 'import sys,yaml;print(int((yaml.safe_load(open(sys.argv[1],encoding="utf-8")) or {}).get("port",53)))' "$CONFIG")"
BLOCKED_LIST="$("$VPY" -c 'import sys,yaml;c=yaml.safe_load(open(sys.argv[1],encoding="utf-8")) or {};print(", ".join(str(d) for d in (c.get("blocked_domains") or [])) or "(none)")' "$CONFIG")"
LOG_DIR_CFG="$("$VPY" -c 'import os,sys,yaml
c=yaml.safe_load(open(sys.argv[1],encoding="utf-8")) or {}
p=os.path.expanduser(str(c.get("log_file","logs/piblock.log")))
p=p if os.path.isabs(p) else os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])),p)
print(os.path.dirname(p))' "$CONFIG")"

case "$LISTEN_PORT" in
    ''|*[!0-9]*) die "could not read the DNS port from $CONFIG" ;;
esac

# ---------------------------------------------------------------- privileges
if [ "$LISTEN_PORT" -lt 1024 ] && [ "$(id -u)" -ne 0 ]; then
    if [ "$ELEVATED" -eq 1 ]; then
        die "still not root after sudo; refusing to continue"
    fi
    if command -v sudo >/dev/null 2>&1; then
        info "port $LISTEN_PORT needs root privileges - re-running with sudo"
        exec sudo --preserve-env=PIBLOCK_CONFIG bash "$SCRIPT_DIR/start.sh" --elevated
    fi
    die "port $LISTEN_PORT needs root. Re-run as root: sudo ./start.sh"
fi

# ---------------------------------------------------------------- port free?
"$VPY" - "$LISTEN_ADDR" "$LISTEN_PORT" <<'PY' || die "port $LISTEN_PORT is already in use (see the hint above)"
import errno, socket, sys

host, port = sys.argv[1], int(sys.argv[2])
family = socket.AF_INET6 if ":" in host else socket.AF_INET
for name, socktype in (("UDP", socket.SOCK_DGRAM), ("TCP", socket.SOCK_STREAM)):
    sock = socket.socket(family, socktype)
    try:
        if socktype == socket.SOCK_STREAM:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError as exc:
        print(f"[piblock] error: cannot bind {host}:{port} ({name}): {exc.strerror or exc}", file=sys.stderr)
        if exc.errno == errno.EADDRINUSE:
            print("[piblock] hint: another DNS server is using this port. Free it first:", file=sys.stderr)
            print("[piblock]       sudo systemctl disable --now systemd-resolved", file=sys.stderr)
            print("[piblock]       (or stop dnsmasq / AdGuard Home / Pi-hole)", file=sys.stderr)
            print("[piblock]       see README.md > Troubleshooting > Port 53 is already in use", file=sys.stderr)
        sys.exit(1)
    finally:
        sock.close()
PY

# ---------------------------------------------------------------- start
mkdir -p "$LOG_DIR"
: >> "$OUT_LOG"
info "starting PiBlock ..."
if command -v setsid >/dev/null 2>&1; then
    setsid "$VPY" "$SCRIPT_DIR/main.py" --config "$CONFIG" --pidfile "$PID_FILE" \
        >> "$OUT_LOG" 2>&1 < /dev/null &
else
    nohup "$VPY" "$SCRIPT_DIR/main.py" --config "$CONFIG" --pidfile "$PID_FILE" \
        >> "$OUT_LOG" 2>&1 < /dev/null &
fi

READY=0
RUN_PID=""
for _ in $(seq 1 40); do
    if [ -f "$PID_FILE" ]; then
        RUN_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [ -n "$RUN_PID" ] && [ -d "/proc/$RUN_PID" ]; then
            READY=1
            break
        fi
    fi
    sleep 0.5
done

if [ "$READY" -ne 1 ]; then
    warn "PiBlock did not become ready. Last log lines:"
    tail -n 25 "$OUT_LOG" >&2 || true
    exit 1
fi

info "PiBlock is running (pid $RUN_PID)"
info "  listening: $LISTEN_ADDR:$LISTEN_PORT (UDP + TCP)"
info "  blocking:  $BLOCKED_LIST"
info "  logs:      $LOG_DIR_CFG"
info "  control:   ./status.sh   ./stop.sh"
