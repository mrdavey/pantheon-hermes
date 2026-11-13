"""Pantheon native plugin entrypoint: registration is inert and network-free."""
from pathlib import Path
from .pantheon_native import api, state, accounting
from .pantheon_native.runtime import Runtime
from .pantheon_native.session import observe
from .pantheon_native.tools import CATALOG, Tools


def register(ctx):
    ctx.register_skill(
        name="collaborate",
        path=Path(__file__).parent / "skills" / "collaborate" / "SKILL.md",
        description="Join a human-shared Pantheon invitation and collaborate using native OAuth tools.",
    )
    runtime = Runtime(ctx)
    tools = Tools(ctx, runtime)
    for item in CATALOG:
        name = item["name"]
        def handler(args, *, session_id=None, task_id=None, _name=name, **kwargs):
            return tools.call(_name, args, session_id=session_id, task_id=task_id)
        ctx.register_tool(name=name, toolset="pantheon", schema={"name": name,
                          "description": item["description"], "parameters": item["inputSchema"]}, handler=handler)

    def before(*, session_id=None, turn_id=None, user_message=None, parent_session_id="", **kwargs):
        try:
            session = observe(ctx, session_id)
            try:
                accounting.begin(session, turn_id, user_message, parent_session_id)
            except Exception:
                pass  # Accounting failure cannot block the native collaboration turn.
            runtime.turn(session, True)
        except api.APIError:
            pass

    def after(*, session_id=None, assistant_response=None, **kwargs):
        if assistant_response:
            try:
                runtime.finish(observe(ctx, session_id))
            except api.APIError:
                pass

    def retire(*, session_id=None, **kwargs):
        try:
            runtime.finish(observe(ctx, session_id), terminal_only=False)
        except api.APIError:
            # Delete may already have removed the host metadata row. Only use an
            # alias previously observed through native session metadata.
            known = state.get("session", session_id) if session_id else None
            if known and known["profile"] == ctx.profile_name:
                runtime.finish(known, terminal_only=False)

    def turn_ended(*, session_id=None, **kwargs):
        try:
            runtime.turn(observe(ctx, session_id), False)
        except api.APIError:
            pass

    def opened(*, session_id=None, platform=None, **kwargs):
        try:
            runtime.schedule_reopen(observe(ctx, session_id, native_surface=platform))
        except api.APIError:
            pass

    def measured(*, session_id=None, turn_id=None, api_request_id=None, usage=None, **kwargs):
        try:
            accounting.record(observe(ctx, session_id), turn_id, api_request_id, usage)
        except Exception:
            pass  # Accounting must never start/retry work or break native delivery.

    ctx.register_hook("post_api_request", measured)
    ctx.register_hook("post_auxiliary_call", measured)
    ctx.register_hook("api_request_error", measured)
    ctx.register_hook("pre_llm_call", before)
    ctx.register_hook("post_llm_call", after)
    ctx.register_hook("on_session_end", turn_ended)
    ctx.register_hook("on_session_open", opened)
    ctx.register_hook("on_session_reset", retire)
    ctx.register_hook("on_session_delete", retire)
    ctx.on_unload(runtime.stop)
