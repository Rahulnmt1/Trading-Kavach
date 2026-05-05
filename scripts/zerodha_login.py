"""Zerodha Kite Connect — daily TOTP-based access-token bootstrap.

SEBI requires a fresh daily login for retail algo trading. This script automates
the user-flow login (username + password + TOTP) and exchanges the resulting
`request_token` for an `access_token`, which it writes back into your `.env`.

Required environment variables (.env):
    KITE_API_KEY        - Your Kite Connect API key
    KITE_API_SECRET     - Your Kite Connect API secret
    KITE_USER_ID        - Zerodha login ID (e.g. AB1234)
    KITE_PASSWORD       - Kite login password
    KITE_TOTP_SECRET    - Base-32 secret seed shown when 2FA was set up

Usage:
    python -m scripts.zerodha_login

The script uses only `requests` + `pyotp` for the user-flow steps and
KiteConnect for the final token exchange. It is the single point in the system
that touches your Zerodha credentials.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import pyotp
import requests

from bot.config import PROJECT_ROOT, env
from bot.logger import logger

KITE_BASE = "https://kite.zerodha.com"


def _login(session: requests.Session, user_id: str, password: str) -> str:
    r = session.post(
        f"{KITE_BASE}/api/login",
        data={"user_id": user_id, "password": password},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Login failed: {data}")
    request_id = data["data"]["request_id"]
    return request_id


def _twofa(session: requests.Session, user_id: str, request_id: str, totp_code: str) -> None:
    r = session.post(
        f"{KITE_BASE}/api/twofa",
        data={
            "user_id": user_id,
            "request_id": request_id,
            "twofa_value": totp_code,
            "twofa_type": "totp",
            "skip_session": "",
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"TOTP step failed: {data}")


def _get_request_token(session: requests.Session, api_key: str) -> str:
    """Hits the Connect login endpoint; redirect URL contains `request_token=...`."""
    r = session.get(
        f"{KITE_BASE}/connect/login",
        params={"api_key": api_key, "v": "3"},
        allow_redirects=False,
        timeout=15,
    )
    while r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers.get("Location", "")
        parsed = urlparse(loc)
        qs = parse_qs(parsed.query)
        if "request_token" in qs:
            return qs["request_token"][0]
        r = session.get(loc if loc.startswith("http") else f"{KITE_BASE}{loc}",
                        allow_redirects=False, timeout=15)
    raise RuntimeError("Did not receive a request_token in the redirect chain.")


def _persist_access_token(token: str) -> None:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        env_file.write_text(f"KITE_ACCESS_TOKEN={token}\n")
        return
    text = env_file.read_text()
    if re.search(r"^KITE_ACCESS_TOKEN=.*$", text, re.MULTILINE):
        text = re.sub(r"^KITE_ACCESS_TOKEN=.*$", f"KITE_ACCESS_TOKEN={token}", text, flags=re.MULTILINE)
    else:
        text += f"\nKITE_ACCESS_TOKEN={token}\n"
    env_file.write_text(text)


def login() -> Optional[str]:
    e_ = env()
    missing = [name for name, val in {
        "KITE_API_KEY": e_.KITE_API_KEY,
        "KITE_API_SECRET": e_.KITE_API_SECRET,
        "KITE_USER_ID": e_.KITE_USER_ID,
        "KITE_PASSWORD": e_.KITE_PASSWORD,
        "KITE_TOTP_SECRET": e_.KITE_TOTP_SECRET,
    }.items() if not val]
    if missing:
        logger.error("[login] missing env vars: {}", ", ".join(missing))
        return None

    try:
        from kiteconnect import KiteConnect
    except ImportError:
        logger.error("[login] kiteconnect not installed. `pip install kiteconnect`")
        return None

    session = requests.Session()
    logger.info("[login] step 1/3 — username + password")
    request_id = _login(session, e_.KITE_USER_ID, e_.KITE_PASSWORD)

    totp_code = pyotp.TOTP(e_.KITE_TOTP_SECRET).now()
    logger.info("[login] step 2/3 — TOTP")
    _twofa(session, e_.KITE_USER_ID, request_id, totp_code)

    logger.info("[login] step 3/3 — request_token + access_token exchange")
    request_token = _get_request_token(session, e_.KITE_API_KEY)

    kite = KiteConnect(api_key=e_.KITE_API_KEY)
    data = kite.generate_session(request_token, api_secret=e_.KITE_API_SECRET)
    access_token = data["access_token"]

    _persist_access_token(access_token)
    os.environ["KITE_ACCESS_TOKEN"] = access_token
    logger.info("[login] success — access_token written to .env (valid until tomorrow ~06:00 IST)")
    return access_token


if __name__ == "__main__":
    sys.exit(0 if login() else 1)
