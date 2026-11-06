"""Lazy profile-scoped, nonsecret SQLite records; no invitation or token storage."""
import contextlib
import hashlib
import json
import time
import uuid


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextlib.contextmanager
def database():
    from plugins.plugin_storage import plugin_db
    db = plugin_db("pantheon")
    try:
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(kind,id))")
        db.commit()
        yield db
    finally:
        db.close()


def rows(kind):
    with database() as db:
        return [json.loads(row[0]) for row in db.execute("SELECT body FROM records WHERE kind=? ORDER BY id LIMIT 1001", (kind,))]


def get(kind, key):
    with database() as db:
        row = db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None


def put(kind, key, value):
    with database() as db, db:
        db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?)", (kind, key, json.dumps(value)))


def update(kind, key, changes):
    with database() as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if row is None:
            return None
        value = json.loads(row[0]); value.update(changes)
        db.execute("UPDATE records SET body=? WHERE kind=? AND id=?", (json.dumps(value), kind, key))
        return value


def intent(kind, session, grant, payload_digest):
    """Atomically reuse a pending identical request, with only its digest retained."""
    key = digest([kind, session, grant, payload_digest])
    with database() as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT body FROM records WHERE kind='intent' AND id=?", (key,)).fetchone()
        if row:
            return json.loads(row[0])
        count = db.execute("SELECT COUNT(*) FROM records WHERE kind='intent'").fetchone()[0]
        if count >= 1000:
            raise ValueError("local_operation_limit")
        value = {"id": key, "operation": uuid.uuid4().hex, "kind": kind, "session": session,
                 "grant": grant, "digest": payload_digest, "created": time.time(), "state": "prepared"}
        db.execute("INSERT INTO records VALUES ('intent',?,?)", (key, json.dumps(value)))
        return value


def claim_wake(connection, signature):
    """Persist before injection: a crash is uncertain, never silently reinjected."""
    with database() as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT body FROM records WHERE kind='connection' AND id=?", (connection,)).fetchone()
        if not row:
            return False
        value = json.loads(row[0])
        if value.get("wake") == signature or value.get("wake_state") in {"attempted", "accepted"}:
            return False
        value.update(wake=signature, wake_state="attempted")
        db.execute("UPDATE records SET body=? WHERE kind='connection' AND id=?", (json.dumps(value), connection))
        return True


def cleanup_intent(connection, requested=None):
    with database() as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT body FROM records WHERE kind='connection' AND id=?", (connection,)).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        if value.get("state") == "disconnected":
            return value
        value.setdefault("cleanup_operation", requested or uuid.uuid4().hex)
        value["state"] = "cleanup_pending"
        db.execute("UPDATE records SET body=? WHERE kind='connection' AND id=?", (json.dumps(value), connection))
        return value


def delete(kind, key):
    with database() as db, db:
        db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, key))
