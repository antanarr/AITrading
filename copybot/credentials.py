"""Read only requested credentials through the local macOS Keychain helper.

Credential values are captured in memory. Never include them in logs, exceptions,
command arguments, or the returned status dictionary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = (
    Path.home()
    / "Library/Application Support/AITrading/AITrading Credentials.app/Contents/MacOS/AITradingCredentials"
)
NAMES = (
    "BIRDEYE_API_KEY",
    "NANSEN_API_KEY",
    "SOLANA_RPC_URL",
    "OPENAI_API_KEY",
    "COPYBOT_OPENAI_MODEL",
)


class CredentialError(ValueError):
    pass


def _invoke(arguments):
    try:
        return subprocess.run(
            [str(HELPER), *arguments],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CredentialError("Keychain helper unavailable or access was not completed") from None


def load_credentials(names):
    if any(name not in NAMES for name in names):
        raise CredentialError("Unknown credential name")
    if sys.platform != "darwin" or not HELPER.is_file():
        return
    for name in names:
        # Explicit environment / .env values continue to take precedence.
        if os.getenv(name):
            continue
        result = _invoke(["--read", name])
        if result.returncode == 44:
            continue
        if result.returncode != 0:
            raise CredentialError(
                f"Keychain access failed for {name}; credentials were not printed"
            )
        try:
            value = result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise CredentialError(f"Invalid Keychain value for {name}") from None
        if not value or any(c in value for c in "\n\r\0"):
            raise CredentialError(f"Invalid Keychain value for {name}")
        os.environ[name] = value


def credential_status():
    if sys.platform != "darwin" or not HELPER.is_file():
        return {"keychain_helper_ready": False}
    result = _invoke(["--status"])
    if result.returncode != 0:
        raise CredentialError("Keychain status unavailable")
    try:
        raw = json.loads(result.stdout)
        if (
            not isinstance(raw, dict)
            or set(raw) != set(NAMES)
            or any(type(v) is not bool for v in raw.values())
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise CredentialError("Invalid Keychain status response") from None
    return {"keychain_helper_ready": True, "saved_in_keychain": raw}


def open_setup():
    if sys.platform != "darwin":
        raise CredentialError("The credential window requires macOS")
    try:
        subprocess.run(
            [str(ROOT / "Open Credential Setup.command")],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        raise CredentialError("Could not open the credential window") from None
