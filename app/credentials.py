"""Passwords live in the Windows Credential Manager (via keyring), never in the DB or on disk."""
import keyring

SERVICE = "kids-homework-moe"


def _key(child_id: int) -> str:
    return f"child-{child_id}"


def set_password(child_id: int, password: str):
    keyring.set_password(SERVICE, _key(child_id), password)


def get_password(child_id: int):
    return keyring.get_password(SERVICE, _key(child_id))


def delete_password(child_id: int):
    try:
        keyring.delete_password(SERVICE, _key(child_id))
    except keyring.errors.PasswordDeleteError:
        pass
