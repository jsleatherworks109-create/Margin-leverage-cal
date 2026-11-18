#!/usr/bin/env bash
#
# PiBlock - show status, counters and a quick self-test.
# Exit codes: 0 = running, 3 = stopped.
#
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${PIBLOCK_CONFIG:-$SCRIPT_DIR/config.yaml}"
PID_FILE="$SCRIPT_DIR/piblock.pid"
LOG_DIR="$SCRIPT_DIR/logs"
STATS_FILE="$LOG_DIR/stats.json"
BLOCKED_LOG="$LOG_DIR/blocked.log"

VPY="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$VPY" ]; then
    VPY="$(command -v python3 || true)"
fi

# Resolve log paths from the config (relative paths are relative to config.yaml).
if [ -n "${VPY:-}" ] && [ -f "$CONFIG" ]; then
    CONFIG_PATHS="$("$VPY" - "$CONFIG" <<'PY' 2>/dev/null
import os
import sys

try:
    import yaml
except Exception:
    sys.exit(0)
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}
except Exception:
    sys.exit(0)
if not isinstance(cfg, dict):
    sys.exit(0)
base = os.path.dirname(os.path.abspath(sys.argv[1]))

def resolve(value, default):
    path = os.path.expanduser(str(value or default))
    return path if os.path.isabs(path) else os.path.join(base, path)

log_file = resolve(cfg.get("log_file"), "logs/piblock.log")
blocked = resolve(cfg.get("blocked_log"), "logs/blocked.log")
print(f"{os.path.dirname(log_file)}|{blocked}|{os.path.join(os.path.dirname(log_file), 'stats.json')}")
PY
)"
    if [ -n "$CONFIG_PATHS" ]; then
        IFS='|' read -r LOG_DIR BLOCKED_LOG STATS_FILE <<< "$CONFIG_PATHS"
    fi
fi

PID=""
RUNNING=0
if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
fi
case "$PID" in
    ''|*[!0-9]*) PID="" ;;
esac
if [ -n "$PID" ] && [ -r "/proc/$PID/cmdline" ]; then
    CMDLINE="$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null || true)"
    case "$CMDLINE" in
        *main.py*) RUNNING=1 ;;
    esac
fi

if [ "$RUNNING" -eq 1 ]; then
    echo "PiBlock status: running (pid $PID)"
    UPTIME="$(ps -o etimes= -p "$PID" 2>/dev/null | tr -d ' ' || true)"
    case "$UPTIME" in
        ''|*[!0-9]*) ;;
        *)
            DAYS=$((UPTIME / 86400))
            HOURS=$(((UPTIME % 86400) / 3600))
            MINS=$(((UPTIME % 3600) / 60))
            SECS=$((UPTIME % 60))
            if [ "$DAYS" -gt 0 ]; then
                printf '  uptime:    %dd %02dh %02dm %02ds\n' "$DAYS" "$HOURS" "$MINS" "$SECS"
            elif [ "$HOURS" -gt 0 ]; then
                printf '  uptime:    %dh %02dm %02ds\n' "$HOURS" "$MINS" "$SECS"
            else
                printf '  uptime:    %dm %02ds\n' "$MINS" "$SECS"
            fi
            ;;
    esac
else
    echo "PiBlock status: stopped"
    if [ -f "$PID_FILE" ]; then
        echo "  stale pid file: $PID_FILE"
    fi
fi

# ------------------------------------------------------------------ settings
if [ -n "${VPY:-}" ] && [ -f "$CONFIG" ]; then
    "$VPY" - "$CONFIG" <<'PY' 2>/dev/null || true
import sys

try:
    import yaml
except Exception:
    sys.exit(0)

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}
except Exception:
    sys.exit(0)

if not isinstance(cfg, dict):
    sys.exit(0)

host = cfg.get("listen", "0.0.0.0")
port = cfg.get("port", 53)
upstreams = cfg.get("upstreams") or []
blocked = cfg.get("blocked_domains") or []
cache = cfg.get("cache") or {}
print(f"  config:    {sys.argv[1]}")
print(f"  listening: {host}:{port} (UDP + TCP)")
print("  upstreams: " + (", ".join(str(item) for item in upstreams) or "(none)"))
if blocked:
    print(f"  blocked ({len(blocked)}): " + ", ".join(str(item) for item in blocked))
else:
    print("  blocked:   (none configured)")
if isinstance(cache, dict):
    state = "enabled" if cache.get("enabled", True) else "disabled"
    print(f"  cache:     {state} (max {cache.get('max_entries', 4096)} entries, "
          f"TTL {cache.get('min_ttl', 1)}-{cache.get('max_ttl', 3600)}s)")
PY
fi

# ------------------------------------------------------------------ counters
if [ "$RUNNING" -eq 1 ]; then
    # Ask the server for a fresh snapshot (works when this script runs as the
    # same user; otherwise the periodic snapshot is shown).
    kill -USR1 "$PID" 2>/dev/null || true
    sleep 0.2
fi
if [ "$RUNNING" -eq 1 ] && [ -n "${VPY:-}" ] && [ -r "$STATS_FILE" ]; then
    "$VPY" - "$STATS_FILE" <<'PY' 2>/dev/null || true
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        stats = json.load(handle)
except Exception:
    sys.exit(0)

print(
    "  counters:  "
    f"queries={stats.get('queries', 0)} "
    f"blocked={stats.get('blocked', 0)} "
    f"cached={stats.get('cached', 0)} "
    f"upstream_errors={stats.get('upstream_errors', 0)} "
    f"refused={stats.get('refused', 0)}"
)
PY
fi

# ------------------------------------------------------------------ blocked log
if [ -r "$BLOCKED_LOG" ]; then
    TODAY="$(date +%Y-%m-%d)"
    COUNT="$(grep -c "^$TODAY" "$BLOCKED_LOG" 2>/dev/null || true)"
    echo "  blocked today: ${COUNT:-0}"
    LAST="$(tail -n 1 "$BLOCKED_LOG" 2>/dev/null || true)"
    if [ -n "$LAST" ]; then
        echo "  last blocked:  $LAST"
    fi
else
    echo "  blocked today: 0 (no blocked requests logged yet)"
fi

# ------------------------------------------------------------------ self-test
if [ "$RUNNING" -eq 1 ] && [ -n "${VPY:-}" ] && [ -f "$CONFIG" ]; then
    "$VPY" - "$CONFIG" <<'PY' 2>/dev/null || true
import sys
import time

try:
    import dns.message
    import dns.query
    import dns.rcode
    import yaml
except Exception:
    sys.exit(0)

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}
    blocked = cfg.get("blocked_domains") or []
    port = int(cfg.get("port", 53))
    listen = str(cfg.get("listen", "0.0.0.0"))
except Exception:
    sys.exit(0)

if not blocked:
    print("  self-test: skipped (no blocked domains configured)")
    sys.exit(0)

host = "127.0.0.1" if listen in ("0.0.0.0", "") else listen
name = str(blocked[0]).strip().lstrip("*.").rstrip(".")
query = dns.message.make_query(name, "A")
started = time.monotonic()
try:
    answer = dns.query.udp(query, host, port=port, timeout=3.0)
except Exception as exc:
    print(f"  self-test: FAILED - no answer from {host}:{port} ({exc})")
    sys.exit(0)

elapsed = (time.monotonic() - started) * 1000.0
if answer.rcode() == dns.rcode.NXDOMAIN:
    print(f"  self-test: OK - {name} answered NXDOMAIN in {elapsed:.0f} ms")
else:
    print(
        f"  self-test: FAILED - {name} answered "
        f"{dns.rcode.to_text(answer.rcode())}, expected NXDOMAIN"
    )
PY
fi

echo "  logs:      $LOG_DIR"
if [ "$RUNNING" -eq 1 ]; then
    exit 0
fi
exit 3
