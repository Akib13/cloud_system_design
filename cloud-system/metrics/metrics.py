#!/usr/bin/env python3
"""
Metrics Engine
Collects CPU and RAM usage per VM every 10 seconds.
Stores rolling 60-point history (10 minutes) per VM.
"""

import sys
import os
import time
import json
import docker
import threading
from datetime import datetime
from collections import deque

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import state as State

client       = docker.from_env()
VM_PREFIX    = "cloud-vm"
METRICS_FILE = os.path.join(BASE_DIR, "logs", "metrics.json")
HISTORY_LEN  = 60      # data points per VM
INTERVAL     = 10      # seconds between collections

# In-memory store: { vm_name: deque([{ts, cpu, ram}, ...]) }
_metrics     = {}
_lock        = threading.Lock()
_stop_event  = threading.Event()


# ── Collection ───────────────────────────────────────────

def collect_vm_stats(container):
    """
    Pull one stats sample from Docker for a container.
    Returns (cpu_pct, ram_pct, ram_mb) or None on error.
    """
    try:
        stats = container.stats(stream=False)

        # CPU %
        cpu_delta    = (
            stats["cpu_stats"]["cpu_usage"]["total_usage"] -
            stats["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        system_delta = (
            stats["cpu_stats"]["system_cpu_usage"] -
            stats["precpu_stats"]["system_cpu_usage"]
        )
        num_cpus = stats["cpu_stats"].get("online_cpus") or \
                   len(stats["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))

        cpu_pct = 0.0
        if system_delta > 0 and cpu_delta > 0:
            cpu_pct = (cpu_delta / system_delta) * num_cpus * 100.0
            cpu_pct = round(min(cpu_pct, 100.0), 2)

        # RAM
        mem_usage = stats["memory_stats"].get("usage", 0)
        mem_limit = stats["memory_stats"].get("limit", 1)
        ram_mb    = round(mem_usage / 1024 / 1024, 1)
        ram_pct   = round((mem_usage / mem_limit) * 100, 2)

        return cpu_pct, ram_pct, ram_mb

    except Exception:
        return None


def collect_all():
    """Collect stats for all running VMs."""
    vms = State.get_all_vms(include_destroyed=False)

    for vm in vms:
        if vm["status"] != "running":
            continue

        container_name = f"{VM_PREFIX}-{vm['name']}"
        try:
            container = client.containers.get(container_name)
        except docker.errors.NotFound:
            continue

        result = collect_vm_stats(container)
        if result is None:
            continue

        cpu_pct, ram_pct, ram_mb = result
        point = {
            "ts":      datetime.now().isoformat(),
            "cpu_pct": cpu_pct,
            "ram_pct": ram_pct,
            "ram_mb":  ram_mb,
        }

        with _lock:
            if vm["name"] not in _metrics:
                _metrics[vm["name"]] = deque(maxlen=HISTORY_LEN)
            _metrics[vm["name"]].append(point)

    # Persist to disk
    _save()


def _save():
    """Write current metrics snapshot to disk."""
    os.makedirs(os.path.dirname(METRICS_FILE), exist_ok=True)
    with _lock:
        data = {
            name: list(points)
            for name, points in _metrics.items()
        }
    with open(METRICS_FILE, "w") as f:
        json.dump(data, f)


# ── Query ────────────────────────────────────────────────

def get_vm_metrics(vm_name, points=60):
    """Return last N data points for a VM."""
    with _lock:
        history = list(_metrics.get(vm_name, []))
    return history[-points:]


def get_vm_latest(vm_name):
    """Return most recent single data point for a VM."""
    history = get_vm_metrics(vm_name, points=1)
    return history[-1] if history else None


def get_all_latest():
    """Return latest data point for every VM."""
    result = {}
    with _lock:
        for name, points in _metrics.items():
            if points:
                result[name] = list(points)[-1]
    return result


def get_cluster_history(points=60):
    """
    Aggregate all VM metrics into cluster-level time series.
    Returns list of {ts, total_cpu_pct, total_ram_mb}.
    """
    with _lock:
        all_data = {
            name: list(pts)
            for name, pts in _metrics.items()
        }

    if not all_data:
        return []

    # Use shortest history length
    min_len = min(len(v) for v in all_data.values())
    min_len = min(min_len, points)
    if min_len == 0:
        return []

    result = []
    vm_names = list(all_data.keys())

    for i in range(-min_len, 0):
        ts         = all_data[vm_names[0]][i]["ts"]
        total_cpu  = sum(all_data[n][i]["cpu_pct"] for n in vm_names)
        total_ram  = sum(all_data[n][i]["ram_mb"]  for n in vm_names)
        result.append({
            "ts":            ts,
            "total_cpu_pct": round(total_cpu, 2),
            "total_ram_mb":  round(total_ram, 1),
        })

    return result


# ── Background loop ──────────────────────────────────────

def run():
    print("[METRICS] Engine started — collecting every "
          f"{INTERVAL}s, keeping {HISTORY_LEN} points per VM")

    # Load persisted metrics on startup
    if os.path.exists(METRICS_FILE):
        try:
            with open(METRICS_FILE) as f:
                saved = json.load(f)
            with _lock:
                for name, points in saved.items():
                    _metrics[name] = deque(points, maxlen=HISTORY_LEN)
            print(f"[METRICS] Loaded saved metrics for "
                  f"{len(saved)} VM(s)")
        except Exception:
            pass

    while not _stop_event.is_set():
        try:
            collect_all()
        except Exception as e:
            print(f"[METRICS] Error: {e}")
        _stop_event.wait(INTERVAL)

    print("[METRICS] Engine stopped")


def stop():
    _stop_event.set()
