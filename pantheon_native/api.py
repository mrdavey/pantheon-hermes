"""All authenticated transport stays inside the official Hermes OAuth helper."""
import re

ORIGIN = "https://pantheon.talk"
HANDLE = re.compile(r"^[A-Za-z0-9_-]{16,80}$")


class APIError(Exception):
    def __init__(self, code, status=0):
        super().__init__(code)
        self.code, self.status = code, status


def handle(value):
    if not isinstance(value, str) or not HANDLE.fullmatch(value):
        raise APIError("invalid_connection")
    return value


def request(method, path, body=None):
    # No arbitrary URL, redirects, bearer extraction or alternate transport.
    from hermes_cli.plugin_provider_requests import credentialed_provider_request, ProviderNotSignedIn
    from hermes_cli.auth_constants import AuthError
    if method not in {"GET", "POST"} or not re.fullmatch(r"/[a-zA-Z0-9_/-]+", path):
        raise APIError("invalid_request")
    try:
        response = credentialed_provider_request("pantheon", method, ORIGIN + "/api/hermes/v1" + path,
                                                json=body, timeout=25)
        if response.status >= 300:
            code = {400: "invalid_request", 401: "sign_in_required", 403: "access_unavailable",
                    404: "not_accepted", 409: "conflict", 410: "closed"}.get(response.status, "unavailable")
            raise APIError(code, response.status)
        return response.json()
    except ProviderNotSignedIn:
        raise APIError("sign_in_required") from None
    except AuthError as error:
        raise APIError("public_client_id_required" if error.code == "oauth_client_id_missing" else "native_auth_unavailable") from None
    except APIError:
        raise
    except Exception:
        # Never return request/response exception strings or token-bearing frames.
        raise APIError("outcome_unknown") from None


def tool(connection, session, name, arguments):
    if not re.fullmatch(r"pantheon_[a-z_]+", name):
        raise APIError("invalid_tool")
    return request("POST", "/connections/" + handle(connection) + "/tools/" + name,
                   {"session": session, "arguments": arguments})
