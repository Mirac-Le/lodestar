"""CSV import tests."""

from __future__ import annotations

from pathlib import Path

from lodestar.db import Repository
from lodestar.importers import CSVImporter


def test_import_csv_creates_people_and_relationships(
    repo: Repository, tmp_path: Path
) -> None:
    repo.ensure_me(name="Me")
    csv_path = tmp_path / "contacts.csv"
    csv_path.write_text(
        "name,bio,tags,skills,companies,cities,strength,context,frequency\n"
        "Alice,ex-Google PM,investor;tech,product,Google,Beijing,5,college friend,monthly\n"
        "Bob,,designer,,OpenAI,Shanghai,3,,yearly\n",
        encoding="utf-8",
    )

    importer = CSVImporter(repo)
    count = importer.import_file(csv_path)
    assert count == 2

    alice = repo.find_person_by_name("Alice")
    assert alice is not None
    assert "investor" in alice.tags
    assert "Google" in alice.companies

    rels = repo.list_relationships()
    assert len(rels) == 2
    alice_rel = next(r for r in rels if r.target_id == alice.id)
    assert alice_rel.strength == 5
