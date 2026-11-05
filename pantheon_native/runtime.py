"""Bounded content-free wakes in the captured native profile; no model loop."""
import contextvars
from datetime import datetime
import threading
from . import api, state, permissions


def deadline(row):
    return datetime.fromisoformat(row["deadline"].replace("Z", "+00:00")).timestamp()


class Runtime:
    def __init__(self, ctx):
        self.ctx = ctx
        self.lock = threading.Lock()
        self.workers = {}
        self.closed = False

    def start(self, row):
        key = (row["session"]["profile"], row["id"])
        with self.lock:
            if self.closed or row.get("state") in {"disconnected", "revoked"} or key in self.workers or len(self.workers) >= 20:
                return
            stop = threading.Event()
            captured = contextvars.copy_context()
            worker = threading.Thread(target=lambda: captured.run(self.run, row, stop), daemon=True,
                                      name="pantheon-receiver")
            self.workers[key] = stop
            worker.start()

    def stop(self):
        with self.lock:
            self.closed = True
            for stop in self.workers.values():
                stop.set()

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
                try:
                    pending = api.tool(row["id"], row["session"], "pantheon_receive", {"connection": row["id"]})
                    ids = [item["message"]["id"] for item in pending]
                    if ids and not stop.is_set() and time.time() < deadline(row):
                        signature = state.digest(ids)
                        if state.claim_wake(row["id"], signature):
                            accepted = self.ctx.inject_message(
                                "Pantheon update for connection " + row["id"] + ". Use pantheon_receive to read it. "
                                "Do not ACK until you read the returned messages. If stale, finish silently.",
                                session_key=row["session"]["route"])
                            state.update("connection", row["id"], {"wake_state": "accepted" if accepted else "unavailable"})
                except api.APIError as error:
                    if error.code in {"access_unavailable", "sign_in_required", "native_auth_unavailable"}:
                        state.update("connection", row["id"], {"state": "authorization_unavailable"})
                        permissions.restore()
                        return
                # Bounded by the original deadline; no background sign-in or turn spawning.
                stop.wait(min(5, max(0, deadline(row) - time.time())))
        finally:
            with self.lock:
                self.workers.pop((original["session"]["profile"], original["id"]), None)

    def observe(self, session):
        for row in state.rows("connection"):
            if row["session"] == session and row.get("state") not in {"disconnected", "revoked"}:
                self.start(row)

    def finish(self, session, terminal_only=True):
        for row in state.rows("connection"):
            if row["session"]["id"] == session["id"] and row["session"]["profile"] == session["profile"] and (not terminal_only or row.get("terminal_seen")):
                if row.get("state") != "disconnected":
                    self.disconnect(row)
