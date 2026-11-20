"""Bounded error explanations from protocol metadata, never rejected payloads."""
import json
import re
from pathlib import Path

CATALOG = json.loads((Path(__file__).parent / 'catalog.json').read_text())['tools']
FIELDS = {'arguments', 'request', 'body', 'ids', 'epoch', 'config', 'usage_status',
          'usage_status.availability', 'usage_status.reason', 'usage_status.source'}
SPECS = {}

def fields(properties, prefix=''):
    for name, spec in properties.items():
        path = prefix + name
        FIELDS.add(path)
        SPECS.setdefault(path, spec)
        if spec.get('type') == 'array' and spec.get('items'):
            FIELDS.add(path + '[]')
            SPECS.setdefault(path + '[]', spec['items'])
        if spec.get('type') == 'object':
            fields(spec.get('properties', {}), path + '.')

for tool in CATALOG:
    fields(tool['inputSchema']['properties'])

REASONS = {
    'required': 'Supply this required field using the current tool schema.',
    'wrong_type': 'Use the type declared for this field in the current tool schema.',
    'invalid_value': 'Use a value allowed by the current tool schema.',
    'out_of_range': 'Use a value within the current tool schema bounds.',
    'too_long': 'Shorten this field to the documented limit.',
    'unknown_field': 'Remove unsupported fields; use tool_describe for this tool. Do not add guessed arguments.',
    'report_only': 'Supply usage only with a progress report; omit it from other operations.',
    'checkpoint_requires_report': 'Use pantheon_checkpoint, read and ACK its messages, then call pantheon_report with its checkpoint. Do not add checkpoint to pantheon_command.',
    'checkpoint_required': 'Call pantheon_checkpoint, read and ACK its messages, then call pantheon_report with the returned checkpoint.',
    'invalid_report_status': "Use target:'waiting' when waiting; otherwise use the matching supported status. Status is not inferred from text.",
    'stale_checkpoint': 'Call pantheon_checkpoint again, process and ACK its messages, then report with the returned checkpoint.',
    'stale_version': 'Read pantheon_chat and use the current authoritative version. Do not guess a version.',
    'idempotency_mismatch': 'This key was used for different arguments. Use a new key only for a corrected distinct write; keep the original key and payload for an uncertain retry.',
    'usage_decreased': 'Cumulative token totals cannot decrease. Use reliable attributable totals or omit usage; do not invent counters.',
    'definition_not_ready': 'Read pantheon_chat and finish required definition confirmations before reporting working. Use waiting while awaiting agreement.',
    'review_paused': 'Read pantheon_chat and resolve the paused review before continuing work.',
    'collaboration_waiting': 'Read pantheon_chat; wait for required ready agents before starting work.',
    'invalid_arguments': 'Check the current tool schema and required fields. No more specific cause was provided; do not guess chat state or fabricate values.',
    'request_conflict': 'Read the latest chat state and operation receipt. Do not guess the conflicting condition or repeat the rejected call unchanged.',
    'request_failed': 'Check connection and operation status. Preserve the exact key and payload when reconciling an uncertain write.',
}

MESSAGES = {
    'invalid_arguments': 'The tool arguments are invalid.',
    'invalid_request': 'Pantheon rejected the request.',
    'invalid_connection': 'The connection handle is invalid. Use the handle returned by connect or resume.',
    'invalid_tool': 'This tool is unavailable. Use tool_describe to check its current name and schema.',
    'conflict': 'The request conflicts with current state or an earlier operation.',
    'closed': 'This connection or chat is closed. Read the retained outcome and stop new writes.',
    'not_accepted': 'The requested connection or operation was not found. Check its existing handle or receipt.',
    'outcome_unknown': 'The write outcome is unknown. Reconcile its receipt; retry only the identical key and payload.',
    'unavailable': 'Pantheon is temporarily unavailable. Check the operation status before retrying a write.',
    'sign_in_required': 'Pantheon sign-in is required. Check native auth status in this profile.',
    'native_auth_unavailable': 'Native authentication is unavailable. Check native auth status in this profile.',
    'public_client_id_required': 'The Pantheon public client ID is missing. Follow the setup configuration instruction.',
    'access_unavailable': 'Pantheon access is unavailable. Check this profile and connection; do not bypass the denial.',
    'incoming_permission_required': 'Incoming replies need the human’s native approval. Show the supplied permission instruction.',
    'coordination_unavailable': 'This server does not support guarded collaboration. Use the supplied read and cleanup recovery steps.',
    'prior_report_pending': 'An earlier report has an unknown outcome. Reconcile or retry its exact key and payload first.',
    'write_key_payload_conflict': 'This write key already has different arguments. Preserve uncertain retries; use a new key only for a distinct corrected write.',
    'report_key_payload_conflict': 'This report key already has different arguments. Preserve uncertain retries; use a new key only for a distinct corrected report.',
    'confirm_source_invalid': 'Confirmation source must be omitted, empty, or disagree.',
    'confirm_fields_required': 'Confirmation requires target, positive expected, and nonempty text from the current definition or result.',
    'confirm_evidence_required': 'Disagreement requires evidence describing the gaps and next step.',
    'file_path_not_allowed': 'This file path is outside the allowed nonsecret workspace or download folder.',
    'session_workspace_required': 'A verified workspace for this native session is required before file access.',
    'destination_exists': 'The destination already exists. Choose a new file path; do not overwrite it.',
    'file_limit': 'The file exceeds the supported transfer limit. Use a smaller authorized file.',
    'site_limit': 'The site exceeds the supported bundle limit. Reduce the bundle.',
    'index_html_required': 'The site bundle needs index.html at its root.',
    'symlink_in_bundle': 'The site contains a symbolic link. Use regular nonsecret files within the workspace.',
    'unsupported_bundle_file': 'The site contains an unsupported file. Use regular nonsecret files within the workspace.',
    'transfer_integrity_failed': 'The downloaded file failed its integrity check. Do not use or acknowledge it as saved.',
    'project_hooks_unavailable': 'This project cannot start a guarded assignment in this conversation.',
    'project_focus_required': 'Ask your human to select this project in the current native conversation with focus.',
    'project_focus_conflict': 'This conversation already has a different project assignment selected.',
    'project_assignment_active': 'Pause or finish the assignment before removing its active project.',
    'invalid_runtime_policy': 'The saved runtime policy is invalid. Use doctor; do not widen or renew the grant.',
    'native_session_required': 'A supported native conversation is required. Continue in the original Hermes conversation.',
    'representative_session_required': 'Only the connected representative conversation can use these tools. Return to that conversation.',
    'invalid_invitation_link': 'Use the exact Pantheon invitation deliberately supplied by the human. Do not reconstruct it.',
    'deliberate_invitation_required': 'Connect only with an invitation deliberately supplied by the human.',
    'connection_not_bound_use_explicit_resume': 'This connection belongs to another session. Use explicit resume in the intended native conversation.',
    'original_consent_required': 'The original runtime approval is missing. Do not renew or recreate it from a retry.',
    'invalid_question': 'The question marker is invalid. Check the pantheon_question schema and current context version.',
    'question_context_changed': 'The question context changed. Read the current chat before asking the human.',
    'question_not_found': 'The pending question marker was not found. List pending questions first.',
    'question_limit': 'The pending question limit was reached. Resolve existing questions before adding another.',
    'question_connection_closed': 'The connection is closed. Do not prompt the human for new collaboration questions.',
    'local_operation_failed': 'The local operation failed. Use pantheon_doctor; do not replay an uncertain write with a new key.',
}


def detail(value):
    """Accept known field/reason metadata only; response prose is never echoed."""
    if not isinstance(value, dict):
        return None
    field, reason = value.get('field'), value.get('reason')
    if not isinstance(field, str) or field not in FIELDS or not isinstance(reason, str) or reason not in REASONS:
        return None
    instruction = REASONS[reason]
    spec = SPECS.get(field, {})
    if reason == 'wrong_type' and spec.get('type'):
        instruction = 'Use a ' + spec['type'] + ' for this field.'
    elif reason == 'invalid_value' and spec.get('enum'):
        instruction = 'Use one of: ' + ', '.join(str(v) for v in spec['enum']) + '.'
    elif reason == 'out_of_range' and 'minimum' in spec and 'maximum' in spec:
        instruction = 'Use an integer from ' + str(spec['minimum']) + ' to ' + str(spec['maximum']) + '.'
    elif reason == 'required' and field == 'key':
        instruction = 'Supply a nonempty idempotency key. Reuse it only for the exact same operation and payload.'
    return {'field': field, 'reason': reason, 'instruction': instruction}


def result(value, *, status=0, metadata=None, server_code=None):
    """Keep a stable code while putting actionable text where Hermes renders errors."""
    code = value.get('error')
    # APIError codes originate in plugin code; transport accepts only fixed
    # status-matched server codes. Keep unknown future machine codes stable.
    if not isinstance(code, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', code):
        code = 'local_operation_failed'
    safe = detail(metadata)
    output = {**value, 'ok': False, 'error_code': code}
    if safe:
        output['detail'] = safe
        output['instruction'] = safe['instruction']
        output['error'] = safe['field'] + ': ' + safe['instruction']
    else:
        output['error'] = MESSAGES.get(code, 'Pantheon could not complete this operation. Check its status before retrying.')
    if status:
        output['http_status'] = status
    if server_code:
        output['server_error_code'] = server_code
    return output
