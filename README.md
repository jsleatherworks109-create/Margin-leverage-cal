# PiBlock — Raspberry Pi DNS Blacklist

PiBlock turns a Raspberry Pi into a tiny, LAN-only DNS server that blocks a
specific list of domains — and every subdomain of them — by answering
**NXDOMAIN** for those names, while forwarding every other query to a normal
upstream resolver.

It is a single Python file with two dependencies, has no web interface and no
database, and idles at roughly 25–40 MB of RAM.

```
Windows PC ──DNS──▶ Raspberry Pi (PiBlock :53) ──▶ 1.1.1.1 / 8.8.8.8
                          │
                          ├─ example.com        ──▶ NXDOMAIN  (blocked)
                          ├─ www.example.com    ──▶ NXDOMAIN  (subdomain)
                          └─ everything-else    ──▶ normal answer
```

Nothing is redirected: blocked names simply do not resolve. No browser
extension or software is installed on Windows — the Pi is just its DNS server.

---

## Features

- Blocks a configurable list of domains **and all of their subdomains**
- Answers **NXDOMAIN** for blocked names (no redirect, no fake IP address)
- Forwards every other query to one or more upstream DNS servers, with failover
- Lightweight in-memory DNS cache with TTL expiry
- UDP **and** TCP DNS on port 53
- Blocked requests logged with timestamps and client IP (`logs/blocked.log`)
- Refuses clients outside `allow_networks` (LAN-only, not an open resolver)
- Validates the whole config on startup with clear error messages
- Three commands: `./start.sh`, `./stop.sh`, `./status.sh`
- Optional Docker Compose deployment
- Runs on Raspberry Pi OS (Bullseye/Bookworm), 32- or 64-bit

---

## Requirements

- Raspberry Pi (any model) with Raspberry Pi OS, network access
- Python **3.9+** (already installed on Raspberry Pi OS)
- `python3-venv` (the installer installs it if missing)
- A free port 53 on the Pi
- A **static IP address** for the Pi (router DHCP reservation is easiest)

---

## Installation

```bash
git clone <your-repository-url> piblock
cd piblock
nano config.yaml          # put your domains in blocked_domains
./start.sh                # asks for your sudo password (port 53)
```

`start.sh` does everything automatically: creates `.venv`, installs the two
Python dependencies, validates `config.yaml`, checks that port 53 is free,
starts PiBlock in the background and waits until it is answering.

Check it:

```bash
./status.sh
```

```
PiBlock status: running (pid 1834)
  uptime:    0m 12s
  config:    /home/pi/piblock/config.yaml
  listening: 0.0.0.0:53 (UDP + TCP)
  upstreams: 1.1.1.1, 8.8.8.8
  blocked (1): example.com
  cache:     enabled (max 4096 entries, TTL 1-3600s)
  counters:  queries=4 blocked=1 cached=0 upstream_errors=0 refused=0
  blocked today: 1
  last blocked:  2026-11-18T22:31:07+01:00 BLOCKED client=192.168.1.23 qname=example.com qtype=A proto=udp
  self-test: OK - example.com answered NXDOMAIN in 1 ms
  logs:      /home/pi/piblock/logs
```

Stop it:

```bash
./stop.sh
```

If the Pi is unavailable or you want to check from another machine:

```bash
dig @192.168.1.50 example.com          # -> status: NXDOMAIN
dig @192.168.1.50 www.example.com      # -> status: NXDOMAIN
dig @192.168.1.50 cloudflare.com       # -> normal A record
dig +tcp @192.168.1.50 example.com     # TCP works too
```

`dig` is not installed on Windows; use `nslookup example.com 192.168.1.50`
there instead.

---

## Configuration

Everything lives in `config.yaml`; the source code never needs editing.
After changing it, restart PiBlock:

```bash
./stop.sh && ./start.sh
```

### Block domains

```yaml
blocked_domains:
  - example.com
  - ads.example.net
```

- `example.com` blocks `example.com`, `www.example.com`, `api.eu.example.com`, …
- Add as many entries as you need, one per line.
- Use a **bare domain**: no `https://`, no path, no `*.` (subdomains are always
  included), no IP addresses.
- Matching is case-insensitive and works for every query type (A, AAAA, HTTPS,
  MX, …).
- `example.com.evil.net` is **not** blocked by `example.com` — only real
  subdomains are.

### Settings reference

| Setting | Default | Meaning |
| --- | --- | --- |
| `listen` | `0.0.0.0` | IP address to bind. `0.0.0.0` = all interfaces |
| `port` | `53` | DNS port |
| `upstreams` | `1.1.1.1`, `8.8.8.8` | Numeric IPs (`IP` or `IP:port`), tried in order |
| `blocked_domains` | `example.com` | Domains to answer with NXDOMAIN |
| `allow_networks` | LAN + loopback ranges | Client networks allowed to use the server |
| `cache.enabled` | `true` | Turn the response cache on/off |
| `cache.max_entries` | `4096` | Maximum cached responses |
| `cache.min_ttl` | `1` | Do not cache responses with a TTL below this |
| `cache.max_ttl` | `3600` | Never cache a response longer than this |
| `query_timeout` | `2.0` | Seconds before trying the next upstream |
| `blocked_ttl` | `60` | Negative-cache TTL sent with NXDOMAIN replies |
| `log_queries` | `false` | Log every forwarded query (noisy, useful for testing) |
| `log_file` | `logs/piblock.log` | Server log (rotated) |
| `blocked_log` | `logs/blocked.log` | One line per blocked request (rotated) |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Relative log paths are resolved next to `config.yaml`, so the working directory
does not matter.

Run a validation without starting anything:

```bash
.venv/bin/python main.py --check-config
```

Invalid YAML, unknown keys, malformed domains, bad CIDR ranges, bad upstream
addresses and unwritable log directories are all reported with the exact
setting name and exit code 2 — nothing starts with a broken config.

---

## Setting Windows to use the Pi as its DNS server

The Pi must have a fixed LAN IP (for example `192.168.1.50`). Use a DHCP
reservation in your router, or set a static IP on the Pi.

### Windows 11

1. **Settings → Network & Internet → Ethernet** (or **Wi-Fi**) → your network.
2. Under **DNS server assignment**, click **Edit**.
3. Set **IPv4** to **On**, **Preferred DNS** to the Pi's IP (e.g. `192.168.1.50`).
4. Leave **Alternate DNS blank** — a secondary server lets Windows bypass the
   block when the Pi is slow to answer.
5. **Save**, then open Command Prompt and run:

```bat
ipconfig /flushdns
nslookup example.com
nslookup cloudflare.com
```

`nslookup example.com` should answer **Non-existent domain** and print the Pi's
IP as the server; `nslookup cloudflare.com` should resolve normally.

### Windows 10

1. **Settings → Network & Internet → Status → Change adapter options**.
2. Right-click your adapter → **Properties** → **Internet Protocol Version 4
   (TCP/IPv4)** → **Properties**.
3. Choose **Use the following DNS server addresses**:
   - Preferred DNS server: `192.168.1.50`
   - Alternate DNS server: *leave empty*
4. OK → Close, then run `ipconfig /flushdns` in Command Prompt.

### Notes

- **Remove any other DNS settings** (a second adapter, a VPN client, or a
  manually set alternate server) or they may win over the Pi.
- Browsers with **Secure DNS / DNS-over-HTTPS** enabled (Chrome, Edge, Firefox)
  can bypass the system DNS server. If a blocked site still loads, open the
  browser settings and turn **Use secure DNS** off, or use a browser without
  DoH while testing.
- Windows caches answers. After changing `config.yaml`, restart PiBlock and run
  `ipconfig /flushdns` (or wait for `blocked_ttl` / the cached TTL to expire).
- To undo everything, set the adapter back to **Obtain DNS server address
  automatically**.

---

## Security, firewall and router

PiBlock is a LAN-only resolver by design:

- By default it only answers clients from private/loopback networks
  (`allow_networks`). Everything else gets `REFUSED` and a rate-limited warning.
- It never modifies router settings and never intercepts traffic: it only
  answers queries sent to it.
- Do **not** forward port 53 (UDP or TCP) on your router. There is no reason for
  the internet to reach it, and an exposed resolver can be abused for DNS
  amplification attacks.
- Keep `allow_networks` as narrow as practical (e.g. only `192.168.1.0/24`).

### Firewall (optional but recommended)

On Raspberry Pi OS with `ufw`:

```bash
sudo apt update && sudo apt install -y ufw
sudo ufw allow from 192.168.0.0/16 to any port 53 proto udp
sudo ufw allow from 192.168.0.0/16 to any port 53 proto tcp
sudo ufw allow 22/tcp          # keep SSH access
sudo ufw enable
```

Replace `192.168.0.0/16` with your actual LAN subnet if it is narrower. If you
use `nftables`/`iptables` directly, allow UDP and TCP 53 from your LAN subnet to
the Pi and drop it from everywhere else.

Do not add a port-forward, DMZ entry or "exposed host" rule for the Pi.

---

## Optional: Docker Compose

Docker is handy if you prefer containers over the venv, or run other services on
the Pi.

```bash
nano config.yaml             # same config file
mkdir -p logs
docker compose up -d --build
docker compose logs -f piblock
```

Stop / remove:

```bash
docker compose down
```

Notes:

- The container publishes UDP and TCP port 53 and runs as root inside the
  container (required to bind port 53). PiBlock itself still enforces
  `allow_networks`.
- `./config.yaml` is mounted read-only and `./logs` holds logs and stats, so the
  three scripts and Docker share the same config format.
- Port 53 on the host must be free; see the troubleshooting section below.
- On Linux, add `network_mode: host` (and delete the `ports:` block) if you want
  the real client IPs in the logs instead of Docker's gateway address. The
  default `allow_networks` already includes Docker's `172.16.0.0/12` bridge, so
  both modes work.
- Use `./stop.sh` only for the non-Docker install. For Docker use
  `docker compose stop`.

---

## Run automatically at boot (optional)

The three scripts start and stop PiBlock, but they do not survive a reboot on
their own. To start PiBlock with the Pi, create a systemd unit:

```bash
sudo tee /etc/systemd/system/piblock.service >/dev/null <<'EOF'
[Unit]
Description=PiBlock DNS filter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/pi/piblock
ExecStart=/home/pi/piblock/.venv/bin/python /home/pi/piblock/main.py --config /home/pi/piblock/config.yaml
Restart=on-failure
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now piblock
```

`sudo systemctl status piblock` and `journalctl -u piblock -f` then replace
`./status.sh`. Stop it with `sudo systemctl disable --now piblock` before using
`./start.sh` again (both would compete for port 53).

---

## Commands

| Command | What it does |
| --- | --- |
| `./start.sh` | Creates the venv if needed, installs deps, validates config, checks port 53, starts in the background, waits until ready |
| `./stop.sh` | Graceful shutdown (SIGTERM, then SIGKILL after 10 s) |
| `./status.sh` | State, uptime, config summary, counters, today's block count, live self-test. Exit 0 if running, 3 if stopped |
| `.venv/bin/python main.py --check-config` | Validate the config only |
| `.venv/bin/python main.py --health` | Query the running instance and exit 0/1 |
| `.venv/bin/python main.py --config config.yaml` | Run in the foreground (useful for debugging) |

Logs:

- `logs/piblock.log` — startup, upstream failures, refused clients, blocked
  events (rotated at 2 MB, 3 backups)
- `logs/blocked.log` — one line per blocked request, timestamped (rotated at
  1 MB, 5 backups)
- `logs/piblock.out` — stdout/stderr of the background process
- `logs/stats.json` — counters refreshed every 15 seconds, and on demand when
  `status.sh` runs

---

## Troubleshooting

### Port 53 is already in use

`start.sh` checks this before launching and prints the hint. Something else is
already a DNS server on the Pi, usually `systemd-resolved`, `dnsmasq`, Pi-hole or
AdGuard Home.

```bash
sudo ss -lunpt | grep ':53'
```

For `systemd-resolved` on Raspberry Pi OS:

```bash
sudo systemctl disable --now systemd-resolved
```

Then make sure the Pi itself still has a working resolver for its own lookups:

```bash
echo 'nameserver 1.1.1.1' | sudo tee /etc/resolv.conf
```

For Pi-hole/AdGuard Home, stop that service or run PiBlock on another machine —
two DNS servers cannot share port 53.

### `sudo: command not found` / permission denied for port 53

Use `sudo ./start.sh` and `sudo ./stop.sh`. Ports below 1024 require root. You
can avoid sudo by using a port above 1023 (e.g. `port: 5353`) and pointing
Windows at `192.168.1.50` — but Windows does not let you change the DNS port, so
port 53 is required for the intended setup.

### The blocked site still loads

1. Check PiBlock first, bypassing Windows:
   `nslookup example.com 192.168.1.50` must return **Non-existent domain**.
2. If it does, Windows is using another DNS server (alternate DNS, VPN, another
   adapter) or a cached answer: `ipconfig /flushdns`.
3. If the browser uses Secure DNS (DoH), disable it or the browser ignores the
   system resolver.
4. If `nslookup` returns a real address, check `./status.sh` and
   `grep BLOCKED logs/blocked.log`; the query probably never reached the Pi.

### Upstream failures / everything is SERVFAIL

The Pi cannot reach 1.1.1.1 / 8.8.8.8. Try other upstream IPs in `config.yaml`
(e.g. your router, `9.9.9.9`, `8.8.4.4`), check the Pi's internet access and the
firewall. PiBlock returns SERVFAIL (not NXDOMAIN) for upstream failures so real
outages are never confused with blocking.

### Python dependency install fails

Raspberry Pi OS needs `python3-venv` and `python3-pip`:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
```

If PyYAML has no prebuilt wheel on your architecture, install build tools once:

```bash
sudo apt install -y build-essential python3-dev libyaml-dev
```

### Start says "did not become ready"

Read the tail of `logs/piblock.out` (start.sh prints it) or run in the
foreground to see the full message:

```bash
.venv/bin/python main.py --config config.yaml
```

---

## How it works

1. A query arrives over UDP or TCP. Clients outside `allow_networks` are refused.
2. The query name is compared against `blocked_domains` at label boundaries, so
   `example.com` matches `example.com` and `*.example.com` but not
   `notexample.com`. A match returns an NXDOMAIN response with a SOA record so
   clients cache the negative answer briefly.
3. Otherwise the response cache is checked (keyed by name, type, class and EDNS
   DO bit; TTLs are decremented as the entry ages).
4. On a miss the query is forwarded to the upstreams (UDP, with automatic TCP
   retry if the answer is truncated), tried in order until one succeeds.
5. Successful answers are cached for their TTL (clamped by `cache.max_ttl`) and
   returned to the client, truncated properly if the UDP payload limit is
   exceeded.
6. Blocked events are appended to `logs/blocked.log`, counters are written to
   `logs/stats.json`, and SIGTERM/SIGINT shut everything down gracefully.

### Performance

- Single asyncio process: no per-query threads, no subprocesses.
- Cache hits are served from memory without touching the network.
- Typical idle footprint on a Pi: ~25–40 MB RSS, effectively 0% CPU.
- Upstream queries are capped at 64 concurrent; excess UDP work is dropped
  rather than queued without bound.

---

## Project layout

```
piblock/
├── main.py               # the DNS server (config validation, blocking, cache, logging)
├── config.yaml           # blocked domains and all settings
├── requirements.txt      # dnspython, PyYAML
├── start.sh              # bootstrap + start in the background
├── stop.sh               # graceful stop
├── status.sh             # status, counters, self-test
├── Dockerfile            # container image
├── docker-compose.yml    # optional Compose setup
├── .gitignore
└── README.md
```

Generated at runtime (ignored by git): `.venv/`, `logs/`, `piblock.pid`.
