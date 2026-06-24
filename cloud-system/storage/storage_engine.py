#!/usr/bin/env python3
"""
Object Storage Engine — S3-style file storage.
Files are saved to disk, indexed in a JSON registry,
isolated per user (owner-based access control).
"""

import os
import json
import uuid
import hashlib
import shutil
from datetime import datetime
from threading import Lock

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR     = os.path.join(BASE_DIR, "data")
REGISTRY_FILE = os.path.join(BASE_DIR, "registry.json")

_lock = Lock()

# Limits — adjust based on your 128GB disk
MAX_FILE_SIZE_MB   = 1024          # max size per file
MAX_USER_QUOTA_MB  = 10240         # 10 GB per user by default

os.makedirs(DATA_DIR, exist_ok=True)


# ── Registry (metadata index) ────────────────────────────

def _load_registry():
    if not os.path.exists(REGISTRY_FILE):
        return {"files": {}}
    with open(REGISTRY_FILE, "r") as f:
        return json.load(f)


def _save_registry(data):
    tmp = REGISTRY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, REGISTRY_FILE)


# ── Quota tracking ────────────────────────────────────────

def get_user_usage_mb(owner):
    """Sum up storage used by a specific user."""
    registry = _load_registry()
    total_bytes = sum(
        f["size_bytes"] for f in registry["files"].values()
        if f["owner"] == owner
    )
    return round(total_bytes / 1024 / 1024, 2)


def check_quota(owner, new_file_size_mb):
    """Return (ok, reason)."""
    if new_file_size_mb > MAX_FILE_SIZE_MB:
        return False, f"File exceeds max size ({MAX_FILE_SIZE_MB} MB)"

    current_usage = get_user_usage_mb(owner)
    if current_usage + new_file_size_mb > MAX_USER_QUOTA_MB:
        return False, (
            f"Quota exceeded — using {current_usage}MB / "
            f"{MAX_USER_QUOTA_MB}MB"
        )
    return True, "OK"


# ── Core operations ──────────────────────────────────────

def save_file(file_obj, filename, owner, content_type="application/octet-stream"):
    """
    Save an uploaded file to disk and register it.
    file_obj must support .save(path) — like Flask's FileStorage,
    or .read() for raw bytes.
    """
    file_id    = str(uuid.uuid4())
    ext        = os.path.splitext(filename)[1]
    stored_name = f"{file_id}{ext}"
    stored_path = os.path.join(DATA_DIR, stored_name)

    # Save to disk
    file_obj.save(stored_path)

    size_bytes = os.path.getsize(stored_path)
    size_mb    = round(size_bytes / 1024 / 1024, 2)

    # Check quota AFTER saving (so we know actual size) —
    # if over quota, delete and reject
    ok, reason = check_quota(owner, size_mb)
    if not ok:
        os.remove(stored_path)
        return None, reason

    # Compute checksum for integrity
    checksum = hashlib.sha256()
    with open(stored_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            checksum.update(chunk)

    with _lock:
        registry = _load_registry()
        registry["files"][file_id] = {
            "file_id":      file_id,
            "filename":     filename,
            "stored_name":  stored_name,
            "owner":        owner,
            "content_type": content_type,
            "size_bytes":   size_bytes,
            "size_mb":      size_mb,
            "checksum":     checksum.hexdigest()[:16],
            "uploaded_at":  datetime.now().isoformat(),
        }
        _save_registry(registry)

    return file_id, "OK"


def get_file_path(file_id, owner):
    """Return the disk path for a file, if owned by this user."""
    registry = _load_registry()
    meta = registry["files"].get(file_id)
    if not meta:
        return None, "File not found"
    if meta["owner"] != owner:
        return None, "Access denied — not your file"
    path = os.path.join(DATA_DIR, meta["stored_name"])
    if not os.path.exists(path):
        return None, "File missing from disk"
    return path, meta


def get_file_meta(file_id):
    """Return metadata without owner check (internal use)."""
    registry = _load_registry()
    return registry["files"].get(file_id)


def list_files(owner):
    """List all files belonging to a user."""
    registry = _load_registry()
    files = [
        f for f in registry["files"].values()
        if f["owner"] == owner
    ]
    return sorted(files, key=lambda f: f["uploaded_at"], reverse=True)


def delete_file(file_id, owner):
    """Delete a file — only if owned by this user."""
    with _lock:
        registry = _load_registry()
        meta = registry["files"].get(file_id)

        if not meta:
            return False, "File not found"
        if meta["owner"] != owner:
            return False, "Access denied — not your file"

        path = os.path.join(DATA_DIR, meta["stored_name"])
        if os.path.exists(path):
            os.remove(path)

        del registry["files"][file_id]
        _save_registry(registry)

    return True, "Deleted"


def storage_stats():
    """Overall storage system stats."""
    registry   = _load_registry()
    files      = registry["files"].values()
    total_size = sum(f["size_bytes"] for f in files)

    # Disk space available
    disk_usage = shutil.disk_usage(DATA_DIR)

    # Per-user breakdown
    by_user = {}
    for f in files:
        owner = f["owner"]
        by_user[owner] = by_user.get(owner, 0) + f["size_mb"]

    return {
        "total_files":      len(files),
        "total_size_mb":    round(total_size / 1024 / 1024, 2),
        "disk_total_gb":    round(disk_usage.total / 1024**3, 1),
        "disk_used_gb":     round(disk_usage.used  / 1024**3, 1),
        "disk_free_gb":     round(disk_usage.free  / 1024**3, 1),
        "by_user":          {k: round(v, 2) for k, v in by_user.items()},
        "max_file_size_mb": MAX_FILE_SIZE_MB,
        "max_user_quota_mb": MAX_USER_QUOTA_MB,
    }
