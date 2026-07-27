# Message platform deployment and migration

Message platforms are optional tenant connections. The Agent Console core can
start and become ready without a WeChat SDK token or a connector worker. Adding
an adapter makes its configuration surface available; it does **not** mean an
external account is authenticated or online.

## Process and network direction

The legacy WeChat integration is a pull/hybrid adapter:

1. `wxbot_client` runs beside WeChat Desktop on Windows and exposes its local
   HTTP SDK (normally port `5080`).
2. The optional `wxbot-bridge-worker` initiates HTTP requests to that SDK. It
   polls status/messages/events and places normalized work on Agent Console's
   queues; outbound delivery is also initiated by Agent Console toward the SDK.
3. If an operator configures remote activation and explicitly enables device
   binding consent, `wxbot_client` calls that service with a persisted random
   device identifier to obtain and refresh signed runtime authorization. It
   does not collect or transmit raw hardware or hostname identifiers.

The SDK does not need to push ordinary messages into the Agent Console API.
Legacy `CS_API_BASE_URL`, `CS_API_TOKEN`, and `CS_TENANT_ID` settings describe an
older callback direction and are not part of the bridge deployment contract.
Do not expose the Agent Console ingress API merely to make those values work.

## Core and optional Compose profiles

Start only the core platform:

```powershell
docker compose --profile app up -d --build
```

Add the WeChat SDK adapter explicitly:

```powershell
$env:COMPOSE_WXBOT_SDK_URL = 'http://host.docker.internal:5080'
$env:COMPOSE_CHANNEL_CONNECTION_ID = '<connection-id-from-/channels>'
$env:COMPOSE_CHANNEL_ALLOWED_SDK_ORIGINS = 'http://host.docker.internal:5080'
docker compose --profile app --profile wxbot up -d --build
```

Leave `COMPOSE_CHANNEL_CONNECTION_ID` empty only while using the synthetic
legacy connection. Set it to the control-plane connection ID before enabling a
managed connection so leases, cursors, status, and outbound work remain scoped
to that connection.

The `app` profile requires only `inbound`, `outbound`, and `scheduler` worker
heartbeats. The `wxbot` profile starts `wxbot-bridge-worker`; that worker's own
health check is stricter and requires the SDK status endpoint to report:

- HTTP 200 with `status=running`;
- active runtime authorization; and
- a resolved, ready bot identity.

A bridge heartbeat by itself is never treated as a healthy SDK connection.
Connection health is reported independently from core `/readyz`.

## Docker Desktop and the loopback trap

`127.0.0.1` has a different meaning inside a Linux container: it points back to
the container, not the Windows host. The Compose default therefore uses
`http://host.docker.internal:5080`.

The companion's example configuration listens on `127.0.0.1`, which Docker
Desktop cannot reach. To opt into Docker access, bind the SDK to an interface
reachable from Docker. The companion requires a high-entropy deployment token
on every interface, including loopback:

```json
{
  "api_host": "0.0.0.0",
  "api_port": 5080,
  "api_token": "replace-with-a-high-entropy-token"
}
```

Set the identical value in `COMPOSE_WXBOT_API_TOKEN`; it remains a deployment
setting and is not part of the connection form. Keep Windows Firewall scoped
to the Docker/host boundary or a trusted private network and never publish port
`5080` to the internet. A 401/403, inactive authorization, or unresolved
identity keeps only the optional bridge unready.

## Configuration and secret boundary

The connection UI and database store versioned, auditable configuration such
as display name, tenant, adapter ID, desired state, SDK URL, bounded poll/send
intervals, and participation policy. These values are entered directly in the
form and saved; operators are never asked for a configuration-file path.

The WeChat SDK adapter does not declare a platform credential. Deployments that
enable the hardened companion's optional HTTP token provide it only to the
bridge process through `COMPOSE_WXBOT_API_TOKEN`. Before sending requests, the
worker requires the configured SDK URL to match the origin of `WXBOT_SDK_URL`
or one of `CHANNEL_ALLOWED_SDK_ORIGINS`. Keep that allowlist deployment-owned;
never expose it as a tenant-editable connection field.

Vault or cloud secret manager references may be advertised only after a
deployment-specific resolver is installed for that adapter. References are
resolved by the least-privileged adapter runner/deployment layer, never read
through the browser. API responses
may expose only `secret_ref`, source, fingerprint, expiry, and rotation status.
Activation codes, SDK API tokens, device/session tokens, tenant HMAC keys, TLS
private keys, and signing keys remain in the deployment secret provider or on
the companion host.

Local WeChat paths and identity material (`wechat_data_dir`, decrypted data,
`self_wxid`, queue database, compiled-module manifest) also remain companion
configuration. The central UI may show redacted diagnostics, but must not copy
those files or credentials into Agent Console storage.

## Legacy environment migration

For compatibility, an installation that provides the old variables is mapped
to one synthetic connection for `WXBOT_DEFAULT_TENANT_ID`:

- connection ID: stable legacy WeChat connection;
- adapter ID: `wechat-sdk`;
- endpoint and intervals: copied as non-secret legacy configuration;
- platform credential: not required.

The raw token is never copied into the connection row. While the connection is
environment-managed, UI edits to those external fields are rejected/read-only.
Move to a managed connection by creating and validating a replacement, enabling
its desired state, selecting its ID in the connector deployment, starting that
connector, waiting for its heartbeat, and then probing it. Drain the legacy
connection only after the managed probe succeeds.

## Rollout and rollback

Recommended rollout:

1. Deploy the core `app` profile and verify `/readyz` without any WXBOT value.
2. Start `wxbot_client`, confirm its local `/status`, and restrict its firewall.
3. Open `/channels?adapter=wechat-sdk`, create a connection, validate its
   non-secret configuration, and enable its desired state.
4. Set `COMPOSE_CHANNEL_CONNECTION_ID` to that connection ID and start or
   restart the `wxbot` profile. One configured connection requires one
   independently configured connector process.
5. Wait for the connector heartbeat, then probe authentication and identity in
   `/channels`. Use `/wxbot` only for WeChat-specific advanced diagnostics and
   policies.

To roll back an adapter without taking down the platform, disable/drain the
connection and stop only the optional service:

```powershell
docker compose --profile wxbot stop wxbot-bridge-worker
```

Keep the core containers and database volumes running. Restore the previous
legacy connection/environment mapping if the new connection cannot be probed.
Never use `docker compose down -v` as a connector rollback.
