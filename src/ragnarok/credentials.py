from __future__ import annotations

import os
import re

import keyring
from keyring.errors import KeyringError, PasswordDeleteError


SERVICE_NAME = "ragnarok-eval"


class CredentialError(RuntimeError):
    pass


def get_stored_credential(credential_id: str) -> str | None:
    try:
        return keyring.get_password(SERVICE_NAME, credential_id)
    except KeyringError as exc:
        raise CredentialError(f"the operating-system credential store is unavailable: {exc}") from exc


def store_credential(credential_id: str, secret: str) -> None:
    if not secret:
        raise CredentialError("an empty credential cannot be stored")
    try:
        keyring.set_password(SERVICE_NAME, credential_id, secret)
    except KeyringError as exc:
        raise CredentialError(f"the operating-system credential store rejected the credential: {exc}") from exc


def delete_credential(credential_id: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, credential_id)
    except PasswordDeleteError:
        pass
    except KeyringError as exc:
        raise CredentialError(f"the operating-system credential store could not delete the credential: {exc}") from exc


def resolve_credential(credential_id: str | None, environment_name: str | None = None) -> str | None:
    if environment_name and os.getenv(environment_name):
        return os.environ[environment_name]
    if credential_id:
        generic_name = "RAGNAROK_CREDENTIAL_" + re.sub(r"[^A-Za-z0-9]+", "_", credential_id).upper()
        if os.getenv(generic_name):
            return os.environ[generic_name]
        return get_stored_credential(credential_id)
    return None
