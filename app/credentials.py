"""Where the passwords live.

- Windows / desktop Linux / macOS: the OS keyring (Windows Credential Manager, Secret Service,
  Keychain) via `keyring` — never in the DB or the repo.
- A Linux server with no keyring service: data/credentials.json, readable only by this user
  (chmod 600). It's outside the repo (data/ is git-ignored); protect the server account itself.
"""
import json
import os
import threading

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from .db import DATA_DIR

SERVICE = "kids-homework-moe"
FILE = DATA_DIR / "credentials.json"
_lock = threading.Lock()


def _key(child_id: int) -> str:
    return f"child-{child_id}"


def _keyring_usable() -> bool:
    backend = keyring.get_keyring()
    return getattr(backend, "priority", 0) > 0 and "fail" not in type(backend).__module__


def backend_name() -> str:
    return "keyring" if _keyring_usable() else "file"


def _read_file() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_file(data: dict):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf8") as f:
        json.dump(data, f)
    os.replace(tmp, FILE)
    try:
        os.chmod(FILE, 0o600)
    except OSError:
        pass


def set_password(child_id: int, password: str):
    if _keyring_usable():
        try:
            keyring.set_password(SERVICE, _key(child_id), password)
            return
        except KeyringError:
            pass
    with _lock:
        data = _read_file()
        data[_key(child_id)] = password
        _write_file(data)


def get_password(child_id: int):
    if _keyring_usable():
        try:
            value = keyring.get_password(SERVICE, _key(child_id))
            if value is not None:
                return value
        except KeyringError:
            pass
    with _lock:
        return _read_file().get(_key(child_id))


def delete_password(child_id: int):
    if _keyring_usable():
        try:
            keyring.delete_password(SERVICE, _key(child_id))
        except (PasswordDeleteError, KeyringError):
            pass
    with _lock:
        data = _read_file()
        if data.pop(_key(child_id), None) is not None:
            _write_file(data)
