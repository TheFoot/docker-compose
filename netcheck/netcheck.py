#!/usr/bin/env python3
"""
netcheck — Network health check for thefootonline.local
Managed by Dave. Runs on smarthome01 as a Docker container.

Usage:
    python netcheck.py --serve              # Start Flask API on port 8090
    python netcheck.py --quick              # Run quick check, print + store
    python netcheck.py --full               # Run full check, print + store
    python netcheck.py --quick --no-store   # Run quick check, print only
"""

import argparse
import http.client
import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timezone

import dns.resolver
from flask import Flask, jsonify, request
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SELF_EXPECTED_IP = "10.137.1.49"
GATEWAY_IP = "10.137.1.49"
ROUTER_IP = "10.137.1.1"
SUBNET = "10.137.1.0/24"
MULLVAD_CHECK_URL = "https://am.i.mullvad.net/json"
CONNECTIVITY_CHECK_URL = "http://connectivitycheck.gstatic.com/generate_204"
INTERNAL_DNS_TEST = ("spark.thefootonline.local", "10.137.1.27")
EXTERNAL_DNS_TEST = "google.com"

KNOWN_DEVICES = {
    "belkin_router":  {"ip": "10.137.1.1",   "name": "Belkin Router",    "always_on": True},
    "smarthome01":    {"ip": "10.137.1.49",  "name": "smarthome01",      "always_on": True},
    "spark":          {"ip": "10.137.1.27",  "name": "DGX Spark",        "always_on": True},
    "macbook":        {"ip": "10.137.1.189", "name": "MacBook Pro M5",   "always_on": False},
    "printer":        {"ip": "10.137.1.190", "name": "HP ENVY 5000",     "always_on": False},
    "wsd_macbook":    {"ip": "10.137.1.248", "name": "WSD MacBook",      "always_on": False},
}

# Service directory — single source of truth for port inventory
SERVICE_DIRECTORY = {
    "dnsmasq":  {"port": 53,    "host": "10.137.1.49",  "proto": "tcp", "server": "smarthome01", "description": "DNS/DHCP server"},
    "litellm":  {"port": 4000,  "host": "10.137.1.49",  "proto": "tcp", "server": "smarthome01", "description": "LLM API gateway"},
    "n8n":      {"port": 5678,  "host": "10.137.1.49",  "proto": "tcp", "server": "smarthome01", "description": "Workflow automation"},
    "mongo":    {"port": 27017, "host": "127.0.0.1",    "proto": "tcp", "server": "smarthome01", "description": "MongoDB (localhost only)"},
    "postgres": {"port": 5432,  "host": "127.0.0.1",    "proto": "tcp", "server": "smarthome01", "description": "PostgreSQL (localhost only)"},
    "redis":    {"port": 6379,  "host": "127.0.0.1",    "proto": "tcp", "server": "smarthome01", "description": "Redis (localhost only)"},
    "netcheck": {"port": 8090,  "host": "10.137.1.49",  "proto": "tcp", "server": "smarthome01", "description": "Network health check API"},
    "vllm":     {"port": 8000,  "host": "10.137.1.27",  "proto": "tcp", "server": "spark",       "description": "vLLM inference (OpenAI-compatible)"},
    "ssh":      {"port": 22,    "host": "10.137.1.49",  "proto": "tcp", "server": "smarthome01", "description": "SSH (key-only)"},
    "samba":    {"port": 445,   "host": "10.137.1.49",  "proto": "tcp", "server": "smarthome01", "description": "Samba file sharing (SMB)"},
    "netbios":  {"port": 139,   "host": "10.137.1.49",  "proto": "tcp", "server": "smarthome01", "description": "NetBIOS/Samba (legacy)"},
    "cups":     {"port": 631,   "host": "127.0.0.1",    "proto": "tcp", "server": "smarthome01", "description": "CUPS printing (localhost)"},
}

EXPECTED_CONTAINERS = [
    "dnsmasq", "litellm", "n8n", "mongo", "postgres", "redis", "netcheck",
]

DHCP_LEASE_FILE = "/host/dnsmasq/dnsmasq.leases"
IP_FORWARD_FILE = "/host/proc/sys/net/ipv4/ip_forward"
WG_HANDSHAKE_MAX_AGE = 180  # seconds — warn if last handshake older than this

# MongoDB config from environment
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://127.0.0.1:27017")
MONGO_DB = os.environ.get("MONGO_DB", "network_monitor")
NETCHECK_PORT = int(os.environ.get("NETCHECK_PORT", "8090"))

# tasks CLI — repo root on smarthome01; override via DAVE_REPO_ROOT for local testing
DAVE_REPO_ROOT = os.environ.get("DAVE_REPO_ROOT", "/home/thefoot/dave")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_cmd(cmd, timeout=10):
    """Run a shell command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def ping(host, count=1, timeout=2):
    """Ping a host, return (reachable, rtt_ms)."""
    rc, out, _ = run_cmd(f"ping -c {count} -W {timeout} {host}")
    rtt = None
    if rc == 0:
        m = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", out)
        if m:
            rtt = float(m.group(1))
    return rc == 0, rtt


def tcp_connect(host, port, timeout=3):
    """Test TCP connection, return (success, latency_ms)."""
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, round((time.monotonic() - start) * 1000, 1)
    except (OSError, socket.timeout):
        return False, None


def timed(fn):
    """Run fn(), return (result, elapsed_ms)."""
    start = time.monotonic()
    result = fn()
    elapsed = round((time.monotonic() - start) * 1000)
    return result, elapsed


DOCKER_SOCKET = "/var/run/docker.sock"


def docker_api(path):
    """Query the Docker Engine API via Unix socket. Returns parsed JSON or None."""
    try:
        conn = http.client.HTTPConnection("localhost")
        conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.sock.connect(DOCKER_SOCKET)
        conn.request("GET", path)
        resp = conn.getresponse()
        if resp.status == 200:
            return json.loads(resp.read())
        return None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_self_network():
    """Verify this host's IP is the expected static IP."""
    rc, out, _ = run_cmd("ip -4 addr show | grep 'inet 10.137'")
    if rc != 0:
        return {"status": "fail", "detail": {"error": "Could not determine IP"}}

    current_ip = None
    interface = None
    for line in out.splitlines():
        m = re.search(r"inet ([\d.]+)/\d+ .* (\S+)$", line.strip())
        if m:
            current_ip = m.group(1)
            interface = m.group(2)
            break

    if current_ip == SELF_EXPECTED_IP:
        return {"status": "ok", "detail": {"ip": current_ip, "interface": interface}}
    else:
        return {
            "status": "fail",
            "detail": {
                "ip": current_ip, "expected": SELF_EXPECTED_IP, "interface": interface,
                "message": f"IP mismatch — possible DHCP reassignment (expected {SELF_EXPECTED_IP}, got {current_ip})"
            },
        }


def check_gateway():
    """Ping the upstream router."""
    reachable, rtt = ping(ROUTER_IP)
    return {
        "status": "ok" if reachable else "fail",
        "detail": {"host": ROUTER_IP, "reachable": reachable, "rtt_ms": rtt},
    }


def check_dns_internal():
    """Resolve an internal hostname via dnsmasq."""
    name, expected_ip = INTERNAL_DNS_TEST
    try:
        resolver = dns.resolver.Resolver()
        resolver.nameservers = [GATEWAY_IP]
        resolver.lifetime = 5
        answers = resolver.resolve(name, "A")
        resolved = str(answers[0])
        ok = resolved == expected_ip
        return {
            "status": "ok" if ok else "warn",
            "detail": {"query": name, "resolved": resolved, "expected": expected_ip},
        }
    except Exception as e:
        return {"status": "fail", "detail": {"query": name, "error": str(e)}}


def check_dns_external():
    """Resolve an external domain via dnsmasq."""
    try:
        resolver = dns.resolver.Resolver()
        resolver.nameservers = [GATEWAY_IP]
        resolver.lifetime = 5
        answers = resolver.resolve(EXTERNAL_DNS_TEST, "A")
        resolved = [str(a) for a in answers]
        return {"status": "ok", "detail": {"query": EXTERNAL_DNS_TEST, "resolved": resolved}}
    except Exception as e:
        return {"status": "fail", "detail": {"query": EXTERNAL_DNS_TEST, "error": str(e)}}


def check_internet():
    """Test internet connectivity via HTTP."""
    rc, out, _ = run_cmd(
        f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 {CONNECTIVITY_CHECK_URL}"
    )
    code = out.strip("'\"")
    ok = code == "204"
    return {
        "status": "ok" if ok else "fail",
        "detail": {"url": CONNECTIVITY_CHECK_URL, "http_code": code},
    }


def check_vpn_tunnel():
    """Check WireGuard tunnel status."""
    rc, out, err = run_cmd("wg show wg0")
    if rc != 0:
        return {"status": "fail", "detail": {"error": f"wg show failed: {err}"}}

    # Parse endpoint and latest handshake
    endpoint = None
    handshake_age = None
    transfer_rx = None
    transfer_tx = None

    for line in out.splitlines():
        line = line.strip()
        if line.startswith("endpoint:"):
            endpoint = line.split(":", 1)[1].strip()
        elif line.startswith("latest handshake:"):
            hs_str = line.split(":", 1)[1].strip()
            # Parse "X minutes, Y seconds ago" or "X seconds ago" etc.
            total_seconds = 0
            for part in re.findall(r"(\d+)\s+(hour|minute|second)", hs_str):
                val = int(part[0])
                unit = part[1]
                if unit == "hour":
                    total_seconds += val * 3600
                elif unit == "minute":
                    total_seconds += val * 60
                else:
                    total_seconds += val
            handshake_age = total_seconds
        elif line.startswith("transfer:"):
            parts = line.split(":", 1)[1].strip()
            transfer_rx = parts.split("received")[0].strip()
            tx_part = parts.split(",")[-1].strip()
            transfer_tx = tx_part.split("sent")[0].strip()

    status = "ok"
    message = None
    if handshake_age is None:
        status = "warn"
        message = "Could not determine last handshake time"
    elif handshake_age > WG_HANDSHAKE_MAX_AGE:
        status = "warn"
        message = f"Last handshake {handshake_age}s ago (threshold: {WG_HANDSHAKE_MAX_AGE}s)"

    return {
        "status": status,
        "detail": {
            "endpoint": endpoint,
            "handshake_age_seconds": handshake_age,
            "transfer_rx": transfer_rx,
            "transfer_tx": transfer_tx,
            "message": message,
        },
    }


def check_docker_containers():
    """Check expected Docker containers are running via Docker API."""
    containers = docker_api("/containers/json?all=true")
    if containers is None:
        return {"status": "fail", "detail": {"error": "Docker API unavailable"}}

    running = {}
    for c in containers:
        # Container names have a leading / in the API
        names = [n.lstrip("/") for n in c.get("Names", [])]
        state = c.get("State", "")
        status_str = c.get("Status", "")
        for name in names:
            running[name] = {"state": state, "status": status_str}

    missing = []
    unhealthy = []
    found = {}
    for name in EXPECTED_CONTAINERS:
        if name in running:
            found[name] = running[name]["status"]
            if "unhealthy" in running[name]["status"].lower():
                unhealthy.append(name)
            elif running[name]["state"] != "running":
                missing.append(name)
        else:
            missing.append(name)

    status = "ok"
    if unhealthy:
        status = "warn"
    if missing:
        status = "fail"

    return {
        "status": status,
        "detail": {
            "running": found,
            "missing": missing,
            "unhealthy": unhealthy,
            "extra": [n for n in running if n not in EXPECTED_CONTAINERS],
        },
    }


def check_known_devices():
    """Ping all known devices for presence check."""
    results = {}
    reachable = 0
    unreachable_always_on = []

    for key, dev in KNOWN_DEVICES.items():
        ok, rtt = ping(dev["ip"], count=1, timeout=1)
        results[key] = {
            "name": dev["name"], "ip": dev["ip"],
            "reachable": ok, "rtt_ms": rtt,
        }
        if ok:
            reachable += 1
        elif dev.get("always_on"):
            unreachable_always_on.append(key)

    status = "ok"
    if unreachable_always_on:
        status = "warn"

    return {
        "status": status,
        "detail": {
            "devices": results,
            "total": len(KNOWN_DEVICES),
            "reachable": reachable,
            "unreachable_always_on": unreachable_always_on,
        },
    }


def check_dhcp_service():
    """Verify DHCP (dnsmasq) is operational."""
    # Check container is running via Docker API
    containers = docker_api('/containers/json?filters={"name":["dnsmasq"]}')
    container_ok = containers is not None and len(containers) > 0 and containers[0].get("State") == "running"

    # Check lease file
    lease_ok = False
    lease_count = 0
    try:
        if os.path.exists(DHCP_LEASE_FILE):
            with open(DHCP_LEASE_FILE) as f:
                leases = f.readlines()
            lease_count = len(leases)
            lease_ok = True
    except Exception:
        pass

    status = "ok" if (container_ok and lease_ok) else "warn" if container_ok else "fail"

    return {
        "status": status,
        "detail": {
            "container_running": container_ok,
            "lease_file_readable": lease_ok,
            "active_leases": lease_count,
        },
    }


# ---------------------------------------------------------------------------
# Full check — additional deep diagnostics
# ---------------------------------------------------------------------------

def check_service_ports():
    """TCP connect to all known service ports."""
    results = {}
    all_ok = True
    for name, svc in SERVICE_DIRECTORY.items():
        if svc["proto"] != "tcp":
            continue
        ok, latency = tcp_connect(svc["host"], svc["port"])
        results[name] = {
            "host": svc["host"], "port": svc["port"],
            "reachable": ok, "latency_ms": latency,
            "description": svc["description"],
        }
        if not ok:
            all_ok = False

    return {
        "status": "ok" if all_ok else "fail",
        "detail": {"services": results},
    }


def check_dns_timing():
    """Resolve multiple domains and measure timing."""
    domains = [
        ("spark.thefootonline.local", GATEWAY_IP),
        ("macbook.thefootonline.local", GATEWAY_IP),
        ("google.com", GATEWAY_IP),
        ("github.com", GATEWAY_IP),
        ("cloudflare.com", GATEWAY_IP),
    ]
    results = []
    for domain, ns in domains:
        start = time.monotonic()
        try:
            resolver = dns.resolver.Resolver()
            resolver.nameservers = [ns]
            resolver.lifetime = 5
            answers = resolver.resolve(domain, "A")
            elapsed = round((time.monotonic() - start) * 1000, 1)
            results.append({
                "domain": domain, "nameserver": ns,
                "resolved": [str(a) for a in answers],
                "ms": elapsed, "ok": True,
            })
        except Exception as e:
            elapsed = round((time.monotonic() - start) * 1000, 1)
            results.append({
                "domain": domain, "nameserver": ns,
                "error": str(e), "ms": elapsed, "ok": False,
            })

    failed = [r for r in results if not r["ok"]]
    return {
        "status": "ok" if not failed else "fail",
        "detail": {"queries": results, "failed_count": len(failed)},
    }


def check_latency():
    """Extended ping to gateway and router."""
    results = {}
    for label, host in [("router", ROUTER_IP), ("gateway_self", SELF_EXPECTED_IP)]:
        rc, out, _ = run_cmd(f"ping -c 10 -W 2 {host}")
        stats = {"host": host}
        if rc == 0:
            m = re.search(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", out)
            if m:
                stats.update({
                    "min_ms": float(m.group(1)), "avg_ms": float(m.group(2)),
                    "max_ms": float(m.group(3)), "mdev_ms": float(m.group(4)),
                })
            loss_m = re.search(r"(\d+)% packet loss", out)
            if loss_m:
                stats["loss_pct"] = float(loss_m.group(1))
        else:
            stats["error"] = "ping failed"
        results[label] = stats

    return {"status": "ok", "detail": results}


def check_dhcp_leases():
    """Parse DHCP lease file for analysis."""
    try:
        if not os.path.exists(DHCP_LEASE_FILE):
            return {"status": "warn", "detail": {"error": "Lease file not found"}}

        with open(DHCP_LEASE_FILE) as f:
            lines = f.readlines()

        leases = []
        ips_seen = {}
        macs_seen = {}
        known_macs = set()  # We don't track MACs here but could extend

        for line in lines:
            parts = line.strip().split()
            if len(parts) >= 4:
                expiry, mac, ip, hostname = parts[0], parts[1], parts[2], parts[3]
                leases.append({
                    "expiry": int(expiry), "mac": mac, "ip": ip, "hostname": hostname,
                })
                # Conflict detection: same IP, different MAC
                if ip in ips_seen and ips_seen[ip] != mac:
                    pass  # Would flag conflict
                ips_seen[ip] = mac
                macs_seen[mac] = ip

        return {
            "status": "ok",
            "detail": {
                "total_leases": len(leases),
                "leases": leases,
                "unique_ips": len(ips_seen),
                "unique_macs": len(macs_seen),
            },
        }
    except Exception as e:
        return {"status": "warn", "detail": {"error": str(e)}}


def check_disk_space():
    """Check disk usage on key mount points."""
    rc, out, _ = run_cmd("df -h / /mnt/data /mnt/backup /mnt/share 2>/dev/null")
    if rc != 0 and not out:
        return {"status": "warn", "detail": {"error": "df command failed"}}

    mounts = {}
    worst = "ok"
    for line in out.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) >= 6:
            mount = parts[5]
            use_pct = int(parts[4].rstrip("%"))
            mounts[mount] = {
                "device": parts[0], "size": parts[1],
                "used": parts[2], "available": parts[3], "use_pct": use_pct,
            }
            if use_pct >= 95:
                worst = "fail"
            elif use_pct >= 80 and worst != "fail":
                worst = "warn"

    return {"status": worst, "detail": {"mounts": mounts}}


def check_container_resources():
    """Get container resource usage via Docker API."""
    containers = docker_api("/containers/json")
    if containers is None:
        return {"status": "warn", "detail": {"error": "Docker API unavailable"}}

    results = []
    for c in containers:
        name = c.get("Names", ["?"])[0].lstrip("/")
        # Get individual container stats (one-shot)
        stats = docker_api(f"/containers/{c['Id']}/stats?stream=false")
        if stats:
            # Calculate CPU percentage
            cpu_pct = 0.0
            cpu_stats = stats.get("cpu_stats", {})
            precpu = stats.get("precpu_stats", {})
            if cpu_stats.get("cpu_usage") and precpu.get("cpu_usage"):
                cpu_delta = cpu_stats["cpu_usage"].get("total_usage", 0) - precpu["cpu_usage"].get("total_usage", 0)
                sys_delta = cpu_stats.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)
                n_cpus = cpu_stats.get("online_cpus", 1)
                if sys_delta > 0:
                    cpu_pct = round((cpu_delta / sys_delta) * n_cpus * 100, 2)

            # Memory
            mem = stats.get("memory_stats", {})
            mem_usage = mem.get("usage", 0)
            mem_limit = mem.get("limit", 1)
            mem_pct = round((mem_usage / mem_limit) * 100, 2) if mem_limit else 0

            results.append({
                "name": name,
                "cpu_pct": cpu_pct,
                "mem_usage_mb": round(mem_usage / 1024 / 1024, 1),
                "mem_limit_mb": round(mem_limit / 1024 / 1024, 1),
                "mem_pct": mem_pct,
            })
        else:
            results.append({"name": name, "error": "stats unavailable"})

    return {"status": "ok", "detail": {"containers": results}}


def check_vpn_leak():
    """Check external IP is Mullvad, not ISP."""
    rc, out, _ = run_cmd(f"curl -s --max-time 10 {MULLVAD_CHECK_URL}")
    if rc != 0 or not out:
        return {"status": "warn", "detail": {"error": "Could not reach Mullvad check"}}

    try:
        data = json.loads(out)
        is_mullvad = data.get("mullvad_exit_ip", False)
        return {
            "status": "ok" if is_mullvad else "fail",
            "detail": {
                "mullvad_exit": is_mullvad,
                "ip": data.get("ip"),
                "country": data.get("country"),
                "city": data.get("city"),
                "organization": data.get("organization"),
            },
        }
    except json.JSONDecodeError:
        return {"status": "warn", "detail": {"error": "Invalid JSON from Mullvad"}}


def check_traceroute():
    """Traceroute to verify traffic goes through VPN tunnel."""
    rc, out, _ = run_cmd("traceroute -n -m 15 -w 2 1.1.1.1", timeout=60)
    hops = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+([\d.*]+)", line)
        if m:
            hops.append({"hop": int(m.group(1)), "ip": m.group(2)})

    # First hop should NOT be the router (10.137.1.1) — it should be the VPN tunnel
    first_hop_is_router = len(hops) > 0 and hops[0].get("ip") == ROUTER_IP
    return {
        "status": "warn" if first_hop_is_router else "ok",
        "detail": {
            "hops": hops[:15],
            "first_hop_is_router": first_hop_is_router,
            "message": "Traffic may be bypassing VPN" if first_hop_is_router else None,
        },
    }


def check_security_ports():
    """Quick nmap scan for unexpected open ports."""
    rc, out, _ = run_cmd(
        "nmap -sT --top-ports 100 --min-rate 5000 -T4 127.0.0.1 2>/dev/null",
        timeout=30,
    )
    open_ports = []
    for line in out.splitlines():
        m = re.match(r"(\d+)/tcp\s+open\s+(\S+)", line)
        if m:
            open_ports.append({"port": int(m.group(1)), "service": m.group(2)})

    expected_ports = {svc["port"] for svc in SERVICE_DIRECTORY.values() if svc["host"] == "127.0.0.1" or svc["host"] == SELF_EXPECTED_IP}
    unexpected = [p for p in open_ports if p["port"] not in expected_ports]

    return {
        "status": "warn" if unexpected else "ok",
        "detail": {
            "open_ports": open_ports,
            "unexpected": unexpected,
        },
    }


def check_ip_forwarding():
    """Verify IP forwarding is enabled."""
    try:
        if os.path.exists(IP_FORWARD_FILE):
            with open(IP_FORWARD_FILE) as f:
                val = f.read().strip()
        else:
            _, val, _ = run_cmd("cat /proc/sys/net/ipv4/ip_forward")
        enabled = val == "1"
        return {
            "status": "ok" if enabled else "fail",
            "detail": {"ip_forward": val, "enabled": enabled},
        }
    except Exception as e:
        return {"status": "warn", "detail": {"error": str(e)}}


def check_iptables_killswitch():
    """Verify the VPN kill switch iptables rule exists."""
    rc, out, _ = run_cmd("iptables -L FORWARD -n -v 2>/dev/null")
    if rc != 0:
        return {"status": "warn", "detail": {"error": "iptables command failed (needs root?)"}}

    has_drop = "DROP" in out
    has_wg0 = "wg0" in out

    return {
        "status": "ok" if (has_drop and has_wg0) else "warn",
        "detail": {
            "has_drop_rule": has_drop,
            "has_wg0_rules": has_wg0,
            "message": None if (has_drop and has_wg0) else "Kill switch rules may be missing",
        },
    }


# ---------------------------------------------------------------------------
# Scheduled tasks collection
# ---------------------------------------------------------------------------

def collect_scheduled_tasks():
    """Invoke operator/extensions/tools/tasks CLI and return (tasksList, summaryStr, healthy).

    Returns:
        tasksList  — list of task dicts from 'tasks status --json', or []
        summaryStr — human-readable aggregate e.g. "3 ok, 1 failing, 0 stale, 0 unknown"
        healthy    — True iff every task status is 'ok' (or list is empty)
    """
    try:
        result = subprocess.run(
            ["node", "operator/extensions/tools/tasks/bin/tasks", "status", "--json"],
            cwd=DAVE_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        print("collect_scheduled_tasks: subprocess timed out", file=__import__("sys").stderr)
        return [], "unavailable: timeout", False
    except Exception as exc:
        print(f"collect_scheduled_tasks: subprocess error: {exc}", file=__import__("sys").stderr)
        return [], f"unavailable: {exc}", False

    stdout = result.stdout.strip()

    # tasks CLI emits this literal when the knowledge tree has no manifests
    if stdout == "no tasks found":
        return [], "0 tasks", True

    try:
        tasks = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        snippet = (stdout or result.stderr or "")[:120]
        print(f"collect_scheduled_tasks: non-JSON output: {snippet!r}", file=__import__("sys").stderr)
        return [], f"unavailable: parse error", False

    if not isinstance(tasks, list):
        return [], "unavailable: unexpected shape", False

    counts = {"ok": 0, "failing": 0, "stale": 0, "unknown": 0}
    for task in tasks:
        status = task.get("status", "unknown")
        if status in counts:
            counts[status] += 1
        else:
            counts["unknown"] += 1

    summary = (
        f"{counts['ok']} ok, {counts['failing']} failing, "
        f"{counts['stale']} stale, {counts['unknown']} unknown"
    )
    healthy = counts["failing"] == 0 and counts["stale"] == 0 and counts["unknown"] == 0
    return tasks, summary, healthy


# ---------------------------------------------------------------------------
# Check orchestrators
# ---------------------------------------------------------------------------

QUICK_CHECKS = [
    ("self_network", check_self_network),
    ("gateway", check_gateway),
    ("dns_internal", check_dns_internal),
    ("dns_external", check_dns_external),
    ("internet", check_internet),
    ("vpn_tunnel", check_vpn_tunnel),
    ("docker_containers", check_docker_containers),
    ("known_devices", check_known_devices),
    ("dhcp_service", check_dhcp_service),
]

FULL_EXTRA_CHECKS = [
    ("service_ports", check_service_ports),
    ("dns_timing", check_dns_timing),
    ("latency", check_latency),
    ("dhcp_leases", check_dhcp_leases),
    ("disk_space", check_disk_space),
    ("container_resources", check_container_resources),
    ("vpn_leak", check_vpn_leak),
    ("traceroute", check_traceroute),
    ("security_ports", check_security_ports),
    ("ip_forwarding", check_ip_forwarding),
    ("iptables_killswitch", check_iptables_killswitch),
]


def run_checks(check_type="quick"):
    """Run the specified check suite and return structured results."""
    start = time.monotonic()

    checks_to_run = list(QUICK_CHECKS)
    if check_type == "full":
        checks_to_run += FULL_EXTRA_CHECKS

    results = {}
    issues = []

    for name, fn in checks_to_run:
        try:
            check_result, elapsed = timed(fn)
            check_result["ms"] = elapsed
            results[name] = check_result

            if check_result["status"] in ("warn", "fail"):
                detail = check_result.get("detail", {})
                message = detail.get("message") or detail.get("error") or f"{name} reported {check_result['status']}"
                issues.append({
                    "check": name,
                    "severity": check_result["status"],
                    "message": message,
                })
        except Exception as e:
            results[name] = {"status": "fail", "detail": {"error": str(e)}, "ms": 0}
            issues.append({"check": name, "severity": "fail", "message": str(e)})

    # Determine overall summary from check results
    statuses = [r["status"] for r in results.values()]
    if "fail" in statuses:
        summary = "fail"
    elif "warn" in statuses:
        summary = "warn"
    else:
        summary = "ok"

    # Collect scheduled task health and fold into summary
    scheduled_tasks, scheduled_tasks_summary, scheduled_tasks_healthy = collect_scheduled_tasks()

    if not scheduled_tasks_healthy:
        # Check whether any task is failing (→ fail) or merely stale/unknown (→ warn)
        task_statuses = {t.get("status") for t in scheduled_tasks}
        if "failing" in task_statuses:
            if summary != "fail":
                summary = "fail"
        else:
            # stale or unknown: escalate to warn but don't downgrade an existing fail
            if summary == "ok":
                summary = "warn"

    # Build device summary from known_devices check
    device_summary = None
    if "known_devices" in results:
        dev_detail = results["known_devices"].get("detail", {})
        device_summary = {
            "total_known": dev_detail.get("total", 0),
            "reachable": dev_detail.get("reachable", 0),
            "unreachable_always_on": dev_detail.get("unreachable_always_on", []),
        }

    duration_ms = round((time.monotonic() - start) * 1000)

    return {
        "type": check_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": duration_ms,
        "summary": summary,
        "checks": results,
        "issues": issues,
        "device_summary": device_summary,
        "scheduledTasks": scheduled_tasks,
        "scheduledTasksSummary": scheduled_tasks_summary,
        "scheduledTasksHealthy": scheduled_tasks_healthy,
    }


# ---------------------------------------------------------------------------
# MongoDB storage
# ---------------------------------------------------------------------------

_mongo_client = None
_mongo_db = None


def get_db():
    """Get MongoDB database connection (lazy init)."""
    global _mongo_client, _mongo_db
    if _mongo_db is not None:
        return _mongo_db
    try:
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        _mongo_client.admin.command("ping")
        _mongo_db = _mongo_client[MONGO_DB]

        # Create indexes
        _mongo_db.checks.create_index([("timestamp", -1)])
        _mongo_db.checks.create_index([("type", 1), ("timestamp", -1)])
        _mongo_db.alerts.create_index([("timestamp", -1)])
        _mongo_db.alerts.create_index([("resolved", 1), ("severity", 1)])

        return _mongo_db
    except ConnectionFailure:
        _mongo_client = None
        _mongo_db = None
        return None


def store_results(result):
    """Store check results and any alerts in MongoDB. Returns True on success."""
    db = get_db()
    if db is None:
        return False

    try:
        check_doc = dict(result)
        insert = db.checks.insert_one(check_doc)
        check_id = insert.inserted_id

        # Write alerts for any issues
        for issue in result.get("issues", []):
            db.alerts.insert_one({
                "timestamp": result["timestamp"],
                "check_id": check_id,
                "severity": issue["severity"],
                "check_name": issue["check"],
                "message": issue["message"],
                "resolved": False,
            })

        return True
    except Exception:
        return False


def get_history(limit=50, check_type=None):
    """Retrieve recent check results."""
    db = get_db()
    if db is None:
        return []

    query = {}
    if check_type:
        query["type"] = check_type

    cursor = db.checks.find(query, {"_id": 0}).sort("timestamp", -1).limit(limit)
    return list(cursor)


# ---------------------------------------------------------------------------
# Flask API
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "netcheck"})


@app.route("/check/quick")
def api_quick():
    result = run_checks("quick")
    stored = store_results(result)
    result["stored"] = stored
    if not stored:
        result["storage_warning"] = "MongoDB unavailable — results not persisted"
    return jsonify(result)


@app.route("/check/full")
def api_full():
    result = run_checks("full")
    stored = store_results(result)
    result["stored"] = stored
    if not stored:
        result["storage_warning"] = "MongoDB unavailable — results not persisted"
    return jsonify(result)


@app.route("/history")
def api_history():
    limit = request.args.get("limit", 50, type=int)
    check_type = request.args.get("type", None)
    return jsonify(get_history(limit, check_type))


@app.route("/directory")
def api_directory():
    """Service and port directory — single source of truth."""
    directory = {}
    for name, svc in SERVICE_DIRECTORY.items():
        directory[name] = {
            "host": svc["host"],
            "port": svc["port"],
            "proto": svc["proto"],
            "server": svc["server"],
            "description": svc["description"],
            "url": f"http://{svc['host']}:{svc['port']}" if svc["proto"] == "tcp" else None,
        }

    # Group by server
    by_server = {}
    for name, svc in directory.items():
        server = svc["server"]
        if server not in by_server:
            by_server[server] = []
        by_server[server].append({"name": name, **svc})

    return jsonify({"services": directory, "by_server": by_server})


@app.route("/alerts")
def api_alerts():
    """Get unresolved alerts."""
    db = get_db()
    if db is None:
        return jsonify({"error": "MongoDB unavailable"}), 503

    alerts = list(
        db.alerts.find({"resolved": False}, {"_id": 0})
        .sort("timestamp", -1)
        .limit(100)
    )
    # Convert ObjectId fields to strings for JSON serialisation
    for alert in alerts:
        for key, val in alert.items():
            if hasattr(val, '__str__') and type(val).__name__ == 'ObjectId':
                alert[key] = str(val)
    return jsonify(alerts)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Network health check for thefootonline.local")
    parser.add_argument("--serve", action="store_true", help="Start Flask API server")
    parser.add_argument("--quick", action="store_true", help="Run quick check")
    parser.add_argument("--full", action="store_true", help="Run full check")
    parser.add_argument("--no-store", action="store_true", help="Don't store results in MongoDB")
    parser.add_argument("--directory", action="store_true", help="Print service directory")
    args = parser.parse_args()

    if args.serve:
        app.run(host="0.0.0.0", port=NETCHECK_PORT, debug=False)
    elif args.directory:
        print(json.dumps(SERVICE_DIRECTORY, indent=2))
    elif args.quick or args.full:
        check_type = "full" if args.full else "quick"
        result = run_checks(check_type)

        if not args.no_store:
            stored = store_results(result)
            result["stored"] = stored
            if not stored:
                result["storage_warning"] = "MongoDB unavailable — results not persisted"

        print(json.dumps(result, indent=2, default=str))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
