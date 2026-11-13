"""Lazy profile-scoped, nonsecret SQLite records; no invitation or token storage."""
import contextlib
import threading
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


def intent(kind, session, grant, payload_digest, lifetime=None, usage_lineage=None):
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
        if lifetime is not None:
            value['lifetime'] = lifetime
        if usage_lineage is not None:
            value['usage_lineage'] = digest([value['operation'], usage_lineage])
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


def restore_receiver(connection, session, grant):
    """A concurrent Disconnect or rebind wins over an in-flight live check."""
    with database() as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT body FROM records WHERE kind='connection' AND id=?", (connection,)).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        if (value['session'] != session or value['grant'] != grant or value.get('state') in
                {'disconnected', 'revoked', 'expired', 'closed', 'cleanup_pending'}):
            return None
        value['state'] = 'connected'
        db.execute("UPDATE records SET body=? WHERE kind='connection' AND id=?", (json.dumps(value), connection))
        return value


def terminal_window(connection):
    """The first terminal observation fixes the cleanup bound across processes."""
    with database() as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT body FROM records WHERE kind='connection' AND id=?", (connection,)).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        value.setdefault('terminal_deadline', time.time() + 60)
        db.execute("UPDATE records SET body=? WHERE kind='connection' AND id=?", (json.dumps(value), connection))
        return value


def delete(kind, key):
    with database() as db, db:
        db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, key))


_permission_depth = threading.local()


@contextlib.contextmanager
def local_guard(name):
    """Serialize one plugin operation across processes in this profile."""
    import sqlite3
    with database() as db:
        path = db.execute('PRAGMA database_list').fetchone()[2] + '.' + name
    held = getattr(_permission_depth, 'paths', ())
    if path in held:
        yield
        return
    # A separate database avoids holding our records writer while network calls
    # execute. No config, credentials or message content is stored in this lock.
    lock = sqlite3.connect(path, timeout=30)
    _permission_depth.paths = (*held, path)
    try:
        lock.execute('CREATE TABLE IF NOT EXISTS guard (id INTEGER PRIMARY KEY)')
        lock.commit()
        lock.execute('BEGIN IMMEDIATE')
        yield
        lock.commit()
    finally:
        _permission_depth.paths = held
        lock.close()


def question(connection, args, context):
    """Claim one native prompt without persisting the question or its answer."""
    from .api import APIError
    identity = args.get('id')
    action = args.get('state')
    if identity is not None and (not identity.strip() or action is None):
        raise APIError('invalid_question')
    if identity is None and (action is not None or 'context_version' in args):
        raise APIError('invalid_question')
    if identity and action == 'pending' and args.get('context_version') != context['context_version']:
        raise APIError('question_context_changed')
    with database() as db, db:
        db.execute('BEGIN IMMEDIATE')
        records = db.execute("SELECT id,body FROM records WHERE kind='question'").fetchall()
        pending = []
        found = None
        for key, raw in records:
            row = json.loads(raw)
            if row['connection'] != connection:
                continue
            if row['state'] == 'pending' and (row['context_version'] != context['context_version'] or row['config'] != context['config']):
                row['state'] = 'cancelled'
                db.execute("UPDATE records SET body=? WHERE kind='question' AND id=?", (json.dumps(row), key))
            if row['id'] == identity:
                found = row
            if row['state'] == 'pending':
                pending.append(row)
        if identity is None:
            return {'pending': pending, 'should_prompt': False}
        key = digest([connection, identity])
        if found:
            if action != 'pending':
                found['state'] = action
                db.execute("UPDATE records SET body=? WHERE kind='question' AND id=?", (json.dumps(found), key))
            return {**found, 'should_prompt': False}
        if action != 'pending':
            raise APIError('question_not_found')
        if len(records) >= 1000:
            raise APIError('question_limit')
        found = {'connection': connection, 'id': identity, 'state': 'pending',
                 'context_version': context['context_version'], 'config': context['config']}
        db.execute("INSERT INTO records VALUES ('question',?,?)", (key, json.dumps(found)))
        return {**found, 'should_prompt': True}


def write_preconditions(connection, name, args, current):
    """An uncertain report/opening keeps the original versions, key and digest."""
    identity = digest([connection, args['key']])
    fingerprint = digest([name, args])
    with database() as db, db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute("SELECT body FROM records WHERE kind='write' AND id=?", (identity,)).fetchone()
        if prior:
            value = json.loads(prior[0])
            if value['digest'] != fingerprint:
                from .api import APIError
                raise APIError('write_key_payload_conflict')
            return value['versions']
        versions = {key: current[key] for key in ('epoch', 'config')}
        db.execute("INSERT INTO records VALUES ('write',?,?)", (identity, json.dumps({'digest': fingerprint, 'versions': versions})))
        return versions


def cancel_questions(connection):
    with database() as db, db:
        db.execute('BEGIN IMMEDIATE')
        for key, raw in db.execute("SELECT id,body FROM records WHERE kind='question'").fetchall():
            row = json.loads(raw)
            if row['connection'] == connection and row['state'] == 'pending':
                row['state'] = 'cancelled'
                db.execute("UPDATE records SET body=? WHERE kind='question' AND id=?", (json.dumps(row), key))


def permission_guard():
    return local_guard('permission-gate')


def report_guard(connection):
    return local_guard('report-' + digest(connection))


def report_pending(connection, key, *, mark=False):
    from .api import APIError
    with database() as db, db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute("SELECT body FROM records WHERE kind='report_pending' AND id=?", (connection,)).fetchone()
        if prior and json.loads(prior[0])['key'] != key:
            raise APIError('prior_report_pending')
        if mark:
            db.execute("INSERT OR REPLACE INTO records VALUES ('report_pending',?,?)", (connection, json.dumps({'key': key})))


def report_resolved(connection, key):
    with database() as db, db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute("SELECT body FROM records WHERE kind='report_pending' AND id=?", (connection,)).fetchone()
        if prior and json.loads(prior[0])['key'] == key:
            db.execute("DELETE FROM records WHERE kind='report_pending' AND id=?", (connection,))
