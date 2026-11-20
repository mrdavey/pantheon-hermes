"""Stop and check in at a verified native tool or turn boundary."""

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from . import api, permissions, state


def call(connection, session, operation, body=None):
    project, member = connection.split(".")
    return api.request(
        "POST", f"/projects/{project}/members/{member}/{operation}",
        {"session": session, **(body or {})},
    )


def key(session):
    return state.digest(session)


def focused(session):
    return state.get("project_work", key(session))


def focus(connection, session, enabled):
    with state.local_guard("project-focus"):
        current = focused(session)
        if not enabled and (not current or current["connection"] != connection):
            return {"focused": False}
        status = call(connection, session, "status")
        project, assignment = status["project"], status.get("assignment")
        if not enabled:
            if assignment and assignment["state"] in {"running", "waiting", "paused"}:
                raise api.APIError("project_assignment_active")
            state.delete("project_work", key(session))
            return {"focused": False}
        if not status.get("workHooks") or not assignment or project["closed"]:
            raise api.APIError("project_hooks_unavailable")
        if current and current["connection"] != connection:
            raise api.APIError("project_focus_conflict")
        if not current:
            state.put("project_work", key(session), {"connection": connection})
        return {"focused": True, "connection": connection}


def clear_focus(connection, session):
    with state.local_guard("project-focus"):
        row = focused(session)
        if row and row["connection"] == connection:
            state.delete("project_work", key(session))


def require_focus(connection, session):
    row = focused(session)
    if row and row["connection"] != connection:
        raise api.APIError("project_focus_conflict")
    if not row and call(connection, session, "status").get("workHooks"):
        raise api.APIError("project_focus_required")


def observe_receipt(connection, session, result):
    row = focused(session)
    if not row or row["connection"] != connection:
        return
    if result.get("next") == "hand_back":
        clear_focus(connection, session)
    else:
        with state.local_guard("project-focus"):
            row = focused(session)
            if row and row["connection"] == connection:
                state.update("project_work", key(session), {"next": result.get("next")})


def deadline_reached(assignment, now):
    if assignment["state"] != "running":
        return False
    due = datetime.fromisoformat(assignment["checkInDue"].replace("Z", "+00:00"))
    return now >= due


def gate(session, tool_name, now=None):
    # Control and reporting must remain available while other actions are fenced.
    if tool_name == "pantheon_project":
        return None
    row = focused(session)
    if not row:
        return None
    connection = row["connection"]
    try:
        status = call(connection, session, "status")
        project, assignment = status["project"], status.get("assignment")
        if not status.get("workHooks"):
            return "Project work is unavailable. Wait for your human."
        if project["closed"] or project["boardPaused"] or not assignment:
            return "Project work is paused. Check Pantheon status."
        if assignment["state"] == "running" and assignment["objectiveVersion"] == project["objectiveVersion"]:
            if deadline_reached(assignment, now or datetime.now(timezone.utc)):
                return f"Check in with pantheon_project for {connection} before continuing."
            return None
        return "Your assignment has no authorized running task. Check Pantheon status."
    except (api.APIError, KeyError, TypeError, ValueError):
        return "Project authorization is unavailable. Wait before starting another action."


def before_finish(session, attempt=0, now=None):
    """Use the host's verification hook once per unchanged assignment state."""
    row = focused(session)
    if not row:
        return None
    connection = row["connection"]
    try:
        status = call(connection, session, "status")
        project, assignment = status["project"], status.get("assignment")
        if not assignment:
            return None
        # All synchronous native tool calls have yielded at this boundary.
        # This receipt confirms the host stopped starting actions, while any
        # external effects remain held for a separate owner review.
        if assignment["state"] in {"paused", "stopped"} and assignment["controlStatus"] == "pending":
            request = str(uuid5(NAMESPACE_URL, f"pantheon:{connection}:{assignment['id']}:{assignment['controlVersion']}"))
            call(connection, session, "work", {"command": {
                "op": "ack_control", "key": request, "assignmentId": assignment["id"],
                "expected": assignment["controlVersion"], "controlStatus": "confirmed",
            }})
            return None
        if (attempt != 0 or not status.get("workHooks") or project["closed"] or project["boardPaused"]
                or assignment["objectiveVersion"] != project["objectiveVersion"]):
            return None
        list_tasks = (row.get("next") == "list_tasks" and assignment["scope"] == "board"
                      and assignment["state"] == "waiting" and not assignment["blocker"])
        if assignment["state"] != "running" and not list_tasks:
            return None
        version = [assignment["id"], assignment["version"]]
        with state.local_guard("project-focus"):
            current = focused(session)
            if not current or current["connection"] != connection or current.get("verified_version") == version:
                return None
            state.update("project_work", key(session), {"verified_version": version})
        if list_tasks:
            return f"Read pantheon_project status for {connection} and list eligible tasks before deciding to wait or complete."
        if deadline_reached(assignment, now or datetime.now(timezone.utc)):
            return f"Check in with pantheon_project for {connection}, then continue the authorized task."
        return f"Read pantheon_project status for {connection}. The same task is still assigned. Continue or report its blocker."
    except api.APIError as error:
        if error.code == "access_unavailable":
            try:
                receipt = call(connection, session, "stop_receipt")
                if receipt["status"] != "confirmed":
                    call(connection, session, "stop_receipt", {
                        "assignmentId": receipt["assignmentId"], "version": receipt["version"],
                    })
            except (api.APIError, KeyError, TypeError):
                pass
        return None
    except (KeyError, TypeError, ValueError):
        return None


def runnable(status, row):
    p, a = status["project"], status.get("assignment")
    if (not status.get("workHooks") or not a or a["objectiveVersion"] != p["objectiveVersion"]
            or p["closed"] or p["boardPaused"] or a.get("blocker")):
        return None
    if a["state"] == "running" or a["state"] == "waiting" and row.get("next") == "list_tasks":
        return [a["id"], a["version"], p["boardRevision"]]
    return None


def continue_once(ctx, session, connection, stop):
    # Only after a captured native limit, under the human's existing native
    # injection approval. Never claim/replay a task from a background hook.
    if stop.is_set() or permissions.current() is not True:
        return
    try:
        with state.local_guard("project-focus"):
            row = focused(session)
            if not row or row["connection"] != connection:
                return
            version = runnable(call(connection, session, "status"), row)
            if not version or row.get("continuation_version") == version:
                return
            state.update("project_work", key(session), {"continuation_version": version})
        if stop.is_set() or permissions.current() is not True:
            return
        current = focused(session)
        if not current or current["connection"] != connection or runnable(call(connection, session, "status"), current) != version:
            return
        if not stop.is_set() and permissions.current() is True:
            ctx.inject_message(
                "Pantheon project work reached a native turn limit. Read current controls for "
                + connection + " and resume its verified checkpoint if work is still authorized.",
                session_key=session["route"],
            )
    except (api.APIError, KeyError, TypeError, ValueError):
        return  # An uncertain native enqueue remains fenced by the recorded version.


def wait_for_tasks(ctx, session, connection, stop, idle):
    while not stop.is_set() and permissions.current() is True:
        row = focused(session)
        if not row or row["connection"] != connection or row.get("next") != "wait_for_tasks":
            return
        if not idle():
            stop.wait(3)
            continue
        try:
            notice = call(connection, session, "wait_work")
            if not notice.get("assignmentId"):
                continue
            while not idle():
                if stop.wait(3) or permissions.current() is not True or focused(session) != row:
                    return
            status = call(connection, session, "status")
            head, assignment = status["project"], status.get("assignment")
            if (assignment and assignment["id"] == notice["assignmentId"] and assignment["version"] == notice["version"]
                    and assignment["waitForTasks"] and status.get("workHooks") and not head["boardPaused"]
                    and not head["closed"] and idle() and not stop.is_set() and permissions.current() is True):
                ctx.inject_message(
                    "Eligible work appeared for the Pantheon project " + connection
                    + ". Read current status and list tasks before claiming anything.",
                    session_key=session["route"],
                )
            return
        except api.APIError as error:
            if error.code in {"access_unavailable", "conflict", "closed", "sign_in_required"}:
                return
            stop.wait(5)
