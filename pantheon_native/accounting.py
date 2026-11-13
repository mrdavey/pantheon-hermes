"""Numeric public-hook usage only. Never infer attribution from an open connection."""
import json
from . import state


def begin(session, turn_id, user_message, parent_session_id=''):
    if not isinstance(turn_id, str) or not turn_id or parent_session_id:
        return
    # Only our exact accepted, content-free wake establishes a known task turn.
    # Human turns and mixed/steered input remain unattributed.
    candidates = [row for row in state.rows('connection') if isinstance(user_message, str) and row['session'] == session
                  and row.get('state') in {'connected', 'waiting'}
                  and row.get('usage_lineage') and not row.get('usage_blocked')
                  and row.get('wake_state') == 'accepted'
                  and row.get('wake_text_digest') == state.digest(user_message)]
    key = state.digest([session, turn_id])
    existing = state.get('usage_turn', key)
    if len(candidates) != 1:
        if existing:
            state.update('usage_turn', key, {'connection': None})
        return
    state.put('usage_turn', key, {'connection': candidates[0]['id']})


def record(session, turn_id, request_id, usage):
    if not all(isinstance(value, str) and value for value in (turn_id, request_id)):
        return
    turn = state.get('usage_turn', state.digest([session, turn_id]))
    if not turn or not turn.get('connection'):
        return
    connection = turn['connection']
    row = state.get('connection', connection)
    if not row or row['session'] != session or row.get('state') not in {'connected', 'waiting'} or not row.get('usage_lineage') or row.get('usage_blocked'):
        return
    values = (usage.get('prompt_tokens'), usage.get('output_tokens')) if isinstance(usage, dict) else (None, None)
    valid = all(type(value) is int and 0 <= value <= 10**12 for value in values)
    event = state.digest([session, request_id])
    with state.database() as db, db:
        db.execute('BEGIN IMMEDIATE')
        latest = db.execute("SELECT body FROM records WHERE kind='connection' AND id=?", (connection,)).fetchone()
        if not latest:
            return
        row = json.loads(latest[0])
        if row['session'] != session or row.get('state') not in {'connected', 'waiting'} or not row.get('usage_lineage') or row.get('usage_blocked'):
            return
        if db.execute("SELECT 1 FROM records WHERE kind='usage_event' AND id=?", (event,)).fetchone():
            return
        # A hard local bound fails accounting closed rather than evicting dedupe
        # records and accidentally counting replayed events twice.
        if db.execute("SELECT count(*) FROM records WHERE kind='usage_event'").fetchone()[0] >= 100000:
            return
        saved = db.execute("SELECT body FROM records WHERE kind='usage' AND id=?", (connection,)).fetchone()
        total = json.loads(saved[0]) if saved else {'input_tokens': 0, 'output_tokens': 0, 'measured': False, 'lineage': row['usage_lineage']}
        if (saved and total.get('lineage') != row['usage_lineage']) or (not saved and row.get('usage_started')):
            row['usage_blocked'] = 'counter_reset'
            db.execute("UPDATE records SET body=? WHERE kind='connection' AND id=?", (json.dumps(row), connection))
            return
        row['usage_started'] = True
        db.execute("UPDATE records SET body=? WHERE kind='connection' AND id=?", (json.dumps(row), connection))
        if valid and all(total[name] + count <= 10**12 for name, count in zip(('input_tokens', 'output_tokens'), values)):
            total['input_tokens'] += values[0]
            total['output_tokens'] += values[1]
            total['measured'] = True
        else:
            total['missing'] = True
        db.execute("INSERT INTO records VALUES ('usage_event',?,?)", (event, '{}'))
        db.execute("INSERT OR REPLACE INTO records VALUES ('usage',?,?)", (connection, json.dumps(total)))


def snapshot(connection):
    row = state.get('connection', connection) or {}
    total = state.get('usage', connection) or {}
    if not row.get('usage_lineage') or row.get('usage_blocked'):
        return {'availability': 'unavailable', 'reason': row.get('usage_blocked', 'attribution_unavailable'), 'source': 'host'}
    if (total and total.get('lineage') != row['usage_lineage']) or (not total and row.get('usage_started')):
        return {'availability': 'unavailable', 'reason': 'counter_reset', 'source': 'host'}
    # Setup/human turns and unobserved auxiliary work cannot be proven complete.
    # Report the measured subset explicitly; do not pretend an entire chat costs zero.
    result = {'availability': 'partial' if total.get('measured') else 'unavailable',
              'reason': 'partial_counters' if total.get('measured') else 'attribution_unavailable',
              'source': 'host'}
    if total.get('measured'):
        result['usage'] = {key: total[key] for key in ('input_tokens', 'output_tokens')}
    return result


def freeze(connection, key, payload):
    """Persist only digest and numeric metadata, never report text or credentials."""
    identity = state.digest([connection, key])
    fingerprint = state.digest(payload)
    current = snapshot(connection)
    with state.database() as db, db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute("SELECT body FROM records WHERE kind='usage_report' AND id=?", (identity,)).fetchone()
        if prior:
            result = json.loads(prior[0])
            if result['digest'] != fingerprint:
                raise ValueError('report_key_payload_conflict')
            return result['accounting']
        if 'usage' in payload:
            saved = db.execute("SELECT body FROM records WHERE kind='connection' AND id=?", (connection,)).fetchone()
            if saved:
                row = json.loads(saved[0]); row['usage_blocked'] = 'attribution_unavailable'
                db.execute("UPDATE records SET body=? WHERE kind='connection' AND id=?", (json.dumps(row), connection))
            current = {'availability': 'partial', 'reason': 'attribution_unavailable', 'source': 'model', 'usage': payload['usage']}
        db.execute("INSERT INTO records VALUES ('usage_report',?,?)", (identity, json.dumps({'digest': fingerprint, 'accounting': current})))
        return current
