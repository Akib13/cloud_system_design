#!/usr/bin/env python3
"""
Auto Scaler
Watches cluster resource usage and automatically
scales VMs up or down based on configurable policies.
"""

import sys
import os
import time
import json
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "vms"))
sys.path.insert(0, os.path.join(BASE_DIR, "scheduler"))

from provision       import create_vm, destroy_vm
from cloud_scheduler import (
    get_allocated_resources,
    get_free_resources,
    can_schedule,
    TOTAL_CPUS,
    TOTAL_MEMORY,
)
import state as State

# ── Default scaling policy ───────────────────────────────
DEFAULT_POLICY = {
    # Thresholds (percentage of total cluster resources)
    "scale_up_cpu_pct":    70,   # scale up if CPU usage exceeds this
    "scale_up_ram_pct":    70,   # scale up if RAM usage exceeds this
    "scale_down_cpu_pct":  25,   # scale down if CPU usage below this
    "scale_down_ram_pct":  25,   # scale down if RAM usage below this

    # VM spec for auto-spawned instances
    "vm_cpus":    "1",
    "vm_memory":  "256m",
    "vm_prefix":  "auto-vm",

    # Limits
    "min_vms":    1,             # never scale below this
    "max_vms":    6,             # never scale above this

    # Timing
    "check_interval": 30,        # seconds between checks
    "cooldown":       60,        # seconds to wait after a scaling action
}

POLICY_FILE   = os.path.join(BASE_DIR, "scaling-policy.json")
LOG_FILE      = os.path.join(BASE_DIR, "logs", "autoscaler.log")
_stop_event   = threading.Event()
_last_action  = 0   # timestamp of last scaling action


# ── Policy management ────────────────────────────────────

def load_policy():
    if not os.path.exists(POLICY_FILE):
        save_policy(DEFAULT_POLICY)
        return DEFAULT_POLICY.copy()
    with open(POLICY_FILE) as f:
        p = json.load(f)
    # Fill in any missing keys with defaults
    for k, v in DEFAULT_POLICY.items():
        p.setdefault(k, v)
    return p


def save_policy(policy):
    with open(POLICY_FILE, "w") as f:
        json.dump(policy, f, indent=2)


# ── Logging ──────────────────────────────────────────────

def log(msg, level="INFO"):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── Scaling logic ────────────────────────────────────────

def get_auto_vms():
    """Return all auto-scaled VMs (not destroyed), sorted oldest first."""
    policy = load_policy()
    vms    = State.get_all_vms(include_destroyed=False)
    return [
        v for v in vms
        if v["name"].startswith(policy["vm_prefix"])
    ]


def count_running_vms():
    """Count all non-destroyed VMs including manually created ones."""
    vms = State.get_all_vms(include_destroyed=False)
    return len([v for v in vms if v["status"] in ("running", "stopped")])


def generate_vm_name(policy):
    """Generate a unique auto-vm name."""
    ts = datetime.now().strftime("%H%M%S")
    return f"{policy['vm_prefix']}-{ts}"


def scale_up(policy):
    """Spawn a new VM."""
    global _last_action

    total_vms = count_running_vms()
    if total_vms >= policy["max_vms"]:
        log(f"Scale up skipped — already at max VMs ({policy['max_vms']})", "WARN")
        return False

    # Check resources available
    ok, reason = can_schedule(
        float(policy["vm_cpus"]),
        int(policy["vm_memory"].replace("m", ""))
    )
    if not ok:
        log(f"Scale up skipped — insufficient resources: {reason}", "WARN")
        return False

    name = generate_vm_name(policy)
    log(f"Scaling UP — creating VM '{name}' ({policy['vm_cpus']} CPU, {policy['vm_memory']})")

    container = create_vm(name, policy["vm_cpus"], policy["vm_memory"])
    if container:
        log(f"Scale up SUCCESS — VM '{name}' created")
        _last_action = time.time()
        return True
    else:
        log(f"Scale up FAILED — could not create VM '{name}'", "ERROR")
        return False


def scale_down(policy):
    """Destroy the oldest auto-scaled VM."""
    global _last_action

    auto_vms  = get_auto_vms()
    total_vms = count_running_vms()

    if total_vms <= policy["min_vms"]:
        log(f"Scale down skipped — already at min VMs ({policy['min_vms']})", "WARN")
        return False

    if not auto_vms:
        log("Scale down skipped — no auto-scaled VMs to remove", "WARN")
        return False

    # Destroy oldest auto VM
    oldest = auto_vms[0]
    name   = oldest["name"]
    log(f"Scaling DOWN — destroying VM '{name}'")
    destroy_vm(name)
    log(f"Scale down SUCCESS — VM '{name}' destroyed")
    _last_action = time.time()
    return True


def check_and_scale():
    """Main scaling decision loop — called every interval."""
    global _last_action

    policy = load_policy()

    # Cooldown check
    since_last = time.time() - _last_action
    if since_last < policy["cooldown"]:
        remaining = int(policy["cooldown"] - since_last)
        log(f"In cooldown — {remaining}s remaining", "DEBUG")
        return

    # Get current usage
    allocated_cpus, allocated_memory = get_allocated_resources()
    cpu_pct = (allocated_cpus   / TOTAL_CPUS)   * 100
    ram_pct = (allocated_memory / TOTAL_MEMORY)  * 100

    log(f"Cluster usage — CPU: {cpu_pct:.1f}%  RAM: {ram_pct:.1f}%  "
        f"VMs: {count_running_vms()}/{policy['max_vms']}")

    # Scale up decision
    if cpu_pct >= policy["scale_up_cpu_pct"] or \
       ram_pct >= policy["scale_up_ram_pct"]:
        log(f"Scale UP triggered — CPU {cpu_pct:.1f}% or RAM {ram_pct:.1f}% "
            f"exceeds threshold")
        scale_up(policy)
        return

    # Scale down decision
    if cpu_pct <= policy["scale_down_cpu_pct"] and \
       ram_pct <= policy["scale_down_ram_pct"]:
        log(f"Scale DOWN triggered — CPU {cpu_pct:.1f}% and RAM {ram_pct:.1f}% "
            f"below threshold")
        scale_down(policy)
        return

    log("No scaling action needed")


# ── Main loop ────────────────────────────────────────────

def run():
    policy = load_policy()
    log("=" * 50)
    log("Auto Scaler started")
    log(f"  Scale up  if CPU > {policy['scale_up_cpu_pct']}% "
        f"or RAM > {policy['scale_up_ram_pct']}%")
    log(f"  Scale down if CPU < {policy['scale_down_cpu_pct']}% "
        f"and RAM < {policy['scale_down_ram_pct']}%")
    log(f"  Min VMs: {policy['min_vms']}  Max VMs: {policy['max_vms']}")
    log(f"  Check interval: {policy['check_interval']}s  "
        f"Cooldown: {policy['cooldown']}s")
    log("=" * 50)

    while not _stop_event.is_set():
        try:
            check_and_scale()
        except Exception as e:
            log(f"Error during scaling check: {e}", "ERROR")
        _stop_event.wait(policy["check_interval"])

    log("Auto Scaler stopped")


def stop():
    _stop_event.set()


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 autoscaler.py start          — run scaler")
        print("  python3 autoscaler.py status         — show current policy")
        print("  python3 autoscaler.py set <key> <value> — update policy")
        print("  python3 autoscaler.py check          — run one check now")
        print("\nPolicy keys:")
        for k, v in DEFAULT_POLICY.items():
            print(f"  {k:<30} (default: {v})")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "start":
        run()

    elif cmd == "check":
        check_and_scale()

    elif cmd == "status":
        policy = load_policy()
        print("\n  Current Scaling Policy")
        print("  " + "─" * 40)
        for k, v in policy.items():
            print(f"  {k:<30} {v}")
        print()

    elif cmd == "set":
        if len(sys.argv) < 4:
            print("[ERROR] Usage: autoscaler.py set <key> <value>")
            sys.exit(1)
        key   = sys.argv[2]
        value = sys.argv[3]
        policy = load_policy()
        if key not in policy:
            print(f"[ERROR] Unknown key '{key}'")
            sys.exit(1)
        # Cast to correct type
        original = DEFAULT_POLICY[key]
        if isinstance(original, int):
            policy[key] = int(value)
        elif isinstance(original, float):
            policy[key] = float(value)
        else:
            policy[key] = value
        save_policy(policy)
        print(f"[OK] {key} = {policy[key]}")

    else:
        print(f"[ERROR] Unknown command '{cmd}'")
        sys.exit(1)
