#!/usr/bin/env python3
"""
Snapshot Manager
Save, list, restore, and delete VM snapshots.
Snapshots are Docker images committed from running containers.
"""

import json
import os
import docker
from datetime import datetime
from threading import Lock

client         = docker.from_env()
SNAPSHOT_FILE  = os.path.expanduser("~/cloud-system/snapshots.json")
SNAPSHOT_TAG   = "cloud-snapshot"
VM_PREFIX      = "cloud-vm"
CLOUD_NETWORK  = "cloud-net"
_lock          = Lock()


# ── Snapshot registry (JSON) ─────────────────────────────

def _load():
    if not os.path.exists(SNAPSHOT_FILE):
        return {"snapshots": {}}
    with open(SNAPSHOT_FILE, "r") as f:
        return json.load(f)


def _save(data):
    tmp = SNAPSHOT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SNAPSHOT_FILE)


# ── Core operations ──────────────────────────────────────

def create_snapshot(vm_name, snapshot_name=None, description=""):
    """
    Commit a running container as a Docker image.
    snapshot_name defaults to vm_name + timestamp.
    """
    container_name = f"{VM_PREFIX}-{vm_name}"

    # Auto-generate snapshot name if not provided
    if not snapshot_name:
        ts            = datetime.now().strftime("%Y%m%d-%H%M%S")
        snapshot_name = f"{vm_name}-snap-{ts}"

    image_tag = f"{SNAPSHOT_TAG}:{snapshot_name}"

    # Check container exists
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        return False, f"VM '{vm_name}' not found"

    # Check snapshot name not already used
    data = _load()
    if snapshot_name in data["snapshots"]:
        return False, f"Snapshot '{snapshot_name}' already exists"

    print(f"[SNAPSHOT] Committing VM '{vm_name}' → image '{image_tag}'...")

    # Commit container to image
    image = container.commit(
        repository=SNAPSHOT_TAG,
        tag=snapshot_name,
        message=description or f"Snapshot of {vm_name}",
        author="MyCloud",
    )

    # Register in snapshot registry
    with _lock:
        data = _load()
        data["snapshots"][snapshot_name] = {
            "snapshot_name": snapshot_name,
            "source_vm":     vm_name,
            "image_tag":     image_tag,
            "image_id":      image.short_id,
            "description":   description or f"Snapshot of {vm_name}",
            "created_at":    datetime.now().isoformat(),
            "size_mb":       round(image.attrs["Size"] / 1024 / 1024, 1),
        }
        _save(data)

    print(f"[SNAPSHOT] ✅ Snapshot '{snapshot_name}' created ({image.attrs['Size'] // 1024 // 1024} MB)")
    return True, snapshot_name


def list_snapshots(vm_name=None):
    """List all snapshots, optionally filtered by source VM."""
    data      = _load()
    snapshots = list(data["snapshots"].values())
    if vm_name:
        snapshots = [s for s in snapshots if s["source_vm"] == vm_name]
    return snapshots


def get_snapshot(snapshot_name):
    """Get details of a specific snapshot."""
    data = _load()
    return data["snapshots"].get(snapshot_name)


def restore_snapshot(snapshot_name, new_vm_name, cpus="1", memory="256m"):
    """
    Restore a snapshot as a brand new VM.
    Creates a new container from the snapshot image.
    """
    import state as State

    snapshot = get_snapshot(snapshot_name)
    if not snapshot:
        return False, f"Snapshot '{snapshot_name}' not found"

    # Check new VM name is available
    if not State.name_available(new_vm_name):
        return False, f"VM name '{new_vm_name}' is already in use"

    image_tag      = snapshot["image_tag"]
    container_name = f"{VM_PREFIX}-{new_vm_name}"

    print(f"[SNAPSHOT] Restoring '{snapshot_name}' → new VM '{new_vm_name}'...")

    try:
        container = client.containers.run(
            image=image_tag,
            name=container_name,
            network=CLOUD_NETWORK,
            detach=True,
            cpu_quota=int(float(cpus) * 100000),
            cpu_period=100000,
            mem_limit=memory,
            labels={
                "cloud.vm":           "true",
                "cloud.name":         new_vm_name,
                "cloud.created":      datetime.now().isoformat(),
                "cloud.cpus":         str(cpus),
                "cloud.memory":       memory,
                "cloud.restored_from": snapshot_name,
            }
        )
    except docker.errors.ImageNotFound:
        return False, f"Snapshot image '{image_tag}' not found — may have been deleted"
    except Exception as e:
        return False, str(e)

    container.reload()
    ip = container.attrs["NetworkSettings"]["Networks"].get(
        CLOUD_NETWORK, {}
    ).get("IPAddress", "N/A")

    # Register in state
    State.register_vm(new_vm_name, container.short_id, ip, cpus, memory)

    print(f"[SNAPSHOT] ✅ VM '{new_vm_name}' restored at {ip}")
    return True, {
        "vm_name":         new_vm_name,
        "container_id":    container.short_id,
        "ip":              ip,
        "restored_from":   snapshot_name,
        "source_vm":       snapshot["source_vm"],
    }


def delete_snapshot(snapshot_name):
    """Delete a snapshot — removes Docker image and registry entry."""
    data     = _load()
    snapshot = data["snapshots"].get(snapshot_name)

    if not snapshot:
        return False, f"Snapshot '{snapshot_name}' not found"

    # Remove Docker image
    try:
        client.images.remove(snapshot["image_tag"], force=True)
        print(f"[SNAPSHOT] Docker image '{snapshot['image_tag']}' removed")
    except docker.errors.ImageNotFound:
        print(f"[SNAPSHOT] Image already gone — cleaning up registry only")
    except Exception as e:
        return False, f"Failed to remove image: {e}"

    # Remove from registry
    with _lock:
        data = _load()
        del data["snapshots"][snapshot_name]
        _save(data)

    print(f"[SNAPSHOT] ✅ Snapshot '{snapshot_name}' deleted")
    return True, f"Snapshot '{snapshot_name}' deleted"


def snapshot_storage_info():
    """Return total snapshot count and disk usage."""
    snapshots  = list_snapshots()
    total_size = sum(s.get("size_mb", 0) for s in snapshots)
    return {
        "count":       len(snapshots),
        "total_size_mb": round(total_size, 1),
    }
