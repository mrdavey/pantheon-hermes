"""Pantheon native plugin entrypoint: registration is inert and network-free."""
from .pantheon_native import api, state
from .pantheon_native.runtime import Runtime
from .pantheon_native.session import observe
from .pantheon_native.tools import CATALOG, Tools


def register(ctx):
    runtime = Runtime(ctx)
    tools = Tools(ctx, runtime)
    for item in CATALOG:
        name = item["name"]
        def handler(args, *, session_id=None, task_id=None, _name=name, **kwargs):
            return tools.call(_name, args, session_id=session_id, task_id=task_id)
        ctx.register_tool(name=name, toolset="pantheon", schema={"name": name,
                          "description": item["description"], "parameters": item["inputSchema"]}, handler=handler)

    def before(*, session_id=None, **kwargs):
        try:
            runtime.observe(observe(ctx, session_id))
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

    ctx.register_hook("pre_llm_call", before)
    ctx.register_hook("post_llm_call", after)
    ctx.register_hook("on_session_reset", retire)
    ctx.register_hook("on_session_delete", retire)
    ctx.on_unload(runtime.stop)
