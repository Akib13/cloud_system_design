#!/usr/bin/env python3
"""
Cloud State Manager
Persistent JSON-based state tracking for all VM instances.
Survives API restarts, tracks full VM lifecycle.
"""

import json
import os
from datetime import datetime
from threading import Lock

STATE_FILE = os.path.expanduser("~/cloud-system/cloud-state.json")
_lock      = Lock()


def _load():
    """Load state from disk."""
    if not os.path.exists(STATE_FILE):
        return {"vms": {}}
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def _save(state):
    """Write state to disk atomically."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ── Public API ───────────────────────────────────────────

def register_vm(name, container_id, ip, cpus, memory):
    """Record a newly created VM."""
    with _lock:
        state = _load()
        state["vms"][name] = {
            "name":         name,
            "container_id": container_id,
            "ip":           ip,
            "cpus":         str(cpus),
            "memory":       str(memory),
            "status":       "running",
            "created_at":   datetime.now().isoformat(),
            "updated_at":   datetime.now().isoformat(),
            "history": [
                {"event": "created", "time": datetime.now().isoformat()}
            ]
        }
        _save(state)


def update_vm_status(name, status):
    """Update the status of an existing VM."""
    with _lock:
        state = _load()
        if name in state["vms"]:
            state["vms"][name]["status"]     = status
            state["vms"][name]["updated_at"] = datetime.now().isoformat()
            state["vms"][name]["history"].append({
                "event": status,
                "time":  datetime.now().isoformat()
            })
            _save(state)


def remove_vm(name):
    with _lock:
        state = _load()
        if name in state["vms"]:
            state["vms"][name]["status"]       = "destroyed"
            state["vms"][name]["updated_at"]   = datetime.now().isoformat()
            state["vms"][name]["destroyed_at"] = datetime.now().isoformat()
            state["vms"][name]["history"].append({
                "event": "destroyed",
                "time":  datetime.now().isoformat()
            })
            _save(state)
            print(f"[STATE] VM '{name}' marked as destroyed in state.")
        else:
            print(f"[STATE] WARNING — VM '{name}' not found in state file.")


def get_vm(name):
    """Get state record for a specific VM."""
    state = _load()
    return state["vms"].get(name)


def name_available(name):
    """
    Return True if name can be used for a new VM.
    Destroyed VMs free up their name for reuse.
    """
    vm = get_vm(name)
    if vm is None:
        return True
    # Allow reuse only if previously destroyed
    return vm["status"] == "destroyed"


def get_all_vms(include_destroyed=False):
    """Return all VM records, optionally including destroyed ones."""
    state = _load()
    vms   = list(state["vms"].values())
    if not include_destroyed:
        vms = [v for v in vms if v["status"] != "destroyed"]
    return vms


def get_history(name):
    """Return full event history for a VM."""
    vm = get_vm(name)
    if vm:
        return vm.get("history", [])
    return []
