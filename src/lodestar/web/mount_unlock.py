"""Per-mount web UI lock: PBKDF2 password verification + HMAC unlock tokens.

每个 mount（``/r/<slug>/``）独立存活：自己的 db、自己的 PBKDF2 hash
(``meta.web_password_hash`` / ``web_password_salt``)、自己的 HMAC
签名密钥（``meta.unlock_secret``，由 ``init_schema`` 在 db 创建时
随机生成一次后持久化）。token 上不带任何身份信息，只证明"有人在
TTL 内通过了某个 slug 的密码挑战"——所以 mount A 的 token 拿到
mount B 那边会被拒（slug 不匹配），并且任何 mount 删除/更换密码
都不需要旋转 token：换密码只影响下一次挑战，已发出的 token 仍然
按原 ``unlock_secret`` 校验。

切 tab 必输的语义在前端实现：每次 URL 进入新 mount 时，前端
会**只**向后端发该 mount 的 ``/api/unlock`` 拿一个新 token，
其它 mount 的 token 不复用、也不写到 localStorage——这样关 tab
或刷新 = 自动锁回去。后端只做无状态校验，对前端的"切 tab 是否
真的清了 token"无感（也无法假设）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import HTTPException

from lodestar.db.repository import Repository

TOKEN_TTL_SEC = 7 * 24 * 3600


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode(s + pad)


def mint_unlock_token(slug: str, secret: str) -> str:
    """Return a base64url(``slug:exp.HMAC``) token valid for ``TOKEN_TTL_SEC``."""
    exp = int(time.time()) + TOKEN_TTL_SEC
    msg = f"{slug}:{exp}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    return _b64url_encode(msg + b"." + sig)


def verify_unlock_token(slug: str, token: str | None, secret: str) -> bool:
    if not token:
        return False
    try:
        raw = _b64url_decode(token)
        msg_b, sig = raw.rsplit(b".", 1)
        slug_t, exp_s = msg_b.decode("utf-8").split(":", 1)
        exp = int(exp_s)
    except (ValueError, UnicodeDecodeError):
        return False
    if slug_t != slug:
        return False
    if exp < int(time.time()):
        return False
    expect = hmac.new(secret.encode("utf-8"), msg_b, hashlib.sha256).digest()
    return hmac.compare_digest(sig, expect)


def assert_mount_access(
    repo: Repository, slug: str, unlock_token: str | None
) -> None:
    """Raise 401 unless the mount is unlocked or the token is valid.

    A mount with no password set (``meta.web_password_hash is None``) is
    treated as unlocked for everyone — useful for trusted single-user
    machines that just want to skip the friction.
    """
    if not repo.web_password_hash:
        return
    if verify_unlock_token(slug, unlock_token, repo.unlock_secret):
        return
    raise HTTPException(
        status_code=401,
        detail={
            "code": "mount_locked",
            "slug": slug,
            "message": "此网络已加锁，请先输入密码解锁。",
        },
    )
