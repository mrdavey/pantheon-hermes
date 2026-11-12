"""Host-supplied context only; model arguments cannot select another session."""
from .api import APIError
from . import state


def observe(ctx, session_id=None, *, native_surface=None):
    from gateway.session_context import get_session_env
    from hermes_state import SessionDB
    session_id = session_id or get_session_env("HERMES_SESSION_ID")
    if not isinstance(session_id, str) or not session_id:
        raise APIError("native_session_required")
    db = SessionDB(read_only=True)
    try:
        row = db.get_session_list_row(session_id)
        # Check source BEFORE following lineage: delegates share a conversation root.
        # Hermes stores Desktop creation as `gui`, then may normalize it to
        # `local`. Neither storage label is the current client surface.
        if not row or row.get("source") not in {"cli", "tui", "gui", "desktop", "local"} or row.get("hidden") or row.get("archived"):
            raise APIError("representative_session_required")
        # on_session_open runs before a turn policy exists. Its public hook
        # supplies platform directly; tool callers cannot provide this override.
        surface = native_surface if native_surface is not None else ctx.session_surface()
        if surface not in {"cli", "tui", "desktop"}:
            raise APIError("representative_session_required")
        root = db.get_conversation_root(session_id)
    finally:
        db.close()
    route = get_session_env("HERMES_SESSION_KEY") or root
    profile = ctx.profile_name
    if not all(isinstance(v, str) and v and len(v) <= 200 and not any(c in v for c in "\x00\r\n") for v in (root, route, profile)):
        raise APIError("native_session_required")
    state.put("session", session_id, {"id": root, "profile": profile})
    return {"id": root, "route": route, "profile": profile, "surface": surface}
