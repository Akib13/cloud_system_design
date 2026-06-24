#!/usr/bin/env python3
"""
Cloud Resource Scheduler
Tracks CPU/RAM usage across all VMs and makes
admission decisions for new instance requests.
"""

import docker
import sys
from datetime import datetime

client = docker.from_env()

CLOUD_NETWORK = "cloud-net"
VM_PREFIX     = "cloud-vm"

# ── Total resources available in your cloud ──────────────
# Adjust these based on what you allocated to your Ubuntu VM
TOTAL_CPUS   = 4      # total CPU cores available
TOTAL_MEMORY = 2048   # total RAM in MB


# ── Resource tracking ────────────────────────────────────

def get_allocated_resources():
    """Sum up CPU and RAM already allocated to running VMs."""
    containers = client.containers.list(
        filters={"label": "cloud.vm=true"}
    )

    allocated_cpus   = 0.0
    allocated_memory = 0  # in MB

    for c in containers:
        cpus   = float(c.labels.get("cloud.cpus", "0"))
        memory = c.labels.get("cloud.memory", "0m")

        # Convert memory label to MB
        if memory.endswith("m"):
            memory_mb = int(memory[:-1])
        elif memory.endswith("g"):
            memory_mb = int(memory[:-1]) * 1024
        else:
            memory_mb = int(memory)

        allocated_cpus   += cpus
        allocated_memory += memory_mb

    return allocated_cpus, allocated_memory


def get_free_resources():
    """Return how much CPU and RAM is still available."""
    allocated_cpus, allocated_memory = get_allocated_resources()
    free_cpus   = TOTAL_CPUS   - allocated_cpus
    free_memory = TOTAL_MEMORY - allocated_memory
    return free_cpus, free_memory


# ── Admission control ────────────────────────────────────

def can_schedule(requested_cpus, requested_memory_mb):
    """
    Decide if a new VM can be scheduled.
    Returns (True, reason) or (False, reason).
    """
    free_cpus, free_memory = get_free_resources()

    if requested_cpus > TOTAL_CPUS:
        return False, f"Requested CPUs ({requested_cpus}) exceeds total cluster CPUs ({TOTAL_CPUS})"

    if requested_memory_mb > TOTAL_MEMORY:
        return False, f"Requested RAM ({requested_memory_mb}MB) exceeds total cluster RAM ({TOTAL_MEMORY}MB)"

    if requested_cpus > free_cpus:
        return False, f"Not enough CPUs — requested {requested_cpus}, only {free_cpus:.1f} free"

    if requested_memory_mb > free_memory:
        return False, f"Not enough RAM — requested {requested_memory_mb}MB, only {free_memory}MB free"

    return True, "Resources available — VM can be scheduled"


# ── Status display ───────────────────────────────────────

def show_status():
    """Print a full resource usage report."""
    allocated_cpus, allocated_memory = get_allocated_resources()
    free_cpus, free_memory           = get_free_resources()

    cpu_pct = (allocated_cpus   / TOTAL_CPUS)   * 100
    ram_pct = (allocated_memory / TOTAL_MEMORY)  * 100

    # Build usage bars
    def bar(pct, width=30):
        filled = int(width * pct / 100)
        return "[" + "█" * filled + "░" * (width - filled) + f"] {pct:.1f}%"

    print("\n╔══════════════════════════════════════════════╗")
    print("║         CLOUD SCHEDULER — RESOURCE STATUS        ║")
    print("╠══════════════════════════════════════════════╣")
    print(f"║  Total CPUs    : {TOTAL_CPUS} cores")
    print(f"║  Total RAM     : {TOTAL_MEMORY} MB")
    print("╠══════════════════════════════════════════════╣")
    print(f"║  CPU  used     : {allocated_cpus:.1f} / {TOTAL_CPUS} cores")
    print(f"║  {bar(cpu_pct)}")
    print(f"║  RAM  used     : {allocated_memory} / {TOTAL_MEMORY} MB")
    print(f"║  {bar(ram_pct)}")
    print("╠══════════════════════════════════════════════╣")
    print(f"║  Free CPUs     : {free_cpus:.1f} cores")
    print(f"║  Free RAM      : {free_memory} MB")
    print("╠══════════════════════════════════════════════╣")

    # List running VMs
    containers = client.containers.list(
        filters={"label": "cloud.vm=true"}
    )
    print(f"║  Running VMs   : {len(containers)}")
    print("╠══════════════════════════════════════════════╣")

    if containers:
        print(f"║  {'NAME':<16} {'CPU':<8} {'RAM':<10} {'STATUS'}")
        print(f"║  {'────':<16} {'───':<8} {'───':<10} {'──────'}")
        for c in containers:
            name   = c.labels.get("cloud.name", "unknown")
            cpus   = c.labels.get("cloud.cpus", "?")
            memory = c.labels.get("cloud.memory", "?")
            print(f"║  {name:<16} {cpus:<8} {memory:<10} {c.status}")
    else:
        print("║  No VMs running.")

    print("╚══════════════════════════════════════════════╝")
    print(f"  Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")


def check_request(cpus, memory):
    """Check if a VM request would be admitted."""
    # Parse memory
    memory = str(memory)
    if memory.endswith("m"):
        memory_mb = int(memory[:-1])
    elif memory.endswith("g"):
        memory_mb = int(memory[:-1]) * 1024
    else:
        memory_mb = int(memory)

    cpus = float(cpus)
    ok, reason = can_schedule(cpus, memory_mb)

    print(f"\n  Admission check — {cpus} CPU(s), {memory_mb}MB RAM")
    print(f"  Decision : {'✅ ADMIT' if ok else '❌ REJECT'}")
    print(f"  Reason   : {reason}\n")
    return ok


# ── Scheduled provisioning ───────────────────────────────

def schedule_vm(name, cpus="1", memory="256m"):
    """
    Full scheduling flow:
    1. Check resources
    2. Admit or reject
    3. Provision if admitted
    """
    import sys
    sys.path.append("../vms")
    from provision import create_vm

    # Parse memory to MB for check
    mem_str = str(memory)
    if mem_str.endswith("m"):
        memory_mb = int(mem_str[:-1])
    elif mem_str.endswith("g"):
        memory_mb = int(mem_str[:-1]) * 1024
    else:
        memory_mb = int(mem_str)

    print(f"\n[SCHEDULER] Request received — VM '{name}' ({cpus} CPU, {memory} RAM)")

    ok, reason = can_schedule(float(cpus), memory_mb)

    if ok:
        print(f"[SCHEDULER] ✅ Admitted — {reason}")
        create_vm(name, cpus, memory)
    else:
        print(f"[SCHEDULER] ❌ Rejected — {reason}")
        show_status()


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 scheduler.py status")
        print("  python3 scheduler.py check <cpus> <memory>")
        print("  python3 scheduler.py schedule <name> <cpus> <memory>")
        sys.exit(1)

    command = sys.argv[1]

    if command == "status":
        show_status()

    elif command == "check":
        if len(sys.argv) < 4:
            print("[ERROR] Usage: scheduler.py check <cpus> <memory>")
            sys.exit(1)
        check_request(sys.argv[2], sys.argv[3])

    elif command == "schedule":
        if len(sys.argv) < 5:
            print("[ERROR] Usage: scheduler.py schedule <name> <cpus> <memory>")
            sys.exit(1)
        schedule_vm(sys.argv[2], sys.argv[3], sys.argv[4])

    else:
        print(f"[ERROR] Unknown command '{command}'")
        sys.exit(1)
