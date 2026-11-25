"""Tiny thread-safe JSON document store on SQLite (projects, clips, jobs, uploads)."""
import json
import math
import re
import sqlite3
import threading
import time
import uuid

from .config import DATA

INDEXED = ("project_id", "status", "clip_id", "kind", "longform_id")  # the fields find() is mostly matched on
_db = sqlite3.connect(DATA / "reelwright.db", check_same_thread=False)
_db.execute("PRAGMA journal_mode=WAL")  # readers never wait on a writer; one fsync per checkpoint, not per write
_db.execute("PRAGMA synchronous=NORMAL")  # with WAL: still crash-safe for the app, only a power cut can lose the last writes
_db.execute("CREATE TABLE IF NOT EXISTS docs (kind TEXT, id TEXT, created REAL, data TEXT, PRIMARY KEY (kind, id))")
_db.execute("DROP INDEX IF EXISTS docs_created")  # ponytail: with it the planner sorts by date instead of using a field index
for _f in INDEXED:
    _db.execute(f"CREATE INDEX IF NOT EXISTS docs_{_f} ON docs (kind, json_extract(data, '$.{_f}'))")
_db.commit()
_lock = threading.RLock()
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def put(kind: str, doc: dict) -> dict:
    doc.setdefault("id", new_id())
    doc.setdefault("created", time.time())
    with _lock:
        _db.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?)", (kind, doc["id"], doc["created"], json.dumps(doc)))
        _db.commit()
    return doc


def get(kind: str, id: str) -> dict | None:
    with _lock:
        row = _db.execute("SELECT data FROM docs WHERE kind=? AND id=?", (kind, id)).fetchone()
    return json.loads(row[0]) if row else None


def _sql_value(v) -> bool:
    """A match value SQLite compares the way Python's == does (else find() filters it in Python only)."""
    if v is None or isinstance(v, bool):
        return True
    if isinstance(v, int):
        return -2 ** 63 <= v < 2 ** 63
    if isinstance(v, float):
        return math.isfinite(v)
    if isinstance(v, str):
        try:
            v.encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate can't be bound
            return False
        return True
    return False


def find(kind: str, /, **match) -> list[dict]:  # positional-only so docs can be matched on a "kind" field
    """All docs of a kind (newest first) whose fields equal every given match value. Scalar matches narrow the rows in
    SQL (json_extract, indexed for INDEXED); the Python check after it keeps the result exactly == semantics."""
    where, args = ["kind=?"], [kind]
    for k, v in match.items():
        if _NAME.fullmatch(k) and _sql_value(v):
            where.append(f"json_extract(data, '$.{k}') IS NULL" if v is None else f"json_extract(data, '$.{k}') = ?")
            args += [] if v is None else [v]
    with _lock:
        rows = _db.execute(f"SELECT data FROM docs WHERE {' AND '.join(where)} ORDER BY created DESC", args).fetchall()
    docs = [json.loads(r[0]) for r in rows]
    return [d for d in docs if all(d.get(k) == v for k, v in match.items())]


def fields(kind: str, *names: str) -> list[tuple]:
    """(id, *those fields' values) for every doc of a kind, newest first, without parsing whole docs (missing = None)."""
    assert all(_NAME.fullmatch(n) for n in names)
    cols = "".join(f", data -> '$.{n}'" for n in names)  # -> gives each value as JSON text: loads() returns it exactly
    with _lock:
        rows = _db.execute(f"SELECT id{cols} FROM docs WHERE kind=? ORDER BY created DESC", (kind,)).fetchall()
    return [(r[0], *(None if v is None else json.loads(v) for v in r[1:])) for r in rows]


def newest(kind: str, n: int) -> list[dict]:
    """The n newest docs of a kind (find(kind)[:n] without parsing the rest)."""
    with _lock:
        rows = _db.execute("SELECT data FROM docs WHERE kind=? ORDER BY created DESC LIMIT ?", (kind, n)).fetchall()
    return [json.loads(r[0]) for r in rows]


def update(kind: str, id: str, patch: dict | None = None, fn=None) -> dict | None:
    """Read-modify-write under the lock so worker threads and API calls don't clobber each other."""
    with _lock:
        doc = get(kind, id)
        if doc is None:
            return None
        if patch:
            doc.update(patch)
        if fn:
            fn(doc)
        return put(kind, doc)


def delete(kind: str, id: str) -> None:
    with _lock:
        _db.execute("DELETE FROM docs WHERE kind=? AND id=?", (kind, id))
        _db.commit()


JOB_DAYS = 7
JOB_KEEP = ("import_url", "proxy")  # storage.start reads old import_url jobs' links; a failed proxy job stops Editor retries


def prune_jobs(days: float = JOB_DAYS) -> int:
    """Delete finished (done / failed / canceled) jobs whose last activity is older than `days`; queued and running
    jobs are never touched. Returns how many went."""
    cutoff = time.time() - days * 86400
    last = ("max(created, coalesce(json_extract(data, '$.started'), 0), coalesce(json_extract(data, '$.finished'), 0))")
    with _lock:
        n = _db.execute(f"DELETE FROM docs WHERE kind='job' AND json_extract(data, '$.status') IN ('done', 'failed', 'canceled') "
                        f"AND coalesce(json_extract(data, '$.kind'), '') NOT IN ({','.join('?' * len(JOB_KEEP))}) AND {last} < ?",
                        (*JOB_KEEP, cutoff)).rowcount
        _db.commit()
    return n
