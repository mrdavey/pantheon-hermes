"""Bounded content-free wakes in the captured native profile; no model loop."""
import contextvars
from datetime import datetime
import threading
from . import api, state, permissions


def deadline(row):
    if 'runtime_policy' in row:
        if row['runtime_policy'] != {'mode': 'connection'} or row.get('deadline') not in (None, '', '0001-01-01T00:00:00Z'):
            raise api.APIError('invalid_runtime_policy')
        end = float('inf')
    else:
        end = datetime.fromisoformat(row["deadline"].replace("Z", "+00:00")).timestamp()
    return min(end, row.get('terminal_deadline', end))


class Runtime:
    def __init__(self, ctx):
        self.ctx = ctx
        self.lock = threading.Lock()
        self.workers = {}
        self.reopening = {}
        self.threads = set()
        self.active_turns = set()
        self.closed = False

    def turn(self, session, active):
        key = (session["profile"], session["id"])
        with self.lock:
            if active:
                self.active_turns.add(key)
            else:
                self.active_turns.discard(key)
        self.observe(session)

    def start(self, row):
        key = (row["session"]["profile"], row["id"])
        with self.lock:
            if self.closed or row.get("state") in {"disconnected", "revoked", "authorization_unavailable"} or key in self.workers or len(self.workers) >= 20:
                return
            stop = threading.Event()
            captured = contextvars.copy_context()
            worker = threading.Thread(target=lambda: captured.run(self.run, row, stop), daemon=True,
                                      name="pantheon-receiver")
            self.workers[key] = stop
            self.threads.add(worker)
            worker.start()

    def stop(self):
        with self.lock:
            self.closed = True
            for stop in self.workers.values():
                stop.set()
            for stop in self.reopening.values():
                stop.set()
            workers = list(self.threads)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=2)

    def schedule_reopen(self, session):
        """Bounded recovery outside the host hook; preserve its profile context."""
        key = (session['profile'], session['id'])
        with self.lock:
            if self.closed or key in self.reopening or len(self.reopening) >= 20:
                return
            stop = threading.Event()
            captured = contextvars.copy_context()
            def recover():
                try:
                    for attempt in range(5):
                        if stop.is_set() or self.reopen(session) is not False:
                            return
                        if stop.wait(2 ** (attempt + 1)):
                            return
                finally:
                    with self.lock:
                        self.reopening.pop(key, None)
                        self.threads.discard(threading.current_thread())
            worker = threading.Thread(target=lambda: captured.run(recover), daemon=True,
                                      name='pantheon-reopen')
            self.reopening[key] = stop
            self.threads.add(worker)
            worker.start()

    def reopen(self, session):
        """Recover only this native conversation, without join, sign-in or rebinding."""
        candidates = [row for row in state.rows('connection') if row['session'] == session
                      and row.get('state') not in {'disconnected', 'revoked', 'expired', 'closed', 'cleanup_pending'}]
        if not candidates or permissions.current() is not True:
            return
        try:
            grant = api.request('GET', '/identity')['grant_id']
        except api.APIError as error:
            return error.code not in {'outcome_unknown', 'unavailable', 'conflict'}
        complete = True
        for row in candidates:
            if row['grant'] != grant:
                continue  # A different OAuth grant needs deliberate owner resume.
            try:
                import time
                if time.time() >= deadline(row):
                    self.disconnect(row)
                    continue
                # The scoped tool checks the complete session, not just account ownership.
                api.tool(row['id'], session, 'pantheon_chat_state', {'connection': row['id']})
                restored = state.restore_receiver(row['id'], session, grant)
                if restored:
                    self.start(restored)
            except api.APIError as error:
                if error.code in {'outcome_unknown', 'unavailable', 'conflict'}:
                    complete = False
                continue  # One revoked connection does not strand other live chats.
        return complete

    def disconnect(self, row):
        # SQLite owns the cleanup identity across threads AND host processes.
        row = state.cleanup_intent(row["id"])
        if not row or row["state"] == "disconnected":
            return
        op = row["cleanup_operation"]
        try:
            out = api.request("POST", "/connections/" + api.handle(row["id"]) + "/disconnect", {"operation": op})
        except api.APIError:
            return
        if out.get("state") == "disconnected":
            state.update("connection", row["id"], {"state": "disconnected"})
            permissions.restore()

    def run(self, original, stop):
        import time
        try:
            while not stop.is_set():
                row = state.get("connection", original["id"])
                if not row or row.get("grant") != original["grant"] or row["session"] != original["session"] or row.get("state") in {"disconnected", "revoked"}:
                    return
                if time.time() >= deadline(row) or row.get("state") == "cleanup_pending":
                    self.disconnect(row)
                    return
                if permissions.current() is not True:
                    state.update("connection", row["id"], {"state": "incoming_disabled"})
                    return
                # Let the current native turn read/ACK its messages before deciding
                # whether an idle conversation needs a wake. No host busy-state API.
                with self.lock:
                    active = (row["session"]["profile"], row["session"]["id"]) in self.active_turns
                if active:
                    stop.wait(min(5, max(0, deadline(row) - time.time())))
                    continue
                try:
                    pending = api.tool(row["id"], row["session"], "pantheon_receive", {"connection": row["id"]})
                    if any(item['message'].get('kind') == 'terminal' for item in pending) and 'terminal_deadline' not in row:
                        row = state.terminal_window(row['id'])
                        if row is None:
                            return
                    ids = [item["message"]["id"] for item in pending]
                    if ids and not stop.is_set() and time.time() < deadline(row):
                        signature = state.digest(ids)
                        if state.claim_wake(row["id"], signature):
                            accepted = self.ctx.inject_message(
                                "Pantheon update for connection " + row["id"] + ". Use pantheon_receive to read it. "
                                "Do not ACK until you read the returned messages. If stale, finish silently. "
                                "For any visible status, copy this exact chat URL: " + row["url"],
                                session_key=row["session"]["route"])
                            state.update("connection", row["id"], {"wake_state": "accepted" if accepted else "unavailable"})
                except api.APIError as error:
                    if error.code in {"access_unavailable", "sign_in_required", "native_auth_unavailable"}:
                        status = "authorization_unavailable"
                        # A Desktop disconnect also denies agent tools. Read only
                        # management state to distinguish it from lost authorization.
                        try:
                            remote = api.request("GET", "/connections/" + api.handle(row["id"]))
                            if remote.get("state") == "disconnected":
                                status = "disconnected"
                        except api.APIError:
                            pass
                        state.update("connection", row["id"], {"state": status})
                        permissions.restore()
                        return
                # Bounded by the original deadline; no background sign-in or turn spawning.
                stop.wait(min(5, max(0, deadline(row) - time.time())))
        finally:
            with self.lock:
                self.workers.pop((original["session"]["profile"], original["id"]), None)
                self.threads.discard(threading.current_thread())

    def observe(self, session):
        for row in state.rows("connection"):
            if row["session"] == session and row.get("state") not in {"disconnected", "revoked"}:
                self.start(row)

    def finish(self, session, terminal_only=True):
        for row in state.rows("connection"):
            if row["session"]["id"] == session["id"] and row["session"]["profile"] == session["profile"] and (not terminal_only or row.get("terminal_seen")):
                if row.get("state") != "disconnected":
                    self.disconnect(row)
