#!/usr/bin/env python3
"""PiBlock - a tiny DNS blackhole for the Raspberry Pi.

PiBlock answers DNS queries normally, except for the domains listed in
``blocked_domains`` (and all of their subdomains), which are answered with
NXDOMAIN.  It is deliberately small: one process, one YAML file, no database
and no web interface.

    python3 main.py --config config.yaml        # run in the foreground
    python3 main.py --check-config              # validate config and exit
    python3 main.py --health                    # local health check
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import ipaddress
import json
import logging
import logging.handlers
import os
import signal
import struct
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

import dns.asyncquery
import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.opcode
import dns.query
import dns.rcode
import dns.rdatatype
import dns.rrset
import yaml

APP_NAME = "PiBlock"
VERSION = "1.0.0"

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.yaml"

# Never advertise more than 1232 bytes over UDP (DNS Flag Day 2020).
MAX_UDP_PAYLOAD = 1232
MAX_INFLIGHT = 256
MAX_UPSTREAM_CONCURRENCY = 64
TCP_IDLE_TIMEOUT = 10.0
STATS_INTERVAL = 15.0
STATS_MIN_WRITE_GAP = 2.0
REFUSAL_LOG_INTERVAL = 10.0

DOMAIN_HINT = (
    'use a bare domain such as "example.com"; subdomains are blocked automatically'
)

DEFAULT_ALLOW_NETWORKS = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)

TOP_LEVEL_KEYS = {
    "listen",
    "port",
    "upstreams",
    "blocked_domains",
    "allow_networks",
    "query_timeout",
    "blocked_ttl",
    "log_queries",
    "log_file",
    "blocked_log",
    "log_level",
    "cache",
}

CACHE_KEYS = {"enabled", "max_entries", "min_ttl", "max_ttl"}

LOG = logging.getLogger("piblock")
BLOCKED_LOG = logging.getLogger("piblock.blocked")


class ConfigError(Exception):
    """Raised when config.yaml cannot be used."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class Upstream:
    host: str
    port: int = 53


@dataclass(frozen=True)
class Config:
    path: Path
    listen: str
    port: int
    upstreams: Tuple[Upstream, ...]
    blocked: FrozenSet[str]
    allow_networks: Tuple[Any, ...]
    cache_enabled: bool
    cache_max_entries: int
    cache_min_ttl: int
    cache_max_ttl: int
    query_timeout: float
    blocked_ttl: int
    log_queries: bool
    log_file: Path
    blocked_log: Path
    log_level: str


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Configuration loading and validation
# ---------------------------------------------------------------------------


def _label(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _int_setting(
    raw: Dict[str, Any],
    key: str,
    default: int,
    where: str,
    errors: List[str],
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{_label(where, key)}: expected an integer, got {value!r}")
        return default
    if minimum is not None and value < minimum:
        errors.append(
            f"{_label(where, key)}: must be at least {minimum}, got {value}"
        )
        return default
    if maximum is not None and value > maximum:
        errors.append(f"{_label(where, key)}: must be at most {maximum}, got {value}")
        return default
    return value


def _float_setting(
    raw: Dict[str, Any],
    key: str,
    default: float,
    where: str,
    errors: List[str],
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{_label(where, key)}: expected a number, got {value!r}")
        return default
    number = float(value)
    if minimum is not None and number < minimum:
        errors.append(f"{_label(where, key)}: must be at least {minimum}, got {value}")
        return default
    if maximum is not None and number > maximum:
        errors.append(f"{_label(where, key)}: must be at most {maximum}, got {value}")
        return default
    return number


def _bool_setting(
    raw: Dict[str, Any], key: str, default: bool, where: str, errors: List[str]
) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        errors.append(
            f"{_label(where, key)}: expected true or false, got {value!r}"
        )
        return default
    return value


def _string_setting(
    raw: Dict[str, Any], key: str, default: str, where: str, errors: List[str]
) -> str:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        errors.append(
            f"{_label(where, key)}: expected a non-empty string, got {value!r}"
        )
        return default
    return value.strip()


def _path_setting(
    raw: Dict[str, Any],
    key: str,
    default: str,
    base_dir: Path,
    errors: List[str],
) -> Path:
    value = _string_setting(raw, key, default, "", errors)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        errors.append(f"{key}: cannot create directory {candidate.parent}: {exc}")
        return candidate
    if not os.access(candidate.parent, os.W_OK):
        errors.append(f"{key}: directory {candidate.parent} is not writable")
    return candidate


def _parse_domain(
    value: Any, where: str, errors: List[str], warnings: List[str]
) -> Optional[str]:
    if not isinstance(value, str):
        errors.append(
            f"{where}: expected a domain name string, got {value!r}"
        )
        return None
    text = value.strip().lower()
    if not text:
        errors.append(f"{where}: domain is empty")
        return None
    if text.startswith("*."):
        warnings.append(
            f"{where}: {text!r} - wildcards are unnecessary, subdomains are blocked automatically"
        )
        text = text[2:]
    if text.startswith("."):
        errors.append(f"{where}: {value!r} has a leading dot ({DOMAIN_HINT})")
        return None
    if any(char in text for char in "/:@ \t\r\n?#"):
        errors.append(f"{where}: {value!r} is not a domain name ({DOMAIN_HINT})")
        return None
    try:
        name = dns.name.from_text(text)
    except (dns.exception.SyntaxError, dns.name.EmptyLabel, ValueError) as exc:
        errors.append(f"{where}: {value!r} is not a valid domain name ({exc})")
        return None
    if name == dns.name.root:
        errors.append(f"{where}: the DNS root cannot be blocked")
        return None
    try:
        ipaddress.ip_address(text)
    except ValueError:
        pass
    else:
        errors.append(f"{where}: {value!r} is an IP address, not a domain name")
        return None
    domain = name.to_text(omit_final_dot=True).lower()
    if "." not in domain:
        warnings.append(
            f"{where}: {domain!r} is a top-level domain; blocking it blocks a huge part of the internet"
        )
    return domain


def _parse_upstream(value: Any, where: str, errors: List[str]) -> Optional[Upstream]:
    if not isinstance(value, str):
        errors.append(f"{where}: expected an IP address string, got {value!r}")
        return None
    text = value.strip()
    host = text
    port = 53
    try:
        if text.startswith("["):
            end = text.index("]")
            host = text[1:end]
            rest = text[end + 1 :]
            if rest:
                if not rest.startswith(":"):
                    raise ValueError("unexpected text after the address")
                port = int(rest[1:])
        elif text.count(":") == 1:
            host, port_text = text.split(":", 1)
            port = int(port_text)
        address = ipaddress.ip_address(host)
    except (ValueError, IndexError) as exc:
        errors.append(
            f"{where}: {value!r} is not an IP address or IP:port ({exc}); "
            "use numeric IPs so PiBlock never depends on DNS to find its upstream"
        )
        return None
    if not 1 <= port <= 65535:
        errors.append(f"{where}: port must be between 1 and 65535, got {port}")
        return None
    return Upstream(str(address), port)


def load_config(path: Path) -> Config:
    """Load and validate config.yaml.  Raises ConfigError on any problem."""
    errors: List[str] = []
    warnings: List[str] = []

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError([f"config file not found: {path}"]) from None
    except OSError as exc:
        raise ConfigError([f"could not read {path}: {exc}"]) from None

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError([f"invalid YAML in {path}:\n{exc}"]) from None

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(
            ["the config file must contain a YAML mapping (key: value pairs)"]
        )

    for key in sorted(raw, key=str):
        if key not in TOP_LEVEL_KEYS:
            errors.append(
                f"unknown setting {key!r} (allowed: {', '.join(sorted(TOP_LEVEL_KEYS))})"
            )

    listen_raw = raw.get("listen", "0.0.0.0")
    listen = "0.0.0.0"
    if isinstance(listen_raw, str) and listen_raw.strip():
        try:
            listen = str(ipaddress.ip_address(listen_raw.strip()))
        except ValueError:
            errors.append(
                f"listen: {listen_raw!r} is not a valid IP address "
                "(use 0.0.0.0 for all interfaces or the Pi's LAN address)"
            )
    else:
        errors.append(f"listen: expected an IP address string, got {listen_raw!r}")

    port = _int_setting(raw, "port", 53, "", errors, minimum=1, maximum=65535)
    query_timeout = _float_setting(
        raw, "query_timeout", 2.0, "", errors, minimum=0.2, maximum=30.0
    )
    blocked_ttl = _int_setting(
        raw, "blocked_ttl", 60, "", errors, minimum=0, maximum=86400
    )
    log_queries = _bool_setting(raw, "log_queries", False, "", errors)

    log_level = _string_setting(raw, "log_level", "INFO", "", errors).upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        errors.append(
            f"log_level: {log_level!r} is not a log level "
            "(use DEBUG, INFO, WARNING or ERROR)"
        )
        log_level = "INFO"

    upstreams_raw = raw.get("upstreams", ["1.1.1.1", "8.8.8.8"])
    if not isinstance(upstreams_raw, list) or not upstreams_raw:
        errors.append(
            "upstreams: expected a non-empty list of IP addresses, e.g. [1.1.1.1, 8.8.8.8]"
        )
        upstreams_raw = []
    upstreams: List[Upstream] = []
    for index, item in enumerate(upstreams_raw):
        upstream = _parse_upstream(item, f"upstreams[{index}]", errors)
        if upstream is None:
            continue
        if upstream in upstreams:
            warnings.append(f"upstreams: {item!r} is listed more than once")
            continue
        upstreams.append(upstream)
    if not upstreams and not errors:
        errors.append("upstreams: at least one upstream DNS server is required")

    blocked_raw = raw.get("blocked_domains", [])
    if blocked_raw is None:
        blocked_raw = []
    if not isinstance(blocked_raw, list):
        errors.append(
            f"blocked_domains: expected a list of domains, got {blocked_raw!r}"
        )
        blocked_raw = []
    blocked: List[str] = []
    for index, item in enumerate(blocked_raw):
        domain = _parse_domain(item, f"blocked_domains[{index}]", errors, warnings)
        if domain is None:
            continue
        if domain in blocked:
            warnings.append(f"blocked_domains: {domain} is listed more than once")
            continue
        blocked.append(domain)
    if not blocked_raw:
        warnings.append(
            "no domains are blocked; PiBlock will only forward DNS until you add one"
        )

    cache_raw = raw.get("cache", {})
    if cache_raw is None:
        cache_raw = {}
    if not isinstance(cache_raw, dict):
        errors.append("cache: expected a mapping with enabled/max_entries/min_ttl/max_ttl")
        cache_raw = {}
    for key in sorted(cache_raw, key=str):
        if key not in CACHE_KEYS:
            errors.append(f"unknown setting cache.{key}")
    cache_enabled = _bool_setting(cache_raw, "enabled", True, "cache", errors)
    cache_max_entries = _int_setting(
        cache_raw, "max_entries", 4096, "cache", errors, minimum=0, maximum=1_000_000
    )
    cache_min_ttl = _int_setting(
        cache_raw, "min_ttl", 1, "cache", errors, minimum=0, maximum=604800
    )
    cache_max_ttl = _int_setting(
        cache_raw, "max_ttl", 3600, "cache", errors, minimum=1, maximum=604800
    )
    if cache_min_ttl > cache_max_ttl:
        errors.append("cache.min_ttl must not be greater than cache.max_ttl")

    allow_raw = raw.get("allow_networks", list(DEFAULT_ALLOW_NETWORKS))
    if not isinstance(allow_raw, list) or not allow_raw:
        errors.append(
            "allow_networks: expected a non-empty list of CIDR ranges "
            "(e.g. 192.168.0.0/16)"
        )
        allow_raw = []
    networks: List[Any] = []
    for index, item in enumerate(allow_raw):
        if not isinstance(item, str):
            errors.append(
                f"allow_networks[{index}]: expected a CIDR string, got {item!r}"
            )
            continue
        try:
            network = ipaddress.ip_network(item.strip(), strict=False)
        except ValueError as exc:
            errors.append(
                f"allow_networks[{index}]: {item!r} is not a valid network ({exc}); "
                "use CIDR notation such as 192.168.0.0/16"
            )
            continue
        if network.prefixlen == 0:
            warnings.append(
                f"allow_networks includes {network}: PiBlock would answer any client "
                "that can reach it, including the internet if port 53 is forwarded"
            )
        networks.append(network)
    if not networks and not errors:
        errors.append(
            "allow_networks: no valid networks; PiBlock would refuse every client"
        )

    log_file = _path_setting(raw, "log_file", "logs/piblock.log", path.parent, errors)
    blocked_log = _path_setting(
        raw, "blocked_log", "logs/blocked.log", path.parent, errors
    )
    if log_file == blocked_log:
        errors.append("log_file and blocked_log must be different files")

    for warning in warnings:
        print(f"{APP_NAME}: warning: {warning}", file=sys.stderr)

    if errors:
        raise ConfigError(errors)

    return Config(
        path=path,
        listen=listen,
        port=port,
        upstreams=tuple(upstreams),
        blocked=frozenset(blocked),
        allow_networks=tuple(networks),
        cache_enabled=cache_enabled,
        cache_max_entries=cache_max_entries,
        cache_min_ttl=cache_min_ttl,
        cache_max_ttl=cache_max_ttl,
        query_timeout=query_timeout,
        blocked_ttl=blocked_ttl,
        log_queries=log_queries,
        log_file=log_file,
        blocked_log=blocked_log,
        log_level=log_level,
    )


def describe_config(cfg: Config) -> str:
    upstreams = ", ".join(
        f"{item.host}:{item.port}" if item.port != 53 else item.host
        for item in cfg.upstreams
    )
    blocked = ", ".join(sorted(cfg.blocked)) if cfg.blocked else "(none)"
    return "\n".join(
        (
            f"config file:      {cfg.path}",
            f"listen address:   {cfg.listen}:{cfg.port} (UDP + TCP)",
            f"upstreams:        {upstreams}",
            f"blocked domains:  {blocked}",
            "cache:            "
            + (
                f"enabled (max {cfg.cache_max_entries} entries, "
                f"TTL {cfg.cache_min_ttl}-{cfg.cache_max_ttl}s)"
                if cfg.cache_enabled
                else "disabled"
            ),
            "allowed networks: "
            + ", ".join(str(network) for network in cfg.allow_networks),
            f"blocked log:      {cfg.blocked_log}",
            f"query log:        {cfg.log_file}",
        )
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(cfg: Config) -> None:
    level = getattr(logging, cfg.log_level, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            cfg.log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        print(
            f"{APP_NAME}: warning: could not open log file {cfg.log_file}: {exc}",
            file=sys.stderr,
        )

    BLOCKED_LOG.setLevel(logging.INFO)
    BLOCKED_LOG.propagate = False
    for handler in list(BLOCKED_LOG.handlers):
        BLOCKED_LOG.removeHandler(handler)
    try:
        blocked_handler = logging.handlers.RotatingFileHandler(
            cfg.blocked_log, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
        blocked_handler.setFormatter(logging.Formatter("%(message)s"))
        BLOCKED_LOG.addHandler(blocked_handler)
    except OSError as exc:
        print(
            f"{APP_NAME}: warning: could not open blocked log {cfg.blocked_log}: {exc}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class CacheEntry:
    __slots__ = ("message", "stored", "ttl")

    def __init__(self, message: dns.message.Message, stored: float, ttl: int) -> None:
        self.message = message
        self.stored = stored
        self.ttl = ttl


class DnsCache:
    """Small LRU cache of complete DNS responses."""

    def __init__(self, max_entries: int) -> None:
        self._entries: "OrderedDict[Tuple[Any, ...], CacheEntry]" = OrderedDict()
        self._max_entries = max(1, max_entries)

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: Tuple[Any, ...]) -> Optional[Tuple[CacheEntry, int]]:
        entry = self._entries.get(key)
        if entry is None:
            return None
        elapsed = time.monotonic() - entry.stored
        if elapsed >= entry.ttl:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry, int(elapsed)

    def put(
        self, key: Tuple[Any, ...], message: dns.message.Message, ttl: int
    ) -> None:
        self._entries[key] = CacheEntry(message, time.monotonic(), max(1, int(ttl)))
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def remove(self, key: Tuple[Any, ...]) -> None:
        self._entries.pop(key, None)


def response_ttl(message: dns.message.Message) -> Optional[int]:
    """Return the smallest TTL in the answer/authority sections."""
    ttls: List[int] = []
    for section in (message.answer, message.authority):
        for rrset in section:
            if rrset.rdtype == dns.rdatatype.OPT:
                continue
            ttls.append(int(rrset.ttl))
    if not ttls:
        return None
    return max(0, min(ttls))


def udp_limit(query: dns.message.Message) -> int:
    if query.edns < 0:
        return 512
    try:
        size = int(query.payload)
    except (TypeError, ValueError):
        size = 512
    return max(512, min(size, MAX_UDP_PAYLOAD))


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class UpstreamError(Exception):
    pass


class PiBlockServer:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cache = DnsCache(cfg.cache_max_entries) if cfg.cache_enabled else None
        self.stats: Dict[str, Any] = {
            "queries": 0,
            "blocked": 0,
            "cached": 0,
            "upstream_errors": 0,
            "refused": 0,
            "malformed": 0,
            "dropped": 0,
        }
        self._tasks: set = set()
        self._upstream_slots = asyncio.Semaphore(MAX_UPSTREAM_CONCURRENCY)
        self._udp_transport = None
        self._tcp_server = None
        self._stats_task: Optional[asyncio.Task] = None
        self._stats_path = cfg.log_file.parent / "stats.json"
        self._started_mono = time.monotonic()
        self._started_iso = now_iso()
        self._last_stats_write = 0.0
        self._last_refusal_log = 0.0
        self._suppressed_refusals = 0

    # -- helpers ---------------------------------------------------------

    def is_blocked(self, qname: str) -> bool:
        name = qname
        while name:
            if name in self.cfg.blocked:
                return True
            _, _, name = name.partition(".")
        return False

    def allows(self, address: Any) -> bool:
        try:
            if address.version == 6 and address.ipv4_mapped is not None:
                address = address.ipv4_mapped
        except AttributeError:
            pass
        for network in self.cfg.allow_networks:
            if address.version == network.version and address in network:
                return True
        return False

    def _log_refusal(self, client_ip: str) -> None:
        now = time.monotonic()
        if now - self._last_refusal_log >= REFUSAL_LOG_INTERVAL:
            if self._suppressed_refusals:
                LOG.warning(
                    "refused %d more request(s) from clients outside allow_networks",
                    self._suppressed_refusals,
                )
                self._suppressed_refusals = 0
            LOG.warning(
                "refused request from %s: not in allow_networks", client_ip
            )
            self._last_refusal_log = now
        else:
            self._suppressed_refusals += 1

    # -- DNS responses ---------------------------------------------------

    def _apply_edns(
        self, response: dns.message.Message, query: dns.message.Message
    ) -> None:
        if query.edns < 0:
            return
        try:
            response.use_edns(
                edns=0,
                ednsflags=int(query.ednsflags) & dns.flags.DO,
                payload=MAX_UDP_PAYLOAD,
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOG.debug("could not apply EDNS to response: %s", exc)

    def _serialize(
        self, response: dns.message.Message, query: dns.message.Message, proto: str
    ) -> bytes:
        if proto == "tcp":
            return response.to_wire()
        limit = udp_limit(query)
        try:
            return response.to_wire(max_size=limit, prefer_truncation=True)
        except dns.exception.TooBig:
            pass
        except TypeError:
            # dnspython < 2.6 has no prefer_truncation keyword
            try:
                return response.to_wire(max_size=limit)
            except dns.exception.TooBig:
                pass
        truncated = dns.message.make_response(query, recursion_available=True)
        truncated.flags |= dns.flags.TC
        try:
            truncated.set_rcode(response.rcode())
        except Exception:  # pragma: no cover - defensive
            pass
        return truncated.to_wire(max_size=limit)

    def _blocked_response(
        self, query: dns.message.Message, proto: str
    ) -> bytes:
        response = dns.message.make_response(query, recursion_available=True)
        response.set_rcode(dns.rcode.NXDOMAIN)
        self._apply_edns(response, query)
        ttl = self.cfg.blocked_ttl
        try:
            soa = dns.rrset.from_text(
                dns.name.from_text("piblock.local."),
                ttl,
                "IN",
                "SOA",
                f"ns.piblock.local. hostmaster.piblock.local. 1 3600 600 86400 {ttl}",
            )
            response.authority.append(soa)
        except Exception as exc:  # pragma: no cover - defensive
            LOG.debug("could not build negative SOA: %s", exc)
        return self._serialize(response, query, proto)

    def _error_response(
        self, query: dns.message.Message, rcode: Any, proto: str
    ) -> bytes:
        response = dns.message.make_response(query, recursion_available=True)
        response.set_rcode(rcode)
        self._apply_edns(response, query)
        return self._serialize(response, query, proto)

    # -- upstream --------------------------------------------------------

    async def _forward(
        self, query: dns.message.Message, raw: bytes
    ) -> dns.message.Message:
        outgoing = dns.message.from_wire(raw)
        if not outgoing.flags & dns.flags.RD:
            outgoing.flags |= dns.flags.RD
        last_error = "no upstream DNS servers configured"
        for upstream in self.cfg.upstreams:
            try:
                async with self._upstream_slots:
                    response = await asyncio.wait_for(
                        dns.asyncquery.udp(
                            outgoing,
                            upstream.host,
                            timeout=self.cfg.query_timeout,
                            port=upstream.port,
                            ignore_unexpected=True,
                        ),
                        timeout=self.cfg.query_timeout + 1.0,
                    )
                    if response.flags & dns.flags.TC:
                        response = await asyncio.wait_for(
                            dns.asyncquery.tcp(
                                outgoing,
                                upstream.host,
                                timeout=self.cfg.query_timeout,
                                port=upstream.port,
                            ),
                            timeout=self.cfg.query_timeout + 1.0,
                        )
            except (dns.exception.DNSException, asyncio.TimeoutError, OSError) as exc:
                last_error = f"{upstream.host}:{upstream.port}: {exc}"
                LOG.warning(
                    "upstream %s:%s failed: %s", upstream.host, upstream.port, exc
                )
                continue
            return response
        raise UpstreamError(last_error)

    # -- request handling ------------------------------------------------

    async def handle_packet(
        self, data: bytes, peer: Tuple[Any, ...], proto: str
    ) -> Optional[bytes]:
        try:
            return await self._handle_packet(data, peer, proto)
        finally:
            self._maybe_write_stats()

    async def _handle_packet(
        self, data: bytes, peer: Tuple[Any, ...], proto: str
    ) -> Optional[bytes]:
        client_ip = str(peer[0]) if peer else "unknown"
        if len(data) < 12:
            self.stats["malformed"] += 1
            return None
        try:
            query = dns.message.from_wire(data)
        except Exception as exc:
            self.stats["malformed"] += 1
            LOG.debug("dropping malformed %s packet from %s: %s", proto, client_ip, exc)
            return None

        try:
            address = ipaddress.ip_address(client_ip.split("%")[0])
        except ValueError:
            return None

        if not self.allows(address):
            self.stats["refused"] += 1
            self._log_refusal(client_ip)
            try:
                return self._error_response(query, dns.rcode.REFUSED, proto)
            except Exception:
                return None

        if query.flags & dns.flags.QR:
            return None
        if query.opcode() != dns.opcode.QUERY:
            try:
                return self._error_response(query, dns.rcode.NOTIMP, proto)
            except Exception:
                return None
        if len(query.question) != 1:
            try:
                return self._error_response(query, dns.rcode.FORMERR, proto)
            except Exception:
                return None

        question = query.question[0]
        qname = question.name.to_text(omit_final_dot=True).lower()
        qtype = int(question.rdtype)
        qclass = int(question.rdclass)
        qtype_text = dns.rdatatype.to_text(qtype)
        self.stats["queries"] += 1

        if self.cfg.log_queries:
            LOG.info("query %s %s from %s (%s)", qname, qtype_text, client_ip, proto)

        if self.is_blocked(qname):
            self.stats["blocked"] += 1
            BLOCKED_LOG.info(
                "%s BLOCKED client=%s qname=%s qtype=%s proto=%s",
                now_iso(),
                client_ip,
                qname,
                qtype_text,
                proto,
            )
            LOG.info("blocked %s (%s) for %s", qname, qtype_text, client_ip)
            return self._blocked_response(query, proto)

        cache_key = (
            qname,
            qtype,
            qclass,
            query.edns >= 0,
            bool(query.ednsflags & dns.flags.DO),
        )
        if self.cache is not None:
            hit = self.cache.get(cache_key)
            if hit is not None:
                entry, elapsed = hit
                self.stats["cached"] += 1
                try:
                    return self._cached_wire(entry, elapsed, query, proto)
                except Exception as exc:
                    LOG.warning("dropping unusable cache entry for %s: %s", qname, exc)
                    self.cache.remove(cache_key)

        try:
            response = await self._forward(query, data)
        except UpstreamError as exc:
            self.stats["upstream_errors"] += 1
            LOG.warning("could not resolve %s: %s", qname, exc)
            return self._error_response(query, dns.rcode.SERVFAIL, proto)

        wire = self._serialize(response, query, proto)
        self._store_response(cache_key, response)
        return wire

    def _cached_wire(
        self,
        entry: CacheEntry,
        elapsed: int,
        query: dns.message.Message,
        proto: str,
    ) -> bytes:
        message = entry.message
        saved_id = message.id
        saved_ttls: List[Tuple[Any, int]] = []
        message.id = query.id
        try:
            for section in (message.answer, message.authority, message.additional):
                for rrset in section:
                    if rrset.rdtype == dns.rdatatype.OPT:
                        continue
                    saved_ttls.append((rrset, int(rrset.ttl)))
                    rrset.ttl = max(0, int(rrset.ttl) - elapsed)
            return self._serialize(message, query, proto)
        finally:
            for rrset, ttl in saved_ttls:
                rrset.ttl = ttl
            message.id = saved_id

    def _store_response(
        self, cache_key: Tuple[Any, ...], response: dns.message.Message
    ) -> None:
        if self.cache is None:
            return
        if response.rcode() not in (dns.rcode.NOERROR, dns.rcode.NXDOMAIN):
            return
        if response.flags & dns.flags.TC:
            return
        ttl = response_ttl(response)
        if ttl is None:
            return
        ttl = min(ttl, self.cfg.cache_max_ttl)
        if ttl < self.cfg.cache_min_ttl or ttl <= 0:
            return
        response.id = 0
        self.cache.put(cache_key, response, ttl)

    # -- TCP -------------------------------------------------------------

    async def _handle_tcp(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername") or ("unknown", 0)
        try:
            while True:
                try:
                    header = await asyncio.wait_for(
                        reader.readexactly(2), timeout=TCP_IDLE_TIMEOUT
                    )
                except (
                    asyncio.IncompleteReadError,
                    asyncio.TimeoutError,
                    ConnectionError,
                ):
                    break
                (length,) = struct.unpack("!H", header)
                if length < 12:
                    break
                try:
                    packet = await asyncio.wait_for(
                        reader.readexactly(length), timeout=TCP_IDLE_TIMEOUT
                    )
                except (
                    asyncio.IncompleteReadError,
                    asyncio.TimeoutError,
                    ConnectionError,
                ):
                    break
                try:
                    response = await self.handle_packet(packet, peer, "tcp")
                except Exception:
                    LOG.exception("error while handling a TCP query from %s", peer[0])
                    break
                if response is None:
                    break
                writer.write(struct.pack("!H", len(response)) + response)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # -- stats -----------------------------------------------------------

    def _maybe_write_stats(self) -> None:
        if time.monotonic() - self._last_stats_write >= STATS_MIN_WRITE_GAP:
            self._write_stats()

    def dump_stats(self) -> None:
        """Write a fresh stats snapshot (also used by SIGUSR1 / status.sh)."""
        self._write_stats()

    def _write_stats(self) -> None:
        self._last_stats_write = time.monotonic()
        data = {
            "app": APP_NAME,
            "version": VERSION,
            "pid": os.getpid(),
            "started_at": self._started_iso,
            "updated_at": now_iso(),
            "uptime_seconds": int(time.monotonic() - self._started_mono),
            "listen": self.cfg.listen,
            "port": self.cfg.port,
            "blocked_domains": len(self.cfg.blocked),
            "cache_entries": len(self.cache) if self.cache is not None else 0,
        }
        data.update(self.stats)
        try:
            tmp = self._stats_path.with_name(self._stats_path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self._stats_path)
        except OSError as exc:
            LOG.debug("could not write stats file %s: %s", self._stats_path, exc)

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(STATS_INTERVAL)
            self._write_stats()

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._started_mono = time.monotonic()
        self._started_iso = now_iso()

        self._udp_protocol = UDPProtocol(self)
        self._udp_transport, _ = await loop.create_datagram_endpoint(
            lambda: self._udp_protocol,
            local_addr=(self.cfg.listen, self.cfg.port),
        )
        try:
            self._tcp_server = await asyncio.start_server(
                self._handle_tcp,
                host=self.cfg.listen,
                port=self.cfg.port,
                reuse_address=True,
                backlog=128,
            )
        except Exception:
            self._udp_transport.close()
            self._udp_transport = None
            raise

        self._stats_task = asyncio.create_task(self._stats_loop())
        self._write_stats()
        LOG.info(
            "%s %s ready - DNS listening on %s:%s (UDP+TCP), %d blocked domain(s), upstream %s",
            APP_NAME,
            VERSION,
            self.cfg.listen,
            self.cfg.port,
            len(self.cfg.blocked),
            ", ".join(item.host for item in self.cfg.upstreams),
        )

    async def stop(self) -> None:
        LOG.info("shutting down")
        if self._tcp_server is not None:
            self._tcp_server.close()
            try:
                await self._tcp_server.wait_closed()
            except Exception:
                pass
            self._tcp_server = None
        if self._udp_transport is not None:
            self._udp_transport.close()
            self._udp_transport = None
        if self._stats_task is not None:
            self._stats_task.cancel()
            try:
                await self._stats_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._stats_task = None
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=3.0)
            for task in pending:
                task.cancel()
        self._write_stats()
        LOG.info("%s stopped", APP_NAME)


class UDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, app: PiBlockServer) -> None:
        self._app = app
        self._transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Tuple[Any, ...]) -> None:
        if len(self._app._tasks) >= MAX_INFLIGHT:
            self._app.stats["dropped"] += 1
            return
        if self._transport is None or self._transport.is_closing():
            return
        task = asyncio.create_task(self._respond(data, addr))
        self._app._tasks.add(task)
        task.add_done_callback(self._app._tasks.discard)

    async def _respond(self, data: bytes, addr: Tuple[Any, ...]) -> None:
        try:
            response = await self._app.handle_packet(data, addr, "udp")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("error while handling a UDP query from %s", addr[0])
            return
        transport = self._transport
        if response and transport is not None and not transport.is_closing():
            transport.sendto(response, addr)

    def error_received(self, exc: Exception) -> None:
        LOG.debug("UDP socket error: %s", exc)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def write_pidfile(path: Path) -> None:
    try:
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError as exc:
        LOG.warning("could not write pid file %s: %s", path, exc)


def remove_pidfile(path: Path) -> None:
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip() == str(
            os.getpid()
        ):
            path.unlink()
    except OSError:
        pass


async def serve(cfg: Config, pidfile: Optional[Path]) -> None:
    app = PiBlockServer(cfg)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        # status.sh asks for a fresh stats snapshot with SIGUSR1.
        loop.add_signal_handler(signal.SIGUSR1, app.dump_stats)
    except (NotImplementedError, RuntimeError, AttributeError):
        pass
    await app.start()
    if pidfile is not None:
        write_pidfile(pidfile)
    try:
        await stop_event.wait()
    finally:
        await app.stop()
        if pidfile is not None:
            remove_pidfile(pidfile)


def health_check(cfg: Config) -> int:
    if cfg.listen in ("0.0.0.0", ""):
        host = "127.0.0.1"
    elif cfg.listen == "::":
        host = "::1"
    else:
        host = cfg.listen
    target = sorted(cfg.blocked)[0] if cfg.blocked else "health.piblock.local"
    query = dns.message.make_query(target, "A")
    started = time.monotonic()
    try:
        response = dns.query.udp(query, host, port=cfg.port, timeout=3.0)
    except Exception as exc:
        print(
            f"{APP_NAME} health check failed: no answer from {host}:{cfg.port} ({exc})",
            file=sys.stderr,
        )
        return 1
    elapsed_ms = (time.monotonic() - started) * 1000.0
    if cfg.blocked and response.rcode() != dns.rcode.NXDOMAIN:
        print(
            f"{APP_NAME} health check failed: {target} returned "
            f"{dns.rcode.to_text(response.rcode())}, expected NXDOMAIN",
            file=sys.stderr,
        )
        return 1
    print(
        f"{APP_NAME} health check OK: {target} -> "
        f"{dns.rcode.to_text(response.rcode())} in {elapsed_ms:.1f} ms"
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="piblock",
        description="PiBlock - lightweight DNS filtering for the Raspberry Pi",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get("PIBLOCK_CONFIG", str(DEFAULT_CONFIG_PATH)),
        help="path to config.yaml (default: %(default)s)",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate the configuration, print a summary and exit",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="query the running PiBlock instance and exit",
    )
    parser.add_argument(
        "--pidfile",
        default=None,
        help="write the process id to this file (used by start.sh/stop.sh)",
    )
    parser.add_argument(
        "--version", action="version", version=f"{APP_NAME} {VERSION}"
    )
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser()
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(
            f"{APP_NAME}: configuration error in {config_path}", file=sys.stderr
        )
        for error in exc.errors:
            print(f"  x {error}", file=sys.stderr)
        print("Fix config.yaml and try again (see README.md).", file=sys.stderr)
        return 2

    if args.check_config:
        print(describe_config(cfg))
        print("configuration OK")
        return 0
    if args.health:
        return health_check(cfg)

    setup_logging(cfg)
    pidfile = Path(args.pidfile).expanduser() if args.pidfile else None
    try:
        asyncio.run(serve(cfg, pidfile))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            LOG.error(
                "cannot bind %s:%s: address already in use - stop systemd-resolved or dnsmasq first (see README.md)",
                cfg.listen,
                cfg.port,
            )
        elif exc.errno in (errno.EACCES, errno.EPERM):
            LOG.error(
                "cannot bind %s:%s: permission denied - ports below 1024 need root, run: sudo ./start.sh",
                cfg.listen,
                cfg.port,
            )
        else:
            LOG.error("failed to start on %s:%s: %s", cfg.listen, cfg.port, exc)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
