#!/usr/bin/env bash
#
# PiBlock - stop the background DNS filter gracefully.
#
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/piblock.pid"

info() { printf '[piblock] %s\n' "$*"; }
warn() { printf '[piblock] warning: %s\n' "$*" >&2; }
die()  { printf '[piblock] error: %s\n' "$*" >&2; exit 1; }

is_ours() {
    [ -r "/proc/$1/cmdline" ] || return 1
    CMDLINE="$(tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null || true)"
    case "$CMDLINE" in
        *main.py*) return 0 ;;
    esac
    return 1
}

if [ ! -f "$PID_FILE" ]; then
    info "PiBlock is not running."
    exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
case "$PID" in
    ''|*[!0-9]*)
        rm -f "$PID_FILE"
        info "removed stale pid file; PiBlock is not running."
        exit 0
        ;;
esac

if [ ! -d "/proc/$PID" ]; then
    rm -f "$PID_FILE"
    info "PiBlock is not running (removed stale pid file)."
    exit 0
fi

if ! is_ours "$PID"; then
    die "pid $PID does not look like PiBlock; refusing to stop it. Remove $PID_FILE manually if it is stale."
fi

if ! kill -TERM "$PID" 2>/dev/null; then
    if [ ! -d "/proc/$PID" ]; then
        rm -f "$PID_FILE"
        info "PiBlock is not running."
        exit 0
    fi
    if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
        info "stopping PiBlock needs root (it was started with sudo) - re-running with sudo"
        exec sudo bash "$SCRIPT_DIR/stop.sh"
    fi
    die "could not stop pid $PID (permission denied). Try: sudo ./stop.sh"
fi

info "stopping PiBlock (pid $PID) ..."
for _ in $(seq 1 40); do
    if [ ! -d "/proc/$PID" ] || ! is_ours "$PID"; then
        rm -f "$PID_FILE"
        info "PiBlock stopped."
        exit 0
    fi
    sleep 0.25
done

warn "PiBlock did not stop within 10 seconds; sending SIGKILL"
kill -KILL "$PID" 2>/dev/null || true
sleep 0.5
rm -f "$PID_FILE"

if [ -d "/proc/$PID" ]; then
    die "could not stop pid $PID"
fi

info "PiBlock stopped (forced)."
