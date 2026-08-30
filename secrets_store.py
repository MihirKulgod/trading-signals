"""
Zerodha credentials and per-day session state.

Two different kinds of value are deliberately kept apart:

* Credentials (api key/secret, user id, password, TOTP seed) are long-lived and
  sensitive. They live in the operating system keyring, never on disk in plain
  text. ``migrate_env_file`` imports them once from a legacy ``.env``.
* Session state (access token, login time) is regenerated daily and is written
  to a JSON file under the data directory. Keeping it out of the credential
  store means a token refresh can never rewrite the credentials -- the failure
  mode that previously truncated the whole ``.env`` file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import keyring
from keyring.errors import KeyringError

import app_paths
from app_logging import get_logger

log = get_logger(__name__)

SERVICE = "trading-signals"
CREDENTIAL_KEYS = ("KITE_API_KEY", "KITE_API_SECRET", "USER_ID", "PASSWORD", "TOTP_SECRET")
SESSION_KEYS = ("KITE_ACCESS_TOKEN", "KITE_LOGIN_TIME")

class CredentialsMissingError(RuntimeError):
    def __init__(self, missing):
        super().__init__(
            f"missing credential(s): {', '.join(missing)}; set them in the app's "
            "Settings page or via secrets_store.set_credential()"
        )
        self.missing = list(missing)

# --- credentials (keyring) --------------------------------------------------

def keyring_available() -> bool:
    try:
        return not isinstance(keyring.get_keyring(), keyring.backends.fail.Keyring)
    except Exception:
        return False

def get_credential(key: str) -> str | None:
    try:
        return keyring.get_password(SERVICE, key)
    except KeyringError as error:
        log.warning("keyring read failed for %s: %s", key, error)
        return None

def set_credential(key: str, value: str) -> None:
    keyring.set_password(SERVICE, key, value)

def delete_credential(key: str) -> None:
    try:
        keyring.delete_password(SERVICE, key)
    except KeyringError:
        pass

def require_credentials() -> dict:
    values = {key: get_credential(key) for key in CREDENTIAL_KEYS}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise CredentialsMissingError(missing)
    return values

def credential_status() -> dict:
    return {key: bool(get_credential(key)) for key in CREDENTIAL_KEYS}

# --- session state (json file) ----------------------------------------------

def read_session() -> dict:
    path = app_paths.session_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        log.warning("could not read session file: %s", error)
        return {}

def clear_session() -> None:
    try:
        app_paths.session_path().unlink()
    except FileNotFoundError:
        pass

def write_session(**values) -> None:
    path = app_paths.session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    session = read_session()
    session.update(values)
    path.write_text(json.dumps(session, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass

# --- one-time migration off .env --------------------------------------------

def migrate_env_file(env_path: Path | None = None) -> list:
    """Import credentials from a legacy .env into the keyring. Returns keys moved."""
    env_path = env_path or (app_paths.base_dir() / ".env")
    if not env_path.is_file():
        return []

    from dotenv import dotenv_values

    values = dotenv_values(env_path)
    moved = []
    for key in CREDENTIAL_KEYS:
        value = values.get(key)
        if value and not get_credential(key):
            set_credential(key, value)
            moved.append(key)

    session = {key: values[key] for key in SESSION_KEYS if values.get(key)}
    if session and not read_session():
        write_session(**session)

    if moved:
        log.info("migrated %d credential(s) from %s into the keyring", len(moved), env_path)
    return moved
