"""Fixed declared tools; no remote schema loading or executable descriptors."""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from . import accounting, api, files, permissions, state
from .session import observe

CATALOG = json.loads((Path(__file__).parent / "catalog.json").read_text())["tools"]
SETUP_CONFIRMATION = r"\o/ Pantheon setup complete!"
PRESENTATION = json.loads((Path(__file__).parent / "presentation.json").read_text())
SCHEMAS = {item["name"]: item for item in CATALOG}
RECOVERY_INSTRUCTION = (
    "Coordination tools are unavailable on this server. This is read/cleanup recovery, not a completed checkpoint. "
    "Read these messages, use pantheon_receive and pantheon_chat for further reads, and ACK only consumed IDs. "
    "Show the retained outcome and exact chat link, then use pantheon_command with op=withdraw for cleanup. "
    "Do not retry unsupported checkpoint/report/opening tools or replace them with unguarded posts/reports. "
    "Resume collaboration only after current-server coordination support is observed or an explicit successful resume."
)
FINAL_INSTRUCTION = (
    "Keep the chat link exact and describe only verified chat and cleanup facts. "
    "Connection cleanup does not establish task completion or validation success. "
    "Use returned incoming_replies values when present; do not infer prior settings from earlier conversation turns. "
    "Incoming replies remain enabled until the human disables them. Only legacy temporary setup restores its prior setting; do not change it yourself or add a config check just to compose the response."
)
CONFIRM_INSTRUCTION = (
    "Read pantheon_chat and review chat.definition or chat.result. Copy that object's id to target, "
    "version to expected (the version being confirmed), and digest verbatim to text. "
    "Omit source to agree, or use an empty string; source=agree is invalid. "
    "To disagree use source=disagree and nonempty evidence describing gaps and the next step. "
    "Never use a history message ID, calculate/guess digests or inspect plugin source. "
    "Use a new key for corrected arguments; preserve the exact key and payload only for an identical uncertain retry."
)
CONFIRM_ERRORS = {
    "confirm_source_invalid": "source must be omitted, empty, or disagree.",
    "confirm_fields_required": "target, positive expected, and nonempty text are required.",
    "confirm_evidence_required": "Disagreement requires nonempty evidence.",
}


def welcome(chat_url):
    """Keep the complete visible setup response available through readiness."""
    return {
        "banner": PRESENTATION["banner"],
        "setup_confirmation": SETUP_CONFIRMATION,
        "setup_message": (
            f"```text\n{PRESENTATION['banner']}\n```\n\n"
            f"[Open Pantheon chat]({chat_url})\n\n```text\n{SETUP_CONFIRMATION}\n```"
        ),
        "welcome_instruction": (
            "If this connection's join banner and link have not been shown, show them now before work or waiting. "
            "Installation, update, repairing public client-ID configuration, OAuth sign-in, or the human enabling incoming replies "
            "in this conversation each counts as setup, even when the plugin was already installed. Checking or reusing ready settings alone is not setup. "
            "After that setup and a successful live join, show setup_confirmation once: use setup_message verbatim "
            "as visible assistant text, then add the observed status. Do not leave it only in tool output or hidden reasoning. "
            "If the banner/link were already shown, show only the missing setup_confirmation in a code block. "
            "Do not repeat a message already shown, or celebrate an already-ready installation or retry with no setup work. "
            "For an ordinary join with no setup work, show just the banner and exact chat link, then observed status."
        ),
    }


def validate(name, args):
    # The complete snapshot is checked by jsonschema at build/test time. Runtime
    # checks reject extra authority selectors before any API or filesystem call.
    schema = SCHEMAS[name]["inputSchema"]
    if not isinstance(args, dict) or set(args) - set(schema["properties"]) or set(schema.get("required", [])) - set(args):
        raise api.APIError("invalid_arguments")
    for key, value in args.items():
        spec = schema["properties"][key]
        kind = spec["type"]
        expected = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}[kind]
        if not isinstance(value, expected) or (kind == "integer" and isinstance(value, bool)):
            raise api.APIError("invalid_arguments")
        if key == "usage" and (args.get("op") != "report" or set(value) != {"input_tokens", "output_tokens"}
                               or any(type(count) is not int or not 0 <= count <= 10**12 for count in value.values())):
            raise api.APIError("invalid_arguments")
        if "enum" in spec and value not in spec["enum"]:
            raise api.APIError("invalid_arguments")
        if kind in {"string", "array"} and not spec.get("minItems", 0) <= len(value) <= spec.get("maxLength", spec.get("maxItems", 1_000_000)):
            raise api.APIError("invalid_arguments")
        if kind == "array" and any(not isinstance(item, str) for item in value):
            raise api.APIError("invalid_arguments")
    if name == "pantheon_command" and args.get("op") == "confirm":
        if args.get("source", "") not in {"", "disagree"}:
            raise api.APIError("confirm_source_invalid")
        if not args.get("target", "").strip() or args.get("expected", 0) < 1 or not args.get("text", "").strip():
            raise api.APIError("confirm_fields_required")
        if args.get("source") == "disagree" and not args.get("evidence", "").strip():
            raise api.APIError("confirm_evidence_required")


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


def store_connection(result, session, grant, usage_lineage=None):
    api.handle(result["id"])
    previous = state.get("connection", result["id"]) or {}
    # No returned authority or content is copied wholesale into local state.
    row = {key: result[key] for key in ("id", "chat", "member", "url", "deadline", "state")}
    if 'runtime_policy' in result:
        row['runtime_policy'] = result['runtime_policy']
    row.update(session=session, grant=grant)
    for key in ("cleanup_operation", "wake", "wake_state", "wake_text_digest", "opening", "terminal_deadline", "terminal_seen", "usage_lineage", "usage_started", "usage_blocked", "coordination_unavailable"):
        if key in previous and previous.get("grant") == grant and previous.get("session") == session:
            row[key] = previous[key]
    if not previous and usage_lineage:
        row["usage_lineage"] = usage_lineage
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
    row = store_connection(result, session, grant, intent.get("usage_lineage"))
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
            if error.code == "coordination_unavailable":
                result["instruction"] = RECOVERY_INSTRUCTION
            if error.code == "prior_report_pending":
                result["instruction"] = "An earlier report has an uncertain outcome. Reconcile or retry that exact key and payload before reporting newer totals. Never replace it with a new key."
            if error.code in CONFIRM_ERRORS:
                result["instruction"] = CONFIRM_ERRORS[error.code] + " " + CONFIRM_INSTRUCTION
            elif name in {"pantheon_command", "pantheon_report", "pantheon_opening"} and error.code in {"invalid_arguments", "invalid_request", "conflict"}:
                result["instruction"] = (
                    "Do not blindly retry. Read pantheon_chat; check current readiness, definition/result agreement, "
                    "versions and whether the key was already used for a different payload. "
                    "Report waiting while required definition confirmations are outstanding; working requires readiness "
                    "and definition agreement. Use a new key for a corrected operation, never to replay an uncertain write."
                )
                if isinstance(args, dict) and args.get("op") == "confirm":
                    result["instruction"] += " " + CONFIRM_INSTRUCTION
            if error.code in {"sign_in_required", "native_auth_unavailable", "public_client_id_required"}:
                result["instruction"] = (
                    "Before requesting sign-in, verify the public client ID yourself in this observed profile: "
                    "env -u PANTHEON_OAUTH_CLIENT_ID hermes config get PANTHEON_OAUTH_CLIENT_ID. "
                    "If unset, empty or already pantheon-hermes, run hermes config set PANTHEON_OAUTH_CLIENT_ID pantheon-hermes yourself, "
                    "then repeat the env -u readback in a fresh command and require exactly pantheon-hermes. "
                    "Preserve a different configured value and report a mismatch; other config failures stop setup. "
                    "Use hermes --profile <observed-profile> for named profiles in every command. "
                    "Do not infer configuration from a manifest, shell-only export or sign_in_required. "
                    "After verified config, check native auth status and reuse valid sign-in. "
                    "If missing, hermes auth add pantheon:pantheon is human-owned. "
                    "Never read or edit .env or auth.json, copy tokens or clear agent-context markers."
                )
            if error.code in {"file_path_not_allowed", "session_workspace_required"}:
                result["instruction"] = "Uploads and site bundles require nonsecret files in this session's workspace. Downloads and durable ACKs require Pantheon's profile downloads directory, reported by pantheon_doctor. Never copy credentials, change the workspace, or use another tool to bypass these restrictions."
            return json.dumps(result)
        except Exception:
            return json.dumps({"ok": False, "error": "local_operation_failed", "instruction": "Use pantheon_doctor. Do not repeat an uncertain write with a new key."})

    def dispatch(self, name, args, session):
        if name in {"pantheon_connect", "pantheon_resume"}:
            with state.permission_guard():
                return self._dispatch(name, args, session)
        if name == "pantheon_report" or (name == "pantheon_command" and args.get("op") == "report"):
            with state.report_guard(args.get("connection")):
                return self._dispatch(name, args, session)
        return self._dispatch(name, args, session)

    def legacy_checkpoint(self, row, connection, session):
        # An initial well-formed read rejected by an older server can fall back
        # only to existing reads. Never manufacture a signed checkpoint or post.
        messages = api.tool(connection, session, "pantheon_receive", {"connection": connection})
        outcome = api.tool(connection, session, "pantheon_chat", {"connection": connection})
        terminal = (outcome.get("state") == "closed" or outcome.get("result_final") is True
                    or outcome.get("chat", {}).get("state") == "closed"
                    or any(item["message"].get("kind") == "terminal" for item in messages))
        state.update("connection", connection, {"coordination_unavailable": True, "wake_state": "consumed"})
        if terminal:
            state.update("connection", connection, {"terminal_seen": True})
        self.runtime.start(row)
        return {"status": "coordination_unavailable", "messages": messages, "outcome": outcome,
                "chat_url": row["url"], "instruction": RECOVERY_INSTRUCTION + (" " + FINAL_INSTRUCTION if terminal else "")}

    def _dispatch(self, name, args, session):
        if name == "pantheon_doctor":
            grant = identity()
            local = state.rows("connection")
            incoming = permissions.current()
            return {"ok": True, "signed_in": True, "incoming": incoming is True,
                    "incoming_setting": permissions.setting(incoming), "incoming_preference": permissions.mode(),
                    "connections": sum(row["grant"] == grant and row["session"] == session and row["state"] != "disconnected" for row in local),
                    "pending_operations": sum(row["session"] == session and row["grant"] == grant and row["state"] != "accepted" for row in state.rows("intent")),
                    "downloads_directory": str(files.download_root()),
                    "usage_capability": "public_hooks_partial", "usage_attribution": "exact_native_wake_turns",
                    "version": "1.0.13"}
        if name in {"pantheon_connect", "pantheon_resume"}:
            if args.get("deliberate") is not True:
                raise api.APIError("deliberate_invitation_required")
            grant = identity()
            if not permissions.ready():
                return {"ok": False, "error": "incoming_permission_required", "instruction":
                        "Explain visibly: Incoming replies let Pantheon deliver collaboration updates in this Hermes conversation while you wait, including after reopening this conversation, until the connection ends or its chosen deadline. This preference stays enabled for future invitations until you disable it in Hermes. Existing temporary approvals still restore their prior setting after their last connection. Show the human this command to run in their selected profile: hermes config set plugins.entries.pantheon.allow_gateway_injection true. Alternatively, they can enable Pantheon's incoming-replies permission in Hermes Desktop. This is a human action: do not execute the command, modify the setting, or enable it through another tool yourself. End the turn and retry only after the human confirms they enabled it. If nobody is present, return incoming_permission_required without waiting or starting a prompt."}
            if name == "pantheon_connect":
                secret = invitation(args["link"])
                lifetime = ({'deadline': datetime.fromtimestamp(args['expires_at'], timezone.utc).isoformat().replace('+00:00', 'Z')}
                            if 'expires_at' in args else {'runtime_policy': {'mode': 'connection'}})
                intent = state.intent("join", session, grant, state.digest([hashlib.sha256(secret.encode()).hexdigest(), args.get("name", "")]), lifetime=lifetime, usage_lineage="hermes.public-hooks.v1")
                # Pending operations created by older plugins keep the same one-hour
                # payload and receipt identity, even after a plugin update.
                lifetime = intent.get('lifetime', {'deadline': datetime.fromtimestamp(intent['created'] + 3590, timezone.utc).isoformat().replace('+00:00', 'Z')})
                if 'expires_at' in args and lifetime != {'deadline': datetime.fromtimestamp(args['expires_at'], timezone.utc).isoformat().replace('+00:00', 'Z')}:
                    raise api.APIError('original_consent_required')
                row = recover(intent, session, grant)
                if row is None:
                    # This deliberate tool call may retry the identical operation.
                    # Concurrent acceptance still has one transactional receipt.
                    state.update("intent", intent["id"], {"state": "sent"})
                    result = api.request("POST", "/connections/join", {"session": session, "invitation": secret,
                        "name": args.get("name", ""), "operation": intent["operation"], **lifetime, "deliberate": True})
                    row = store_connection(result, session, grant, intent.get("usage_lineage"))
            else:
                connection = api.handle(args["connection"])
                intent = state.intent("resume", session, grant, state.digest(connection))
                row = recover(intent, session, grant)
                if row is None:
                    result = api.request("POST", "/connections/" + connection + "/resume", {
                        "session": session, "operation": intent["operation"], "deliberate": True})
                    row = store_connection(result, session, grant)
            state.update("intent", intent["id"], {"state": "accepted", "connection": row["id"]})
            if name == "pantheon_resume":
                row = state.update("connection", row["id"], {"coordination_unavailable": False})
            self.runtime.start(row)
            connection = {"ok": True, "connection": row["id"], "chat_url": row["url"],
                          "deadline": row["deadline"], "state": row["state"]}
            if 'runtime_policy' in row:
                connection['runtime_policy'] = row['runtime_policy']
            if row["state"] not in {"connected", "waiting"}:
                return {**connection, "instruction": "Report the observed connection state with the exact chat link. A recovered operation receipt does not establish a live connection."}
            return {**connection, **welcome(row["url"]),
                    "instruction": PRESENTATION["instruction"],
                    "next": "Follow welcome_instruction before work or waiting; if the host buffers visible output, do so immediately after initial ACK/readiness. Read context, complete authorized history and pending messages; explicitly ACK what you read and respond to readiness. Check pantheon_status for this connection. While the chat is waiting, end your turn without planning or doing the objective. When active with peers, negotiate roles and agree the definition of done before substantive work."}
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
        if row.get("coordination_unavailable"):
            if name == "pantheon_checkpoint":
                return self.legacy_checkpoint(row, connection, session)
            if name in {"pantheon_report", "pantheon_opening"} or (name == "pantheon_command" and args.get("op") == "report"):
                raise api.APIError("coordination_unavailable")
        if name == "pantheon_question":
            status = api.tool(connection, session, "pantheon_chat_state", {"connection": connection})
            if status.get("state") == "closed" or row.get("state") not in {"connected", "waiting"}:
                raise api.APIError("question_connection_closed")
            context = api.tool(connection, session, "pantheon_context", {"connection": connection})
            return state.question(connection, args, context)
        report = name == "pantheon_report" or (name == "pantheon_command" and args.get("op") == "report")
        if report:
            state.report_pending(connection, args["key"])
        values = dict(args)
        if name in {"pantheon_ack", "pantheon_ack_ready", "pantheon_post", "pantheon_command", "pantheon_report", "pantheon_opening"}:
            status = api.tool(connection, session, "pantheon_chat_state", {"connection": connection})
            state.update("connection", connection, {"opening": status.get("opening")})
            if name in {"pantheon_report", "pantheon_opening"} or values.get("op") == "report":
                values.update(state.write_preconditions(connection, name, args, status))
            else:
                values.update(epoch=status["epoch"], config=status["config"])
            if values.get("op") in {"definition", "result", "confirm"}:
                values["completion_protocol"] = "pantheon.definition.v1"
        if name == "pantheon_report" or (name == "pantheon_command" and values.get("op") == "report"):
            try:
                measured = accounting.freeze(connection, values["key"], args)
            except ValueError as error:
                raise api.APIError(str(error)) from None
            values["usage_status"] = {key: measured[key] for key in ("availability", "reason", "source")}
            if "usage" in measured:
                values["usage"] = measured["usage"]
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
        if report:
            state.report_pending(connection, values["key"], mark=True)
        try:
            result = api.tool(connection, session, name, values)
        except api.APIError as error:
            if name == "pantheon_checkpoint" and not args.get("cursor") and error.code in {"invalid_request", "not_accepted"}:
                return self.legacy_checkpoint(row, connection, session)
            if report and error.code in {"invalid_request", "conflict", "closed", "not_accepted", "sign_in_required", "access_unavailable"}:
                state.report_resolved(connection, values["key"])
            raise
        if report:
            # Includes read_required: that response explicitly accepted no write.
            state.report_resolved(connection, values["key"])
        if name == "pantheon_transfer_fetch":
            return files.save_file(args["path"], result["data"], result["transfer"])
        if name == "pantheon_checkpoint" or (isinstance(result, dict) and (result.get("opening") or {}).get("version") == "pantheon.opening.v1"):
            state.update("connection", connection, {"coordination_unavailable": False})
        if name == "pantheon_chat_state" and isinstance(result, dict):
            state.update("connection", connection, {"opening": result.get("opening")})
        if isinstance(result, dict) and "opening" in result:
            state.update("connection", connection, {"opening": result["opening"]})
        if name in {"pantheon_receive", "pantheon_checkpoint"}:
            state.update("connection", connection, {"wake_state": "consumed"})
        terminal = isinstance(result, dict) and (result.get("state") == "closed" or result.get("result_final") is True or result.get("chat", {}).get("state") == "closed")
        terminal = terminal or (name == "pantheon_receive" and any(item["message"]["kind"] == "terminal" for item in result))
        terminal = terminal or (name == "pantheon_checkpoint" and any(item["message"]["kind"] == "terminal" for item in result.get("messages", [])))
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
                result.update(welcome(row["url"]))
        return result
