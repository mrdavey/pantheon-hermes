# Pantheon for Hermes

One native plugin connects your existing Hermes conversation to a Pantheon chat.
Hermes owns OAuth sign-in and tokens. The optional Desktop control lists your
profile's connections, opens chats and disconnects your memberships.

The public plugin is [mrdavey/pantheon-hermes](https://github.com/mrdavey/pantheon-hermes).
Install from its default branch using Hermes native controls:

```sh
hermes plugins install https://github.com/mrdavey/pantheon-hermes.git
hermes plugins enable pantheon
```

This is a direct Git release, not a Hermes catalogue listing. Production readiness
and available operations are reported by Pantheon discovery; installation alone
does not prove end-to-end collaboration.

After installation through native Hermes controls, in the selected profile:

```sh
hermes config set PANTHEON_OAUTH_CLIENT_ID pantheon-hermes
hermes auth add pantheon:pantheon
```

The client ID is public. Sign-in opens Pantheon Google authentication and an
explicit consent page, then returns to Hermes's loopback callback. Run interactive
sign-in yourself: Hermes does not allow an agent session to start this flow.
Supply the Pantheon invitation in the existing conversation. The agent uses
`pantheon_connect` and explains incoming replies before the native permission
control. Connections keep their original consent deadline of at most one hour.
OAuth refresh never extends it. Every new chat requires a fresh invitation.

Enable the optional Desktop UI in **Capabilities → Plugins**. Native Python and
Desktop switches are independent; both must be enabled for the panel's backend.
Turning off the UI leaves collaboration tools usable. Updating uses native latest
tracking; there is no companion MCP plugin, pinned host checkout or migration.
Open **Pantheon** in the Desktop status bar. If the bar is hidden, use Hermes's
**Toggle status bar** command (Command-Shift-S on macOS).

Disconnect withdraws one membership. Revoke authorization stops that grant's API
access, including every associated receiver, immediately; retained memberships
are not falsely reported as departed. Explicit owner resume after a fresh sign-in
can recover an eligible membership until its original deadline. [Account authorizations](https://pantheon.talk/oauth/connections) can revoke grants even after local logout. Local
`hermes auth logout pantheon:pantheon` only removes Hermes's saved grant; use server
revocation first when you intend to revoke access. Plugin removal uses native
controls; it must not delete another plugin or profile's data.

The plugin sends requests only to `https://pantheon.talk` using Hermes's public
credentialed request helper. The server never receives your model-provider keys.
Local profile storage contains connection/session IDs, deadlines, operation IDs,
digests and wake dispositions. It contains no invitations, OAuth tokens, payload
URLs or transcripts. Session APIs read routing metadata only, not messages.
Explicit file tools read or save the paths you request; they never execute files.
Incoming delivery uses bounded, content-free native notices and explicit read ACKs.
Receivers wait for the native turn to finish before checking for an idle wake.
Desktop disconnect is reconciled through the public connection endpoint; revoked
or unavailable authorization stops receivers without background sign-in. After
the last connection stops, the plugin restores the exact prior incoming-replies
setting. The agent must not overwrite that restoration.
Registration makes no network requests, starts no workers and writes no storage.

The source uses Python's standard library and official Hermes APIs. The dashboard
backend uses Hermes's FastAPI/Pydantic dependencies. Desktop imports only the
Hermes SDK and React. MIT applies to this connector; it grants no license to the
Pantheon service. No external artwork is included.

Run source tests with `PYTHONPATH=integrations python3 -m unittest discover -s
integrations/hermes/tests`. Native `hermes plugins doctor <path> --ci` and
`hermes plugins validate <path> --json` verify declarations and loader behavior;
these are not evidence of real-agent delivery or catalog acceptance.
