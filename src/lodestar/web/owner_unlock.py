"""Per-owner web UI lock: password hash + short-lived HMAC unlock tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException

from lodestar.config import get_settings
from lodestar.models import Owner

TOKEN_TTL_SEC = 7 * 24 * 3600
_PBKDF2_ITERS = 480_000


def unlock_secret_bytes() -> bytes:
    """Secret for signing `X-Owner-Unlock` tokens.

    If ``LODESTAR_OWNER_UNLOCK_SECRET`` is unset, derives from ``db_path``
    (fine for single-machine local use; set an explicit secret for multi-host).
    """
    s = get_settings()
    raw = (s.owner_unlock_secret or "").strip()
    if raw:
        return raw.encode("utf-8")
    return hashlib.sha256(str(s.db_path.resolve()).encode()).digest()


def hash_web_password(plain: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", plain.encode("utf-8"), salt, _PBKDF2_ITERS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_web_password(plain: str, stored: str | None) -> bool:
    if not stored or not plain:
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iters = int(parts[1])
        salt = bytes.fromhex(parts[2])
        want = bytes.fromhex(parts[3])
    except ValueError:
        return False
    if iters != _PBKDF2_ITERS:
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", plain.encode("utf-8"), salt, _PBKDF2_ITERS
    )
    return hmac.compare_digest(dk, want)


def mint_unlock_token(slug: str, secret: bytes) -> str:
    exp = int(time.time()) + TOKEN_TTL_SEC
    msg = f"{slug}:{exp}".encode()
    sig = hmac.new(secret, msg, hashlib.sha256).digest()
    raw = msg + b"." + sig
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def verify_unlock_token(slug: str, token: str | None, secret: bytes) -> bool:
    if not token:
        return False
    pad = "=" * ((4 - (len(token) % 4)) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + pad)
        msg_b, sig = raw.rsplit(b".", 1)
        slug_t, exp_s = msg_b.decode("utf-8").split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    if slug_t != slug:
        return False
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    expect = hmac.new(secret, msg_b, hashlib.sha256).digest()
    return hmac.compare_digest(sig, expect)


def assert_owner_web_access(
    owner: Owner, unlock_token: str | None, secret: bytes
) -> None:
    h = owner.web_password_hash
    if not h:
        return
    if verify_unlock_token(owner.slug, unlock_token, secret):
        return
    raise HTTPException(
        status_code=401,
        detail={
            "code": "owner_locked",
            "slug": owner.slug,
            "message": "此网络已加锁，请先输入密码解锁。",
        },
    )
