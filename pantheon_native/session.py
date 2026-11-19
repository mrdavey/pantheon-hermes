"""Host-supplied context only; model arguments cannot select another session."""
from contextlib import contextmanager
from .api import APIError
from . import state


@contextmanager
def session_metadata():
    try:
        from persistence_assembly import open_persistence_reader, profile_scope
    except ModuleNotFoundError as error:
        if error.name != "persistence_assembly":
            raise
        # The supported Hermes release predates the public storage interface.
        from hermes_state import SessionDB
        db = SessionDB(read_only=True)
        try:
            yield db.get_session_list_row, db.get_conversation_root
        finally:
            db.close()
    else:
        # Resolve the current host profile, never a model-supplied name or path.
        with open_persistence_reader(profile_scope().profile_dir) as store:
            def row(session_id):
                value = store.sessions.list_row(session_id)
                return None if value is None else {
                    "source": value.source, "hidden": value.hidden,
                    "archived": value.archived,
                }
            yield row, store.lineage.conversation_root


def observe(ctx, session_id=None, *, native_surface=None):
    from gateway.session_context import get_session_env
    session_id = session_id or get_session_env("HERMES_SESSION_ID")
    if not isinstance(session_id, str) or not session_id:
        raise APIError("native_session_required")
    with session_metadata() as (get_row, get_root):
        row = get_row(session_id)
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
        root = get_root(session_id)
    route = get_session_env("HERMES_SESSION_KEY") or root
    profile = ctx.profile_name
    if not all(isinstance(v, str) and v and len(v) <= 200 and not any(c in v for c in "\x00\r\n") for v in (root, route, profile)):
        raise APIError("native_session_required")
    state.put("session", session_id, {"id": root, "profile": profile})
    return {"id": root, "route": route, "profile": profile, "surface": surface}
