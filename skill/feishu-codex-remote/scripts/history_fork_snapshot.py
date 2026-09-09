"""Read-only history import view for a confirmed paginated-fork rejection."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


def snapshot_rollout(parent_id: str, destination: Path, *, home: Path | None = None) -> Path:
    home = home or codex_home()
    with closing(sqlite3.connect((home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT rollout_path FROM threads WHERE id=?", (parent_id,)).fetchone()
    if row is None:
        raise ValueError("Fork source is not present in the local thread index")
    with Path(row[0]).open("rb") as handle:
        # Freeze the byte extent while the main turn may continue appending.
        raw = handle.read(os.fstat(handle.fileno()).st_size)
    records = raw[:raw.rfind(b"\n") + 1].splitlines(keepends=True)
    if not records:
        raise ValueError("Fork source has no complete records")
    meta = json.loads(records[0])
    payload = meta.get("payload", {}) if isinstance(meta, dict) else {}
    if not isinstance(payload, dict) or not isinstance(meta, dict) or meta.get("type") != "session_meta" or payload.get("id") != parent_id:
        raise ValueError("Fork source identity does not match the requested parent")
    if payload.get("history_base"):
        raise ValueError("Referenced history must be resolved before legacy import")
    for line in records[1:]:
        if not isinstance(json.loads(line), dict):
            raise ValueError("Fork source contains an invalid record")
    # Only copied storage metadata changes. Original records and DB are read-only.
    payload["history_mode"] = "legacy"
    records[0] = (json.dumps(meta, ensure_ascii=False) + "\n").encode("utf-8")
    data = b"".join(records)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / (hashlib.sha256(data).hexdigest() + "-" + uuid.uuid4().hex + ".jsonl")
    with target.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return target
