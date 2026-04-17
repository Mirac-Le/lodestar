"""CSV import using Polars.

Expected schema (column order does not matter; extra columns are ignored):

    name           : required. Person's name.
    bio            : optional freeform background description.
    notes          : optional private notes.
    tags           : optional, semicolon-separated.
    skills         : optional, semicolon-separated.
    companies      : optional, semicolon-separated.
    cities         : optional, semicolon-separated.
    needs          : optional, semicolon-separated. What this person is seeking.
    strength       : optional int 1-5, defaults to 3 (relationship to me).
    context        : optional freeform context (e.g. "college roommate").
    frequency      : optional one of weekly/monthly/quarterly/yearly/rare.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from lodestar.db.repository import Repository
from lodestar.models import Frequency, Person, Relationship

_LIST_SEPARATOR = ";"


class CSVImporter:
    """Reads a CSV and upserts each row as a Person + optional Relationship(me, person)."""

    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    def import_file(self, path: str | Path) -> int:
        me = self._repo.get_me()
        if me is None or me.id is None:
            raise RuntimeError("No 'me' record. Run `lodestar init` first.")

        df = pl.read_csv(path, infer_schema_length=0)
        if "name" not in df.columns:
            raise ValueError("CSV must contain a 'name' column.")

        count = 0
        for row in df.iter_rows(named=True):
            name = str(row.get("name", "")).strip()
            if not name:
                continue
            person = Person(
                name=name,
                bio=_or_none(row.get("bio")),
                notes=_or_none(row.get("notes")),
                tags=_split(row.get("tags")),
                skills=_split(row.get("skills")),
                companies=_split(row.get("companies")),
                cities=_split(row.get("cities")),
                needs=_split(row.get("needs")),
            )
            existing = self._repo.find_person_by_name(name)
            if existing and existing.id is not None:
                person.id = existing.id
                saved = self._repo.update_person(person)
            else:
                saved = self._repo.add_person(person)

            strength = _parse_int(row.get("strength"), default=3)
            frequency = _parse_frequency(row.get("frequency"))
            context = _or_none(row.get("context"))
            assert saved.id is not None
            self._repo.add_relationship(
                Relationship(
                    source_id=me.id,
                    target_id=saved.id,
                    strength=strength,
                    context=context,
                    frequency=frequency,
                )
            )
            count += 1
        return count


def _or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _split(value: object) -> list[str]:
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(_LIST_SEPARATOR) if part.strip()]


def _parse_int(value: object, default: int) -> int:
    try:
        return int(str(value).strip()) if value is not None else default
    except (ValueError, AttributeError):
        return default


def _parse_frequency(value: object) -> Frequency:
    if value is None:
        return Frequency.YEARLY
    text = str(value).strip().lower()
    try:
        return Frequency(text)
    except ValueError:
        return Frequency.YEARLY
