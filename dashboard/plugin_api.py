"""Host-authenticated management only. No agent tool proxy or session spoofing."""
import re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()
HANDLE = r"^[A-Za-z0-9_-]{16,80}$"


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str = Field(pattern=HANDLE)
    grant: str = Field(pattern=HANDLE)


def call(method, path, body=None):
    # This file is loaded independently by the official dashboard backend loader.
    # Keep the helper call in this plugin-owned frame and the host's profile scope.
    from hermes_cli.plugin_provider_requests import credentialed_provider_request, ProviderNotSignedIn
    from hermes_cli.auth_constants import AuthError
    try:
        response = credentialed_provider_request("pantheon", method,
            "https://pantheon.talk/api/hermes/v1" + path, json=body, timeout=25)
        if response.status >= 300:
            raise HTTPException(status_code=response.status if response.status in {400,401,403,404,409,410} else 503,
                                detail="Pantheon access unavailable")
        return response.json()
    except ProviderNotSignedIn:
        raise HTTPException(401, "Sign in with hermes auth add pantheon:pantheon") from None
    except AuthError:
        raise HTTPException(401, "Native sign-in unavailable") from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Outcome unknown; reconcile the original operation") from None


def check(value):
    if not re.fullmatch(HANDLE, value):
        raise HTTPException(400, "Invalid selector")
    return value


def grant(expected=None):
    identity = call("GET", "/identity")
    current = identity["grant_id"]
    if expected is not None and current != expected:
        raise HTTPException(409, "Sign-in changed; refresh connections")
    return current


@router.get("/connections")
def connections():
    current = grant()
    result = call("GET", "/connections")
    # A reauthentication during these requests cannot mix two snapshots.
    grant(current)
    return {"grant": current, "connections": result["connections"], "version": "1.0.9"}


@router.post("/connections/{connection}/disconnect")
def disconnect(connection: str, operation: Operation):
    grant(operation.grant)
    result = call("POST", "/connections/" + check(connection) + "/disconnect", {"operation": operation.operation})
    return {"grant": operation.grant, "connection": result}


@router.get("/operations/{operation}")
def operation(operation: str, expected_grant: str):
    current = grant(check(expected_grant))
    return {"grant": current, "connection": call("GET", "/operations/" + check(operation))}


@router.post("/authorization/revoke")
def revoke(operation: Operation):
    grant(operation.grant)
    return call("POST", "/authorization/revoke", {"grant": operation.grant})
