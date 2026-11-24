"""Tiny thread-safe JSON document store on SQLite (projects, clips, jobs, uploads)."""
import json
import sqlite3
import threading
import time
import uuid

from .config import DATA

_db = sqlite3.connect(DATA / "reelwright.db", check_same_thread=False)
_db.execute("CREATE TABLE IF NOT EXISTS docs (kind TEXT, id TEXT, created REAL, data TEXT, PRIMARY KEY (kind, id))")
_db.commit()
_lock = threading.RLock()


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


def find(kind: str, /, **match) -> list[dict]:  # positional-only so docs can be matched on a "kind" field
    """All docs of a kind (newest first) whose fields equal every given match value."""
    with _lock:
        rows = _db.execute("SELECT data FROM docs WHERE kind=? ORDER BY created DESC", (kind,)).fetchall()
    docs = [json.loads(r[0]) for r in rows]
    return [d for d in docs if all(d.get(k) == v for k, v in match.items())]


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
