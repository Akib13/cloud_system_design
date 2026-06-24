#!/usr/bin/env python3
"""
Cloud VM Provisioner
Manages container lifecycle — create, list, stop, destroy.
Now backed by persistent state manager.
"""

import docker
import sys
from datetime import datetime


import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import state as State

client = docker.from_env()

CLOUD_IMAGE   = "cloud-base:v1"
CLOUD_NETWORK = "cloud-net"
VM_PREFIX     = "cloud-vm"


def create_vm(name, cpus="1", memory="256m"):
    """Spin up a new cloud VM instance."""
    container_name = f"{VM_PREFIX}-{name}"

    # Check state — not just Docker
    if not State.name_available(name):
        print(f"[ERROR] VM '{name}' already exists and is not destroyed.")
        return None

    # Also clean up any orphaned Docker container with this name
    try:
        orphan = client.containers.get(container_name)
        orphan.remove(force=True)
        print(f"[INFO] Removed orphaned container '{container_name}'")
    except docker.errors.NotFound:
        pass

    print(f"[INFO] Creating VM '{name}' — CPU: {cpus}, RAM: {memory}")

    container = client.containers.run(
        image=CLOUD_IMAGE,
        name=container_name,
        network=CLOUD_NETWORK,
        detach=True,
        cpu_quota=int(float(cpus) * 100000),
        cpu_period=100000,
        mem_limit=memory,
        labels={
            "cloud.vm":      "true",
            "cloud.name":    name,
            "cloud.created": datetime.now().isoformat(),
            "cloud.cpus":    str(cpus),
            "cloud.memory":  memory,
        }
    )

    container.reload()
    ip = container.attrs["NetworkSettings"]["Networks"][CLOUD_NETWORK]["IPAddress"]

    # Register in persistent state
    State.register_vm(name, container.short_id, ip, cpus, memory)

    print(f"[OK] VM '{name}' started")
    print(f"     Container ID : {container.short_id}")
    print(f"     IP Address   : {ip}")
    print(f"     SSH          : ssh clouduser@{ip}  (password: cloudpass)")
    return container


def list_vms():
    """List all active VMs from state."""
    vms = State.get_all_vms(include_destroyed=False)

    if not vms:
        print("[INFO] No cloud VMs currently running.")
        return

    print(f"\n{'NAME':<20} {'ID':<12} {'STATUS':<12} {'IP':<16} {'CPU':<6} {'RAM':<8} {'CREATED'}")
    print("-" * 90)

    for vm in vms:
        print(f"{vm['name']:<20} {vm['container_id']:<12} {vm['status']:<12} "
              f"{vm['ip']:<16} {vm['cpus']:<6} {vm['memory']:<8} {vm['created_at'][:19]}")


def stop_vm(name):
    """Stop a running cloud VM."""
    container_name = f"{VM_PREFIX}-{name}"
    try:
        container = client.containers.get(container_name)
        container.stop(timeout=5)
        State.update_vm_status(name, "stopped")
        print(f"[OK] VM '{name}' stopped.")
    except docker.errors.NotFound:
        print(f"[ERROR] VM '{name}' not found.")


def destroy_vm(name):
    """Stop, remove, and mark VM as destroyed in state."""
    container_name = f"{VM_PREFIX}-{name}"
    try:
        container = client.containers.get(container_name)
        container.stop(timeout=5)
        container.remove()
    except docker.errors.NotFound:
        pass  # Container already gone — still clean up state

    # Always update state regardless of Docker outcome
    State.remove_vm(name)
    print(f"[OK] VM '{name}' destroyed and removed from state.")


def vm_info(name):
    """Show detailed info about a specific VM from state."""
    vm = State.get_vm(name)
    if not vm:
        print(f"[ERROR] VM '{name}' not found in state.")
        return

    print(f"\n VM Info — {name}")
    print(f"  Container ID : {vm['container_id']}")
    print(f"  Status       : {vm['status']}")
    print(f"  IP Address   : {vm['ip']}")
    print(f"  CPU limit    : {vm['cpus']} core(s)")
    print(f"  Memory limit : {vm['memory']}")
    print(f"  Created      : {vm['created_at'][:19]}")
    print(f"  Updated      : {vm['updated_at'][:19]}")

    history = State.get_history(name)
    if history:
        print(f"\n  Event History:")
        for h in history:
            print(f"    {h['time'][:19]}  →  {h['event']}")


# ── CLI ──────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 provision.py create <name> [cpus] [memory]")
        print("  python3 provision.py list")
        print("  python3 provision.py info <name>")
        print("  python3 provision.py stop <name>")
        print("  python3 provision.py destroy <name>")
        sys.exit(1)

    command = sys.argv[1]

    if command == "create":
        if len(sys.argv) < 3:
            print("[ERROR] Provide a VM name.")
            sys.exit(1)
        name   = sys.argv[2]
        cpus   = sys.argv[3] if len(sys.argv) > 3 else "1"
        memory = sys.argv[4] if len(sys.argv) > 4 else "256m"
        create_vm(name, cpus, memory)

    elif command == "list":
        list_vms()

    elif command == "info":
        vm_info(sys.argv[2])

    elif command == "stop":
        stop_vm(sys.argv[2])

    elif command == "destroy":
        destroy_vm(sys.argv[2])

    else:
        print(f"[ERROR] Unknown command '{command}'")
        sys.exit(1)
