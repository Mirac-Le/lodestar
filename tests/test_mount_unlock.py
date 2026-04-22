"""Per-mount web UI password (一人一库 / 切 tab 必重输).

These tests exercise the only auth surface the SPA can hit before
unlocking — the root `/api/mounts` listing — plus the per-mount
`/api/unlock` challenge and HMAC-token verification on data endpoints.

The "切 tab 必重输" guarantee is a frontend property (full-page navigate
between mounts drops in-memory state). Here we just verify the *backend*
half of that contract: a token minted under mount A is rejected on
mount B's endpoints.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lodestar.config import Settings
from lodestar.db import Repository, connect, init_schema
from lodestar.models import Person
from lodestar.web import create_app


def _make_db(path: Path, *, display_name: str, password: str | None = None) -> None:
    conn = connect(path)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    repo.display_name = display_name
    repo.ensure_me(name=display_name)
    repo.add_person(Person(name=f"{display_name}-Friend"))
    if password is not None:
        repo.set_web_password(password)
    conn.close()


@pytest.fixture
def two_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Two mounts: ``richard`` (locked) and ``tommy`` (open)."""
    rich = tmp_path / "richard.db"
    tommy = tmp_path / "tommy.db"
    _make_db(rich, display_name="Richard", password="r-secret")
    _make_db(tommy, display_name="Tommy", password=None)

    settings = Settings(
        db_path=rich, embedding_dim=8,
        llm_api_key="x", embedding_api_key="x",
    )
    monkeypatch.setattr("lodestar.config.get_settings", lambda: settings)
    monkeypatch.setattr("lodestar.web.app.get_settings", lambda: settings)
    monkeypatch.setenv(
        "LODESTAR_MOUNTS_JSON",
        json.dumps([
            {"slug": "richard", "db_path": str(rich)},
            {"slug": "tommy", "db_path": str(tommy)},
        ]),
    )
    yield TestClient(create_app())


def test_mounts_list_surfaces_lock_state(two_mounts: TestClient) -> None:
    r = two_mounts.get("/api/mounts")
    assert r.status_code == 200
    data = r.json()
    by_slug = {m["slug"]: m for m in data["mounts"]}
    assert by_slug["richard"]["locked"] is True
    assert by_slug["richard"]["display_name"] == "Richard"
    assert by_slug["tommy"]["locked"] is False
    # default_slug is just a UX hint — must be one of the configured mounts
    assert data["default_slug"] in by_slug


def test_open_mount_skips_password(two_mounts: TestClient) -> None:
    """A mount with no password set must serve data without challenge,
    even if the SPA forgot to call /api/unlock first. Otherwise local
    single-user setups would have an unnecessary friction step."""
    r = two_mounts.get("/r/tommy/api/graph")
    assert r.status_code == 200


def test_locked_mount_rejects_without_token(two_mounts: TestClient) -> None:
    r = two_mounts.get("/r/richard/api/graph")
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert detail["code"] == "mount_locked"
    assert detail["slug"] == "richard"


def test_unlock_bad_password_returns_401(two_mounts: TestClient) -> None:
    r = two_mounts.post("/r/richard/api/unlock", json={"password": "wrong"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "bad_password"


def test_unlock_good_password_grants_token(two_mounts: TestClient) -> None:
    r = two_mounts.post("/r/richard/api/unlock", json={"password": "r-secret"})
    assert r.status_code == 200
    token = r.json()["token"]
    assert token
    g = two_mounts.get(
        "/r/richard/api/graph", headers={"X-Mount-Unlock": token}
    )
    assert g.status_code == 200


def test_token_does_not_cross_mounts(two_mounts: TestClient) -> None:
    """Token minted for /r/richard must NOT unlock /r/other (even another
    locked mount). The HMAC bakes the slug in, so this is a backend-side
    invariant — not just frontend discipline."""
    # Need a 2nd LOCKED mount to actually exercise the slug check; the
    # default fixture has tommy open, so we can't use it. Use richard's
    # token against tommy: tommy is open (no auth), so the cross-mount
    # property is best demonstrated by a separate test below.
    rich_token = two_mounts.post(
        "/r/richard/api/unlock", json={"password": "r-secret"}
    ).json()["token"]
    # Garbage token also rejected — sanity check the verifier.
    bad = two_mounts.get(
        "/r/richard/api/graph", headers={"X-Mount-Unlock": "garbage"}
    )
    assert bad.status_code == 401
    # Real token still works on its own mount.
    ok = two_mounts.get(
        "/r/richard/api/graph", headers={"X-Mount-Unlock": rich_token}
    )
    assert ok.status_code == 200


def test_token_from_one_locked_mount_rejected_by_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slug-binding test that two_mounts can't do (because tommy is
    open). Boots two LOCKED mounts and verifies cross-mount tokens 401."""
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    _make_db(a, display_name="A", password="aaa")
    _make_db(b, display_name="B", password="bbb")

    settings = Settings(
        db_path=a, embedding_dim=8,
        llm_api_key="x", embedding_api_key="x",
    )
    monkeypatch.setattr("lodestar.config.get_settings", lambda: settings)
    monkeypatch.setattr("lodestar.web.app.get_settings", lambda: settings)
    monkeypatch.setenv(
        "LODESTAR_MOUNTS_JSON",
        json.dumps([
            {"slug": "alpha", "db_path": str(a)},
            {"slug": "beta", "db_path": str(b)},
        ]),
    )
    client = TestClient(create_app())

    a_tok = client.post("/r/alpha/api/unlock", json={"password": "aaa"}).json()["token"]
    b_tok = client.post("/r/beta/api/unlock", json={"password": "bbb"}).json()["token"]

    assert client.get(
        "/r/alpha/api/graph", headers={"X-Mount-Unlock": a_tok}
    ).status_code == 200
    assert client.get(
        "/r/beta/api/graph", headers={"X-Mount-Unlock": b_tok}
    ).status_code == 200
    # Cross-mount: each token MUST be rejected on the other mount.
    assert client.get(
        "/r/alpha/api/graph", headers={"X-Mount-Unlock": b_tok}
    ).status_code == 401
    assert client.get(
        "/r/beta/api/graph", headers={"X-Mount-Unlock": a_tok}
    ).status_code == 401
