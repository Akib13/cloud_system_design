#!/usr/bin/env python3
"""
Cloud REST API — with JWT authentication
"""

import sys
import os
import docker
import threading
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager, create_access_token,
    jwt_required, get_jwt_identity, get_jwt
)
from datetime import datetime, timedelta
from functools import wraps

# ── Path setup — BASE_DIR defined first ──────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "vms"))
sys.path.insert(0, os.path.join(BASE_DIR, "scheduler"))
sys.path.insert(0, os.path.join(BASE_DIR, "autoscaler"))
sys.path.insert(0, os.path.join(BASE_DIR, "metrics"))
sys.path.insert(0, os.path.join(BASE_DIR, "storage"))

# ── Local imports — after path setup ─────────────────────
from provision import create_vm, stop_vm, destroy_vm
from cloud_scheduler import (
    get_allocated_resources,
    get_free_resources,
    can_schedule,
    TOTAL_CPUS,
    TOTAL_MEMORY,
)
import state       as State
import users       as Users
import snapshots   as Snapshots
import autoscaler  as AutoScaler
import metrics as Metrics
import storage_engine as Storage

# ── App setup ────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

from dotenv import load_dotenv
load_dotenv()

app.config["JWT_SECRET_KEY"] = os.environ.get(
    "JWT_SECRET_KEY",
    "dev-only-insecure-default-change-me"
)
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=8)
app.config["JWT_QUERY_STRING_NAME"] = "token"

jwt    = JWTManager(app)
client = docker.from_env()

CLOUD_NETWORK = "cloud-net"
VM_PREFIX     = "cloud-vm"

# Bootstrap default admin on startup
Users.ensure_default_admin()


# ── Role decorator ───────────────────────────────────────

def admin_required(fn):
    """Decorator — restricts endpoint to admin role only."""
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        claims = get_jwt()
        if claims.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapper


# ── Helpers ──────────────────────────────────────────────

def parse_memory_mb(memory_str):
    memory_str = str(memory_str)
    if memory_str.endswith("g"):
        return int(memory_str[:-1]) * 1024
    elif memory_str.endswith("m"):
        return int(memory_str[:-1])
    return int(memory_str)


def container_to_dict(c):
    c.reload()
    ip = c.attrs["NetworkSettings"]["Networks"].get(
        CLOUD_NETWORK, {}
    ).get("IPAddress", "N/A")
    return {
        "name":    c.labels.get("cloud.name", "unknown"),
        "id":      c.short_id,
        "status":  c.status,
        "ip":      ip,
        "cpus":    c.labels.get("cloud.cpus", "?"),
        "memory":  c.labels.get("cloud.memory", "?"),
        "created": c.labels.get("cloud.created", "?")[:19],
        "ssh":     f"ssh clouduser@{ip}",
    }


# ── Auth routes ──────────────────────────────────────────

@app.route("/auth/signup", methods=["POST"])
def signup():
    data = request.get_json()
    if not data or "username" not in data or "password" not in data:
        return jsonify({"error": "Missing username or password"}), 400

    username = data["username"].strip()
    password = data["password"]

    if len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    ok, msg = Users.create_user(username, password, role="user")
    if not ok:
        return jsonify({"error": msg}), 409

    # Auto-login after signup
    token = create_access_token(
        identity=username,
        additional_claims={"role": "user"}
    )

    return jsonify({
        "status":   "created",
        "token":    token,
        "username": username,
        "role":     "user",
    }), 201

@app.route("/auth/login", methods=["POST"])
def login():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing request body"}), 400

    username = data.get("username", "")
    password = data.get("password", "")

    ok, result = Users.verify_user(username, password)
    if not ok:
        return jsonify({"error": result}), 401

    role  = result
    token = create_access_token(
        identity=username,
        additional_claims={"role": role}
    )

    return jsonify({
        "token":    token,
        "username": username,
        "role":     role,
        "expires":  "8 hours",
    })


@app.route("/auth/me", methods=["GET"])
@jwt_required()
def me():
    username = get_jwt_identity()
    user     = Users.get_user(username)
    return jsonify(user)


@app.route("/auth/users", methods=["GET"])
@admin_required
def list_users():
    return jsonify({"users": Users.list_users()})


@app.route("/auth/users", methods=["POST"])
@admin_required
def create_user():
    data = request.get_json()
    if not data or "username" not in data or "password" not in data:
        return jsonify({"error": "Missing username or password"}), 400

    ok, msg = Users.create_user(
        data["username"],
        data["password"],
        data.get("role", "user")
    )
    if not ok:
        return jsonify({"error": msg}), 409
    return jsonify({"status": "created", "username": data["username"]}), 201


@app.route("/auth/users/<username>", methods=["DELETE"])
@admin_required
def delete_user(username):
    current = get_jwt_identity()
    if username == current:
        return jsonify({"error": "Cannot delete yourself"}), 400
    ok, msg = Users.delete_user(username)
    if not ok:
        return jsonify({"error": msg}), 404
    return jsonify({"status": "deleted", "username": username})


# ── Cloud routes (all protected) ─────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "name":    "MyCloud API",
        "version": "2.0",
        "auth":    "JWT required on all /vm and /status endpoints",
        "login":   "POST /auth/login"
    })


@app.route("/status", methods=["GET"])
@admin_required          # was @jwt_required()
def status():
    allocated_cpus, allocated_memory = get_allocated_resources()
    free_cpus, free_memory           = get_free_resources()
    containers = client.containers.list(filters={"label": "cloud.vm=true"})

    return jsonify({
        "cluster": {
            "total_cpus":          TOTAL_CPUS,
            "total_memory_mb":     TOTAL_MEMORY,
            "allocated_cpus":      allocated_cpus,
            "allocated_memory_mb": allocated_memory,
            "free_cpus":           free_cpus,
            "free_memory_mb":      free_memory,
            "cpu_usage_pct":       round((allocated_cpus / TOTAL_CPUS) * 100, 1),
            "ram_usage_pct":       round((allocated_memory / TOTAL_MEMORY) * 100, 1),
        },
        "vm_count":  len(containers),
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/vm/create", methods=["POST"])
@admin_required          # was @jwt_required()
def api_create_vm():
    data = request.get_json()
    if not data or "name" not in data:
        return jsonify({"error": "Missing required field: name"}), 400

    name   = data.get("name")
    cpus   = str(data.get("cpus",   "1"))
    memory = str(data.get("memory", "256m"))
    owner  = get_jwt_identity()

    if not State.name_available(name):
        return jsonify({"error": f"VM '{name}' already exists"}), 409

    memory_mb  = parse_memory_mb(memory)
    ok, reason = can_schedule(float(cpus), memory_mb)
    if not ok:
        return jsonify({"status": "rejected", "reason": reason}), 409

    container = create_vm(name, cpus, memory)
    if container is None:
        return jsonify({"error": "Failed to create VM"}), 500

    # Tag owner in state
    State.update_vm_status(name, "running")
    vm = State.get_vm(name)
    vm["owner"] = owner

    return jsonify({"status": "created", "vm": container_to_dict(container)}), 201


@app.route("/vm/list", methods=["GET"])
@admin_required          # was @jwt_required()
def api_list_vms():
    state_vms = State.get_all_vms(include_destroyed=False)
    result    = []

    for vm in state_vms:
        container_name = f"{VM_PREFIX}-{vm['name']}"
        try:
            c = client.containers.get(container_name)
            c.reload()
            live_status = c.status
            ip = c.attrs["NetworkSettings"]["Networks"].get(
                CLOUD_NETWORK, {}
            ).get("IPAddress", vm["ip"])
        except docker.errors.NotFound:
            live_status = "lost"
            ip = vm["ip"]

        if live_status != vm["status"]:
            State.update_vm_status(vm["name"], live_status)

        result.append({
            "name":    vm["name"],
            "id":      vm["container_id"],
            "status":  live_status,
            "ip":      ip,
            "cpus":    vm["cpus"],
            "memory":  vm["memory"],
            "created": vm["created_at"][:19],
            "owner":   vm.get("owner", "unknown"),
            "ssh":     f"ssh clouduser@{ip}",
        })

    return jsonify({"count": len(result), "vms": result})


@app.route("/vm/all", methods=["GET"])
@admin_required
def api_all_vms():
    vms = State.get_all_vms(include_destroyed=True)
    return jsonify({"count": len(vms), "vms": vms})


@app.route("/vm/check", methods=["POST"])
@admin_required          # was @jwt_required()
def api_check():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing request body"}), 400

    cpus      = float(data.get("cpus",   1))
    memory    = str(data.get("memory", "256m"))
    memory_mb = parse_memory_mb(memory)
    ok, reason = can_schedule(cpus, memory_mb)
    free_cpus, free_memory = get_free_resources()

    return jsonify({
        "decision":       "admit" if ok else "reject",
        "reason":         reason,
        "requested":      {"cpus": cpus, "memory_mb": memory_mb},
        "free_resources": {"cpus": free_cpus, "memory_mb": free_memory},
    })


@app.route("/vm/<name>", methods=["GET"])
@admin_required          # was @jwt_required()
def api_vm_info(name):
    vm = State.get_vm(name)
    if not vm:
        return jsonify({"error": f"VM '{name}' not found"}), 404
    return jsonify(vm)


@app.route("/vm/<name>/start", methods=["POST"])
@admin_required          # was @jwt_required()
def api_start_vm(name):
    container_name = f"{VM_PREFIX}-{name}"
    try:
        c = client.containers.get(container_name)
        c.start()
        c.reload()
        ip = c.attrs["NetworkSettings"]["Networks"].get(
            CLOUD_NETWORK, {}
        ).get("IPAddress", "N/A")
        State.update_vm_status(name, "running")
        return jsonify({"status": "running", "name": name, "ip": ip})
    except docker.errors.NotFound:
        return jsonify({"error": f"VM '{name}' not found"}), 404


@app.route("/vm/<name>/stop", methods=["POST"])
@admin_required          # was @jwt_required()
def api_stop_vm(name):
    container_name = f"{VM_PREFIX}-{name}"
    try:
        c = client.containers.get(container_name)
        c.stop(timeout=5)
        State.update_vm_status(name, "stopped")
        return jsonify({"status": "stopped", "name": name})
    except docker.errors.NotFound:
        return jsonify({"error": f"VM '{name}' not found"}), 404


@app.route("/vm/<name>", methods=["DELETE"])
@admin_required          # was @jwt_required()
def api_destroy_vm(name):
    container_name = f"{VM_PREFIX}-{name}"
    try:
        c = client.containers.get(container_name)
        c.stop(timeout=5)
        c.remove()
    except docker.errors.NotFound:
        pass
    State.remove_vm(name)
    return jsonify({"status": "destroyed", "name": name})


@app.route("/vm/<name>/history", methods=["GET"])
@admin_required          # was @jwt_required()
def api_vm_history(name):
    history = State.get_history(name)
    if not history:
        return jsonify({"error": f"No history for VM '{name}'"}), 404
    return jsonify({"name": name, "history": history})


# ── Snapshot routes ──────────────────────────────────────

@app.route("/vm/<name>/snapshot", methods=["POST"])
@admin_required          # was @jwt_required()
def api_create_snapshot(name):
    data          = request.get_json() or {}
    snapshot_name = data.get("snapshot_name")
    description   = data.get("description", "")

    ok, result = Snapshots.create_snapshot(name, snapshot_name, description)
    if not ok:
        return jsonify({"error": result}), 400

    snapshot = Snapshots.get_snapshot(result)
    return jsonify({
        "status":   "created",
        "snapshot": snapshot,
    }), 201


@app.route("/snapshots", methods=["GET"])
@admin_required          # was @jwt_required()
def api_list_snapshots():
    vm_name   = request.args.get("vm")
    snapshots = Snapshots.list_snapshots(vm_name)
    info      = Snapshots.snapshot_storage_info()
    return jsonify({
        "count":     len(snapshots),
        "storage":   info,
        "snapshots": snapshots,
    })


@app.route("/snapshots/<snapshot_name>", methods=["GET"])
@admin_required          # was @jwt_required()
def api_get_snapshot(snapshot_name):
    snapshot = Snapshots.get_snapshot(snapshot_name)
    if not snapshot:
        return jsonify({"error": f"Snapshot '{snapshot_name}' not found"}), 404
    return jsonify(snapshot)


@app.route("/snapshots/<snapshot_name>/restore", methods=["POST"])
@admin_required          # was @jwt_required()
def api_restore_snapshot(snapshot_name):
    data        = request.get_json() or {}
    new_vm_name = data.get("vm_name")
    cpus        = str(data.get("cpus",   "1"))
    memory      = str(data.get("memory", "256m"))

    if not new_vm_name:
        return jsonify({"error": "Missing required field: vm_name"}), 400

    ok, result = Snapshots.restore_snapshot(snapshot_name, new_vm_name, cpus, memory)
    if not ok:
        return jsonify({"error": result}), 400

    return jsonify({
        "status": "restored",
        "vm":     result,
    }), 201


@app.route("/snapshots/<snapshot_name>", methods=["DELETE"])
@admin_required          # was @jwt_required()
def api_delete_snapshot(snapshot_name):
    ok, result = Snapshots.delete_snapshot(snapshot_name)
    if not ok:
        return jsonify({"error": result}), 400
    return jsonify({"status": "deleted", "snapshot": snapshot_name})


# ── Auto scaler routes ───────────────────────────────────

_scaler_thread = None

@app.route("/autoscaler/status", methods=["GET"])
@jwt_required()
def autoscaler_status():
    policy  = AutoScaler.load_policy()
    running = _scaler_thread is not None and _scaler_thread.is_alive()
    allocated_cpus, allocated_memory = get_allocated_resources()
    cpu_pct = round((allocated_cpus   / AutoScaler.TOTAL_CPUS)   * 100, 1)
    ram_pct = round((allocated_memory / AutoScaler.TOTAL_MEMORY)  * 100, 1)

    return jsonify({
        "running":      running,
        "policy":       policy,
        "current_usage": {
            "cpu_pct": cpu_pct,
            "ram_pct": ram_pct,
            "vm_count": AutoScaler.count_running_vms(),
        }
    })


@app.route("/autoscaler/start", methods=["POST"])
@admin_required
def autoscaler_start():
    global _scaler_thread
    if _scaler_thread and _scaler_thread.is_alive():
        return jsonify({"status": "already running"})

    AutoScaler._stop_event.clear()
    _scaler_thread = threading.Thread(
        target=AutoScaler.run, daemon=True
    )
    _scaler_thread.start()
    return jsonify({"status": "started"})


@app.route("/autoscaler/stop", methods=["POST"])
@admin_required
def autoscaler_stop():
    AutoScaler.stop()
    return jsonify({"status": "stopped"})


@app.route("/autoscaler/check", methods=["POST"])
@admin_required
def autoscaler_check():
    AutoScaler.check_and_scale()
    return jsonify({"status": "check complete"})


@app.route("/autoscaler/policy", methods=["GET"])
@jwt_required()
def autoscaler_get_policy():
    return jsonify(AutoScaler.load_policy())


@app.route("/autoscaler/policy", methods=["PUT"])
@admin_required
def autoscaler_set_policy():
    data   = request.get_json()
    policy = AutoScaler.load_policy()

    allowed = set(AutoScaler.DEFAULT_POLICY.keys())
    for key, value in data.items():
        if key not in allowed:
            return jsonify({"error": f"Unknown policy key '{key}'"}), 400
        original = AutoScaler.DEFAULT_POLICY[key]
        if isinstance(original, int):
            policy[key] = int(value)
        elif isinstance(original, float):
            policy[key] = float(value)
        else:
            policy[key] = value

    AutoScaler.save_policy(policy)
    return jsonify({"status": "updated", "policy": policy})

@app.route("/autoscaler/log", methods=["GET"])
@jwt_required()
def autoscaler_log():
    lines = []
    if os.path.exists(AutoScaler.LOG_FILE):
        with open(AutoScaler.LOG_FILE) as f:
            lines = f.readlines()[-50:]   # last 50 lines
        lines = [l.strip() for l in lines]
    return jsonify({"lines": lines})

# ── Metrics routes ───────────────────────────────────────

@app.route("/metrics", methods=["GET"])
@admin_required          # was @jwt_required()
def metrics_all_latest():
    """Latest stats for every running VM."""
    return jsonify({
        "metrics":   Metrics.get_all_latest(),
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/metrics/<vm_name>", methods=["GET"])
@admin_required          # was @jwt_required()
def metrics_vm(vm_name):
    """Full history for a specific VM."""
    points  = int(request.args.get("points", 60))
    history = Metrics.get_vm_metrics(vm_name, points)
    latest  = history[-1] if history else None
    return jsonify({
        "vm_name": vm_name,
        "points":  len(history),
        "latest":  latest,
        "history": history,
    })


@app.route("/metrics/<vm_name>/latest", methods=["GET"])
@admin_required          # was @jwt_required()
def metrics_vm_latest(vm_name):
    """Single latest data point for a VM."""
    latest = Metrics.get_vm_latest(vm_name)
    if not latest:
        return jsonify({"error": f"No metrics for VM '{vm_name}'"}), 404
    return jsonify(latest)


@app.route("/metrics/cluster/history", methods=["GET"])
@admin_required          # was @jwt_required()
def metrics_cluster_history():
    """Aggregated cluster metrics over time."""
    points  = int(request.args.get("points", 60))
    history = Metrics.get_cluster_history(points)
    return jsonify({
        "points":  len(history),
        "history": history,
    })
    
@app.route("/auth/password", methods=["PUT"])
@jwt_required()
def change_password():
    data     = request.get_json()
    username = get_jwt_identity()

    if not data or "old_password" not in data or "new_password" not in data:
        return jsonify({"error": "Missing old_password or new_password"}), 400

    ok, result = Users.verify_user(username, data["old_password"])
    if not ok:
        return jsonify({"error": "Current password incorrect"}), 401

    # Update password
    import bcrypt
    hashed = bcrypt.hashpw(
        data["new_password"].encode("utf-8"),
        bcrypt.gensalt()
    ).decode("utf-8")

    import json as _json
    users_data = Users._load()
    users_data["users"][username]["password"] = hashed
    Users._save(users_data)

    return jsonify({"status": "password changed"})

# ── Start metrics engine on boot ─────────────────────────
_metrics_thread = threading.Thread(target=Metrics.run, daemon=True)
_metrics_thread.start()

# ── Storage routes ────────────────────────────────────────

@app.route("/storage/upload", methods=["POST"])
@jwt_required()
def storage_upload():
    owner = get_jwt_identity()

    if "file" not in request.files:
        return jsonify({"error": "No file provided. Use form field 'file'"}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    file_id, result = Storage.save_file(
        f, f.filename, owner,
        content_type=f.content_type or "application/octet-stream"
    )

    if file_id is None:
        return jsonify({"error": result}), 400

    meta = Storage.get_file_meta(file_id)
    return jsonify({
        "status": "uploaded",
        "file":   meta,
        "download_url": f"/storage/file/{file_id}",
    }), 201


@app.route("/storage/file/<file_id>", methods=["GET"])
@jwt_required(locations=["headers", "query_string"])
def storage_download(file_id):
    owner = get_jwt_identity()
    path, meta_or_err = Storage.get_file_path(file_id, owner)

    if path is None:
        return jsonify({"error": meta_or_err}), 404

    meta = meta_or_err
    return send_file(
        path,
        mimetype=meta["content_type"],
        as_attachment=False,
        download_name=meta["filename"]
    )

@app.route("/storage/list", methods=["GET"])
@jwt_required()
def storage_list():
    owner = get_jwt_identity()
    files = Storage.list_files(owner)
    usage = Storage.get_user_usage_mb(owner)

    return jsonify({
        "count":         len(files),
        "usage_mb":      usage,
        "quota_mb":      Storage.MAX_USER_QUOTA_MB,
        "usage_pct":     round((usage / Storage.MAX_USER_QUOTA_MB) * 100, 1),
        "files":         files,
    })


@app.route("/storage/file/<file_id>", methods=["DELETE"])
@jwt_required()
def storage_delete(file_id):
    owner      = get_jwt_identity()
    ok, result = Storage.delete_file(file_id, owner)

    if not ok:
        return jsonify({"error": result}), 404 if "not found" in result.lower() else 403

    return jsonify({"status": "deleted", "file_id": file_id})


@app.route("/storage/stats", methods=["GET"])
@admin_required
def storage_stats_route():
    return jsonify(Storage.storage_stats())
    

 

# ── Run ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n  MyCloud API v2.0 starting...")
    print("  Auth: JWT enabled")
    print("  Listening on http://0.0.0.0:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=True)
