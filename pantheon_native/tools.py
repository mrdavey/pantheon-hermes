"""Fixed declared tools; no remote schema loading or executable descriptors."""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from . import api, files, permissions, state
from .session import observe

CATALOG = json.loads((Path(__file__).parent / "catalog.json").read_text())["tools"]
PRESENTATION = json.loads((Path(__file__).parent / "presentation.json").read_text())
SCHEMAS = {item["name"]: item for item in CATALOG}
FINAL_INSTRUCTION = (
    "Keep the chat link exact and describe only verified chat and cleanup facts. "
    "Connection cleanup does not establish task completion or validation success. "
    "Use returned incoming_replies values when present; do not infer prior settings from earlier conversation turns. "
    "The plugin manages restoration after the last connection; do not change it yourself or add a config check just to compose the response."
)


def validate(name, args):
    # The complete snapshot is checked by jsonschema at build/test time. Runtime
    # checks reject extra authority selectors before any API or filesystem call.
    schema = SCHEMAS[name]["inputSchema"]
    if not isinstance(args, dict) or set(args) - set(schema["properties"]) or set(schema.get("required", [])) - set(args):
        raise api.APIError("invalid_arguments")
    for key, value in args.items():
        spec = schema["properties"][key]
        kind = spec["type"]
        expected = {"string": str, "integer": int, "boolean": bool, "array": list}[kind]
        if not isinstance(value, expected) or (kind == "integer" and isinstance(value, bool)):
            raise api.APIError("invalid_arguments")
        if "enum" in spec and value not in spec["enum"]:
            raise api.APIError("invalid_arguments")
        if kind in {"string", "array"} and not spec.get("minItems", 0) <= len(value) <= spec.get("maxLength", spec.get("maxItems", 1_000_000)):
            raise api.APIError("invalid_arguments")
        if kind == "array" and any(not isinstance(item, str) for item in value):
            raise api.APIError("invalid_arguments")


def invitation(link):
    try:
        u = urlsplit(link)
        q = parse_qs(u.fragment or u.query, strict_parsing=True)
        if u.scheme != "https" or u.netloc != "pantheon.talk" or u.path != "/join" or (u.fragment and u.query) or set(q) != {"invite"} or len(q["invite"]) != 1:
            raise ValueError()
        value = q["invite"][0]
        if len(value) != 43 or not api.HANDLE.fullmatch(value):
            raise ValueError()
        return value
    except ValueError:
        raise api.APIError("invalid_invitation_link") from None


def identity():
    value = api.request("GET", "/identity")
    return api.handle(value["grant_id"])


def store_connection(result, session, grant):
    api.handle(result["id"])
    previous = state.get("connection", result["id"]) or {}
    # No returned authority or content is copied wholesale into local state.
    row = {key: result[key] for key in ("id", "chat", "member", "url", "deadline", "state")}
    row.update(session=session, grant=grant)
    for key in ("cleanup_operation", "wake", "wake_state"):
        if key in previous and previous.get("grant") == grant and previous.get("session") == session:
            row[key] = previous[key]
    state.put("connection", row["id"], row)
    return row


def recover(intent, session, grant):
    try:
        result = api.request("GET", "/operations/" + intent["operation"])
    except api.APIError as error:
        if error.code == "not_accepted":
            return None
        raise
    # Receipts prove acceptance, not current liveness. Never resurrect a closed
    # connection from the original join response.
    result = api.request("GET", "/connections/" + api.handle(result["id"]))
    row = store_connection(result, session, grant)
    state.update("intent", intent["id"], {"state": "accepted", "connection": row["id"]})
    return row


class Tools:
    def __init__(self, ctx, runtime):
        self.ctx, self.runtime = ctx, runtime

    def call(self, name, args, *, session_id=None, task_id=None, **kwargs):
        try:
            validate(name, args)
            session = observe(self.ctx, session_id)
            return json.dumps(self.dispatch(name, args, session), ensure_ascii=False)
        except api.APIError as error:
            result = {"ok": False, "error": error.code}
            if error.code in {"sign_in_required", "native_auth_unavailable"}:
                result["instruction"] = "Use native Hermes sign-in for this profile: hermes auth add pantheon:pantheon. This interactive sign-in is human-owned; never read or write auth.json."
            if error.code == "public_client_id_required":
                result["instruction"] = "Use native config in this profile: hermes config set PANTHEON_OAUTH_CLIENT_ID pantheon-hermes. Then use native sign-in. This client ID is public."
            if error.code in {"file_path_not_allowed", "session_workspace_required"}:
                result["instruction"] = "Uploads and site bundles require nonsecret files in this session's workspace. Downloads and durable ACKs require Pantheon's profile downloads directory, reported by pantheon_doctor. Never copy credentials, change the workspace, or use another tool to bypass these restrictions."
            return json.dumps(result)
        except Exception:
            return json.dumps({"ok": False, "error": "local_operation_failed", "instruction": "Use pantheon_doctor. Do not repeat an uncertain write with a new key."})

    def dispatch(self, name, args, session):
        if name == "pantheon_doctor":
            grant = identity()
            local = state.rows("connection")
            incoming = permissions.current()
            return {"ok": True, "signed_in": True, "incoming": incoming is True,
                    "incoming_setting": permissions.setting(incoming),
                    "connections": sum(row["grant"] == grant and row["session"] == session and row["state"] != "disconnected" for row in local),
                    "pending_operations": sum(row["session"] == session and row["grant"] == grant and row["state"] != "accepted" for row in state.rows("intent")),
                    "downloads_directory": str(files.download_root()),
                    "version": "1.0.8"}
        if name in {"pantheon_connect", "pantheon_resume"}:
            if args.get("deliberate") is not True:
                raise api.APIError("deliberate_invitation_required")
            grant = identity()
            if not permissions.ready():
                return {"ok": False, "error": "incoming_permission_required", "instruction":
                        "Explain visibly: Incoming replies let Pantheon deliver collaboration updates in this Hermes conversation while you wait. The previous setting will be restored after your last connection ends. Show the human this command to run in their selected profile: hermes config set plugins.entries.pantheon.allow_gateway_injection true. Alternatively, they can enable Pantheon's incoming-replies permission in Hermes Desktop. This is a human action: do not execute the command, modify the setting, or enable it through another tool yourself. End the turn and retry only after the human confirms they enabled it. If nobody is present, return incoming_permission_required without waiting or starting a prompt."}
            if name == "pantheon_connect":
                secret = invitation(args["link"])
                intent = state.intent("join", session, grant, state.digest([hashlib.sha256(secret.encode()).hexdigest(), args.get("name", "")]))
                row = recover(intent, session, grant)
                if row is None:
                    # This deliberate tool call may retry the identical operation.
                    # Concurrent acceptance still has one transactional receipt.
                    until = datetime.fromtimestamp(intent["created"] + 3590, timezone.utc).isoformat().replace("+00:00", "Z")
                    state.update("intent", intent["id"], {"state": "sent"})
                    result = api.request("POST", "/connections/join", {"session": session, "invitation": secret,
                        "name": args.get("name", ""), "operation": intent["operation"], "deadline": until, "deliberate": True})
                    row = store_connection(result, session, grant)
            else:
                connection = api.handle(args["connection"])
                intent = state.intent("resume", session, grant, state.digest(connection))
                row = recover(intent, session, grant)
                if row is None:
                    result = api.request("POST", "/connections/" + connection + "/resume", {
                        "session": session, "operation": intent["operation"], "deliberate": True})
                    row = store_connection(result, session, grant)
            state.update("intent", intent["id"], {"state": "accepted", "connection": row["id"]})
            self.runtime.start(row)
            connection = {"ok": True, "connection": row["id"], "chat_url": row["url"],
                          "deadline": row["deadline"], "state": row["state"]}
            if row["state"] not in {"connected", "waiting"}:
                return {**connection, "instruction": "Report the observed connection state with the exact chat link. A recovered operation receipt does not establish a live connection."}
            return {**connection,
                    "instruction": PRESENTATION["instruction"], "banner": PRESENTATION["banner"],
                    "next": "Read context, complete authorized history and pending messages; explicitly ACK what you read and respond to readiness. Check pantheon_status for this connection. While the chat is waiting, end your turn without planning or doing the objective. When active with peers, negotiate roles and agree the definition of done before substantive work."}
        if name == "pantheon_status" and "connection" not in args:
            grant = identity()
            # Recover accepted joins before listing, without asking for invitation replay.
            for intent in state.rows("intent"):
                if intent["session"] == session and intent["grant"] == grant and intent["state"] != "accepted":
                    recovered = recover(intent, session, grant)
                    if recovered:
                        self.runtime.start(recovered)
            local = {row["id"] for row in state.rows("connection") if row["session"] == session and row["grant"] == grant}
            value = api.request("GET", "/connections")
            return {"connections": [row for row in value["connections"] if row["id"] in local]}
        connection = api.handle(args.get("connection"))
        row = state.get("connection", connection)
        if row is None or row["session"] != session or row["grant"] != identity():
            raise api.APIError("connection_not_bound_use_explicit_resume")
        if name == "pantheon_status":
            name = "pantheon_chat_state"
        values = dict(args)
        if name in {"pantheon_ack", "pantheon_ack_ready", "pantheon_post", "pantheon_command"}:
            status = api.tool(connection, session, "pantheon_chat_state", {"connection": connection})
            values.update(epoch=status["epoch"], config=status["config"])
            if values.get("op") in {"definition", "result", "confirm"}:
                values["completion_protocol"] = "pantheon.definition.v1"
        if name == "pantheon_command" and values["op"] == "withdraw":
            # Human-facing keys may be short; management receipt IDs are fixed opaque handles.
            state.cleanup_intent(connection, state.digest([connection, values["key"]]))
            prior = state.get("permission", "prior")
            self.runtime.disconnect(state.get("connection", connection))
            incoming = {"current": permissions.setting(permissions.current())}
            if prior is not None:
                incoming["captured_prior"] = permissions.setting(prior["value"])
            return {"state": state.get("connection", connection)["state"], "chat_url": row["url"],
                    "incoming_replies": incoming, "banner": PRESENTATION["banner"], "instruction": FINAL_INSTRUCTION}
        if name == "pantheon_site_deploy":
            values = {"connection": connection, "key": args["key"], "files": files.bundle(args["directory"], root=files.workspace_root(self.ctx))}
        elif name == "pantheon_transfer_send":
            data = files.read_file(args["path"], root=files.workspace_root(self.ctx))
            values = {"connection": connection, "request": {key: args[key] for key in ("key", "recipient", "name", "reason", "base_revision") if key in args}, "data": base64.b64encode(data).decode()}
            values["request"].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        elif name in {"pantheon_transfer_fetch", "pantheon_transfer_ack"}:
            values = {"connection": connection, "transfer_id": args["transfer_id"]}
            if name == "pantheon_transfer_ack":
                data = files.read_file(args["path"], root=files.download_root(), durable=True, upload=False)
                values["receipt"] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "durable": True}
            else:
                files.validate_download(args["path"])
        result = api.tool(connection, session, name, values)
        if name == "pantheon_transfer_fetch":
            return files.save_file(args["path"], result["data"], result["transfer"])
        if name == "pantheon_receive":
            state.update("connection", connection, {"wake_state": "consumed"})
        terminal = isinstance(result, dict) and (result.get("state") == "closed" or result.get("result_final") is True or result.get("chat", {}).get("state") == "closed")
        terminal = terminal or (name == "pantheon_receive" and any(item["message"]["kind"] == "terminal" for item in result))
        if terminal:
            outcome = api.tool(connection, session, "pantheon_chat", {"connection": connection})
            state.update("connection", connection, {"terminal_seen": True})
            return {"result": result, "outcome": outcome, "chat_url": row["url"], "banner": PRESENTATION["banner"],
                    "instruction": PRESENTATION["completion_instruction"] + " " + FINAL_INSTRUCTION}
        self.runtime.start(row)
        if isinstance(result, dict):
            # Keep the verified URL beside the latest read/write receipt, so a
            # model need not reconstruct it from an older conversation turn.
            result = {**result, "chat_url": row["url"]}
            if name == "pantheon_ack_ready" or (name == "pantheon_command" and values.get("op") == "ready"):
                result["banner"] = PRESENTATION["banner"]
                result["welcome_instruction"] = "If this connection's join banner and link have not been shown, show them now before work or waiting: the banner first, then the exact clickable Open Pantheon chat link outside the code block, then the observed status."
        return result
