"""Smoke tests for the pyvis exporter."""

from pathlib import Path

from lodestar.db import Repository, connect, init_schema
from lodestar.models import (
    Frequency,
    PathResult,
    PathStep,
    Person,
    Relationship,
)
from lodestar.viz import GraphExporter, infer_industry


def _make_repo(tmp_path: Path) -> Repository:
    conn = connect(tmp_path / "viz.db")
    init_schema(conn, embedding_dim=8)
    return Repository(conn)


def test_infer_industry_buckets() -> None:
    p_invest = Person(name="A", tags=["私募基金"])
    label, color, _ = infer_industry(p_invest)
    assert label == "投资金融"
    assert color.startswith("#")

    p_tech = Person(name="B", companies=["腾讯"], bio="后端工程师，写AI")
    label2, _, _ = infer_industry(p_tech)
    assert label2 == "技术研发"

    p_other = Person(name="C")
    label3, _, _ = infer_industry(p_other)
    assert label3 == "其他"


def test_export_writes_html(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    me = repo.ensure_me("我")
    p = repo.add_person(Person(name="Alice", tags=["私募基金"]))
    assert me.id and p.id
    repo.add_relationship(
        Relationship(
            source_id=me.id,
            target_id=p.id,
            strength=4,
            frequency=Frequency.MONTHLY,
            context="老朋友",
        )
    )

    out = tmp_path / "out.html"
    exporter = GraphExporter(repo)
    written = exporter.export(out, title="Test")
    assert written.exists()
    text = written.read_text(encoding="utf-8")
    assert "Alice" in text
    assert "ls-header" in text and "LODESTAR" in text
    assert "ls-search" in text


def test_export_highlights_path(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    me = repo.ensure_me("我")
    p = repo.add_person(Person(name="Bob", tags=["券商"]))
    assert me.id and p.id
    repo.add_relationship(
        Relationship(source_id=me.id, target_id=p.id, strength=5)
    )

    fake = PathResult(
        target=p,
        path=[
            PathStep(person_id=me.id, name=me.name),
            PathStep(person_id=p.id, name=p.name, strength=5),
        ],
        relevance_score=0.9,
        path_strength=5.0,
        combined_score=1.8,
        rationale="test",
    )
    out = tmp_path / "highlighted.html"
    GraphExporter(repo).export(out, highlighted=[fake], title="Goal")
    text = out.read_text(encoding="utf-8")
    assert "Top Paths" in text
    assert "Bob" in text
