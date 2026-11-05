"""Observe native permission; never enable incoming turns ourselves."""
from . import state

KEY = "plugins.entries.pantheon.allow_gateway_injection"


def current():
    from hermes_cli.config import load_config_readonly
    return load_config_readonly().get("plugins", {}).get("entries", {}).get("pantheon", {}).get("allow_gateway_injection")


def ready():
    value = current()
    if value is True:
        return True
    if state.get("permission", "prior") is None:
        state.put("permission", "prior", {"value": value})
    return False


def restore():
    # No restoration while another connection still needs replies. A human's
    # subsequent false/unset choice always wins. This only restores our baseline.
    if any(row.get("state") not in {"disconnected", "revoked", "authorization_unavailable"} for row in state.rows("connection")):
        return
    previous = state.get("permission", "prior")
    if previous is None:
        return
    if current() is True:
        from hermes_cli.config import set_config_value, unset_config_value
        if previous["value"] is None:
            unset_config_value(KEY)
        else:
            set_config_value(KEY, "false")
    state.delete("permission", "prior")
