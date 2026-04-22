"""In-process registry of background enrichment jobs (one-db-per-mount).

Why not Celery / RQ: the web app is single-process uvicorn and the
expected fanout is "one user clicks 批量 AI 解析, waits a couple of
minutes". A worker thread per job + a dict guarded by a lock is more
than enough and adds zero deployment moving parts.

Each job:

  * runs in a worker thread that opens its OWN sqlite connection — the
    request thread's `Repository` is not shareable across threads.
  * publishes progress to a `JobState` dataclass that the polling
    `GET /api/enrich/status/{task_id}` endpoint reads.
  * is mount-keyed: a second start while one is still RUNNING for the
    same mount slug returns the existing task_id rather than spawning a
    parallel one (avoids racing the same rows twice).

The job registry is process-local and not persisted; if the server
restarts mid-job, the client will see "unknown task_id" and can simply
re-trigger from the UI.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from lodestar.config import get_settings
from lodestar.db import Repository, connect, init_schema
from lodestar.enrich import L1Extractor, LLMClient, LLMError

_log = logging.getLogger(__name__)


JobStatus = Literal["pending", "running", "done", "error"]


@dataclass
class JobState:
    task_id: str
    mount_slug: str
    db_path: str
    status: JobStatus = "pending"
    only_missing: bool = True
    total: int = 0
    processed: int = 0
    touched: int = 0
    errors: int = 0
    current_name: str | None = None
    error_message: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "mount_slug": self.mount_slug,
            "status": self.status,
            "only_missing": self.only_missing,
            "total": self.total,
            "processed": self.processed,
            "touched": self.touched,
            "errors": self.errors,
            "current_name": self.current_name,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": (
                (self.finished_at or time.time()) - self.started_at
            ),
        }


_lock = threading.Lock()
_jobs: dict[str, JobState] = {}
# Mount slug → task_id of currently running (or pending) job, if any.
_mount_running: dict[str, str] = {}


def get(task_id: str) -> JobState | None:
    with _lock:
        return _jobs.get(task_id)


def list_jobs() -> list[JobState]:
    with _lock:
        return list(_jobs.values())


def start(
    *,
    mount_slug: str,
    db_path: str | Path,
    only_missing: bool = True,
) -> JobState:
    """Start (or join) a background enrichment job for ``mount_slug``.

    Returns the JobState. If a job is already running for this mount,
    the existing JobState is returned untouched so the caller can poll
    the same task_id.
    """
    db_path_str = str(db_path)
    with _lock:
        existing_id = _mount_running.get(mount_slug)
        if existing_id and (st := _jobs.get(existing_id)):
            if st.status in ("pending", "running"):
                return st
            _mount_running.pop(mount_slug, None)

        task_id = uuid.uuid4().hex[:12]
        state = JobState(
            task_id=task_id,
            mount_slug=mount_slug,
            db_path=db_path_str,
            only_missing=only_missing,
        )
        _jobs[task_id] = state
        _mount_running[mount_slug] = task_id

    thread = threading.Thread(
        target=_run, args=(task_id,), name=f"enrich-{task_id}", daemon=True
    )
    thread.start()
    return state


# ---------------------------------------------------------------- internals
def _run(task_id: str) -> None:
    state = get(task_id)
    if state is None:
        return

    conn = None
    try:
        settings = get_settings()
        # Worker thread MUST own its connection — the request-scope
        # Repository is bound to a connection on the FastAPI thread pool.
        conn = connect(Path(state.db_path))
        init_schema(conn, embedding_dim=settings.embedding_dim)
        repo = Repository(conn)

        try:
            client = LLMClient()
        except LLMError as exc:
            _finish(state, status="error", error_message=str(exc))
            return

        extractor = L1Extractor(repo, client=client)
        people = repo.list_people()
        with _lock:
            state.total = len(people)
            state.status = "running"

        def _on_progress(idx: int, total: int, current_name: str) -> None:
            with _lock:
                state.processed = idx
                state.current_name = current_name

        results = extractor.run(
            only_missing=state.only_missing, progress_cb=_on_progress
        )
        touched = extractor.apply(results)
        errors = sum(1 for r in results if r.error)

        with _lock:
            state.touched = touched
            state.errors = errors

        _finish(state, status="done")
    except Exception as exc:  # noqa: BLE001
        _log.exception("enrich job %s failed", task_id)
        _finish(state, status="error", error_message=str(exc))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _finish(
    state: JobState, *, status: JobStatus, error_message: str | None = None
) -> None:
    with _lock:
        state.status = status
        state.finished_at = time.time()
        if error_message:
            state.error_message = error_message
        # Clear mount-running ONLY if it still points at us (defensive
        # against race with a re-trigger that already kicked us out).
        if _mount_running.get(state.mount_slug) == state.task_id:
            _mount_running.pop(state.mount_slug, None)
