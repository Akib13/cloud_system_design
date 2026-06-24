#!/usr/bin/env python3
"""
User store — manages users, passwords, roles.
Backed by a JSON file. Passwords are bcrypt hashed.
"""

import json
import os
import bcrypt
from datetime import datetime
from threading import Lock

USERS_FILE = os.path.expanduser("~/cloud-system/users.json")
_lock      = Lock()


def _load():
    if not os.path.exists(USERS_FILE):
        return {"users": {}}
    with open(USERS_FILE, "r") as f:
        return json.load(f)


def _save(data):
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, USERS_FILE)


# ── Public API ───────────────────────────────────────────

def create_user(username, password, role="user"):
    """Create a new user. Roles: admin, user."""
    with _lock:
        data = _load()
        if username in data["users"]:
            return False, "User already exists"

        hashed = bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt()
        ).decode("utf-8")

        data["users"][username] = {
            "username":   username,
            "password":   hashed,
            "role":       role,
            "created_at": datetime.now().isoformat(),
            "last_login": None,
        }
        _save(data)
        return True, "User created"


def verify_user(username, password):
    """Verify username + password. Returns (True, role) or (False, reason)."""
    data = _load()
    user = data["users"].get(username)
    if not user:
        return False, "User not found"

    match = bcrypt.checkpw(
        password.encode("utf-8"),
        user["password"].encode("utf-8")
    )
    if not match:
        return False, "Incorrect password"

    # Update last login
    with _lock:
        data = _load()
        data["users"][username]["last_login"] = datetime.now().isoformat()
        _save(data)

    return True, user["role"]


def get_user(username):
    data = _load()
    user = data["users"].get(username)
    if user:
        # Never return the password hash
        return {k: v for k, v in user.items() if k != "password"}
    return None


def list_users():
    data = _load()
    return [
        {k: v for k, v in u.items() if k != "password"}
        for u in data["users"].values()
    ]


def delete_user(username):
    with _lock:
        data = _load()
        if username not in data["users"]:
            return False, "User not found"
        del data["users"][username]
        _save(data)
        return True, "User deleted"


# ── Bootstrap default admin ──────────────────────────────

def ensure_default_admin():
    """Create default admin if no users exist yet."""
    data = _load()
    if not data["users"]:
        create_user("admin", "admin123", role="admin")
        print("[AUTH] Default admin created — username: admin, password: admin123")
        print("[AUTH] Change this password immediately in production!")
