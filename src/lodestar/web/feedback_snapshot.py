"""Build a scrubbed db snapshot for feedback tickets.

Given a list of ``involved_person_ids``, return each person's Person row,
their Me-edge (if any), and their 1-hop neighbors. All free-text PII is
scrubbed via ``lodestar.privacy.scrub``.

出发点：让 AI 拿到 ticket md 时直接看到"这个联系人的 bio/tags/关系强度
都是什么"，省去反查 db 的一轮。
"""

from __future__ import annotations

from typing import Any

from lodestar.db import Repository
from lodestar.privacy import scrub


def build_snapshot(
    repo: Repository,
    involved_person_ids: list[int],
) -> list[dict[str, Any]]:
    people = {p.id: p for p in repo.list_people() if p.id is not None}
    rels = repo.list_relationships()
    me = repo.get_me()
    me_id = me.id if me else None

    out: list[dict[str, Any]] = []
    for pid in involved_person_ids:
        p = people.get(pid)
        if p is None:
            out.append({"person": None, "missing_id": pid})
            continue

        me_edge = None
        if me_id is not None:
            for r in rels:
                if {r.source_id, r.target_id} == {me_id, pid}:
                    me_edge = {
                        "strength": r.strength,
                        "frequency": r.frequency.value if r.frequency else None,
                        "context": scrub(r.context),
                    }
                    break

        # 1 跳邻居（排除 me 自己，避免和 me_edge 重复）
        neighbors: list[dict[str, Any]] = []
        for r in rels:
            if pid not in (r.source_id, r.target_id):
                continue
            other = r.target_id if r.source_id == pid else r.source_id
            if other == me_id or other not in people:
                continue
            neighbors.append({
                "id": other,
                "name": people[other].name,
                "strength": r.strength,
                "frequency": r.frequency.value if r.frequency else None,
            })

        out.append({
            "person": {
                "id": p.id,
                "name": p.name,
                "bio": scrub(p.bio),
                "notes": scrub(p.notes),
                "tags": p.tags,
                "skills": p.skills,
                "companies": p.companies,
                "cities": p.cities,
                "is_wishlist": p.is_wishlist,
            },
            "me_edge": me_edge,
            "neighbors": neighbors,
        })
    return out
