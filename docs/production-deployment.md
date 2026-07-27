# Production Docker Compose deployment

## Release artifact contract

The supported production artifact is the pinned Docker/Compose image set. The
Python wheel built in CI is a package-discovery smoke test for `app` and the
built-in `plugins`; it is not a standalone deployment bundle and intentionally
does not carry `alembic.ini`, migration scripts, or the marketplace manifest.
Run migrations and services from the repository-built image so those versioned
resources remain part of one immutable release artifact.

This guide hardens the repository Compose stack for a single-host production
deployment. The base file publishes only the API and frontend to loopback. It
does not publish Postgres, Redis, Qdrant, or OpenTelemetry ports. A host reverse
proxy is expected to terminate TLS and forward requests to the loopback frontend.

## 1. Create a dedicated production environment file

Create `.env.production` outside source control. Do not copy the values from
`.env.example`: that file is a development template and intentionally contains
known `compose_dev_*` credentials.

The production overlay refuses to render unless every value below is non-empty:

| Variable | Requirement |
| --- | --- |
| `COMPOSE_POSTGRES_PASSWORD` | Unique database password. Keep it consistent with `COMPOSE_DB_DSN`. |
| `COMPOSE_DB_DSN` | Production Postgres DSN; do not use the development password. |
| `COMPOSE_REDIS_URL` | Private or authenticated Redis URL. |
| `COMPOSE_OUTBOUND_WEBHOOK_URL` | Intended HTTPS delivery endpoint. |
| `COMPOSE_OUTBOUND_HMAC_SECRET` | Unique high-entropy signing secret. |
| `COMPOSE_ADMIN_BEARER_TOKEN` | Unique high-entropy bootstrap/admin token. |
| `COMPOSE_ADMIN_SESSION_SIGNING_SECRET` | Independent 32+ character key for short-lived admin cookies and audit pseudonyms. |
| `COMPOSE_MEDIA_ID_SIGNING_SECRET` | Independent 32+ character key for opaque media IDs; never reuse admin, wxbot, or webhook credentials. |
| `COMPOSE_ADMIN_SESSION_COOKIE_SECURE` | Must be `true`; production admin login requires HTTPS. |
| `COMPOSE_TENANT_DEMO_SECRET` | Unique ingress HMAC secret; use a tenant secret store when multiple tenants are enabled. |
| `COMPOSE_MODERATION_WEBHOOK_ALLOWED_HOSTS` | Comma-separated exact hostnames only, without schemes, paths, ports, or wildcards. |
| `COMPOSE_FRONTEND_CORS_ORIGINS` | Exact deployed HTTPS frontend origins. |

Also set these production-safe controls:

```dotenv
COMPOSE_PROJECT_NAME=agent-console-prod
COMPOSE_ADMIN_SESSION_SIGNING_SECRET=<independent-random-value-of-at-least-32-characters>
COMPOSE_MEDIA_ID_SIGNING_SECRET=<different-random-value-of-at-least-32-characters>
COMPOSE_ADMIN_SESSION_COOKIE_SECURE=true
COMPOSE_ADMIN_ALLOW_BEARER_FALLBACK=false
COMPOSE_BIND_ADDRESS=127.0.0.1
COMPOSE_FRONTEND_CORS_ORIGINS=https://console.example.com
COMPOSE_MODERATION_WEBHOOK_ALLOWED_HOSTS=qyapi.weixin.qq.com
```

The production overlay also assigns explicit `agent-console-prod-*` names to
all six persistent volumes. This prevents a checkout that was previously used
for development from silently reusing its Postgres password, application
configuration, or generated assets. For an existing production installation,
set the matching `COMPOSE_PROD_*_VOLUME` values to the existing production
volume names before the first rollout; never point them at development volumes.

### Multi-tenant ingress and egress

The legacy `COMPOSE_TENANT_DEMO_SECRET`, `COMPOSE_OUTBOUND_WEBHOOK_URL`, and
`COMPOSE_OUTBOUND_HMAC_SECRET` values remain available for one-release
single-tenant compatibility. A multi-tenant deployment must instead provide
single-line JSON objects keyed by the exact tenant ID:

```dotenv
COMPOSE_TENANT_INBOUND_SECRETS='{"tenant-a":"inbound-secret-a","tenant-b":"inbound-secret-b"}'
COMPOSE_TENANT_OUTBOUND_WEBHOOK_URLS='{"tenant-a":"https://a.example.com/deliver","tenant-b":"https://b.example.com/deliver"}'
COMPOSE_TENANT_OUTBOUND_HMAC_SECRETS='{"tenant-a":"outbound-secret-a","tenant-b":"outbound-secret-b"}'
```

Once `COMPOSE_TENANT_INBOUND_SECRETS` is non-empty, an inbound request for an
unlisted tenant has no fallback secret. Once either outbound map is non-empty,
delivery for an unknown tenant—or a tenant missing either its URL or HMAC
secret—fails closed. Keep the two outbound maps on the same tenant key set and
rotate each tenant independently.

Use a password manager or deployment secret provider to generate and store the
values. Restrict read access to `.env.production`, never package or commit it,
and rotate a value immediately if it appears in logs or version control. The
repository `.dockerignore` excludes `.env` and `.env.*` (while retaining only
`.env.example`), so the production file is not sent to the Docker builder or
copied into an image layer.

## 2. Validate the resolved configuration

Render the merged configuration before every deployment:

```powershell
docker compose --project-name agent-console-prod --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml --profile app config
```

The command fails when a required production value is missing. The overlay sets
`APP_ENV=prod`, which additionally makes application startup reject known
development admin, tenant, and outbound secrets and insecure admin cookies.
Check the rendered output without storing or sharing it because it contains
resolved secrets.

Do not add `docker-compose.dev.yml` to this command. That file intentionally
publishes stateful-service ports for local debugging, although only on loopback.

## 3. Apply migrations and roll safely

Take a database backup, then run the one-shot migration service before replacing
application replicas:

```powershell
docker compose --project-name agent-console-prod --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml --profile app run --rm migrate
```

Runtime startup verifies the migration-owned table/index contract and the
`app_schema_contract` compatibility marker. It does not create or alter schema.
An exact Alembic head is intentionally not required, so an older replica can
drain during a backwards-compatible rolling upgrade. The current runtime
contract is compatibility level `4`; any future breaking migration must
bump the marker and be deployed in a coordinated maintenance window. Never
change a table incompatibly while old replicas are running.

Workers verify this compatibility contract before initialization, again at the
consume boundary, and periodically while running. A failed periodic probe marks
the worker degraded, stops consumption, and lets Compose restart it. Each Redis
heartbeat carries the process role and instance plus the worker code's schema
revision and compatibility level. Only `ready` instances appear in the API's
ready-heartbeat namespace; starting, degraded, and stopping states remain in the
separate TTL-bound liveness namespace. API readiness therefore cannot accept a
live but schema-incompatible core worker during a rolling deployment.

## 4. Start and verify

```powershell
docker compose --project-name agent-console-prod --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml --profile app up -d --build
docker compose --project-name agent-console-prod --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml --profile app ps
```

Verify through the TLS endpoint exposed by the reverse proxy. The admin session
cookie is Secure and will not work over plain HTTP. Postgres, Redis, Qdrant, and
OTLP must have no host-published address in `docker compose ps`.

The production `app` profile starts Qdrant even when knowledge features are
temporarily disabled, so enabling `COMPOSE_KNOWLEDGE_FEATURES_ENABLED=true`
cannot leave the API waiting for a service omitted by the deployment command.
The API, frontend, and three core long-running workers use `unless-stopped`
restart policies. Compose checks the API at `/readyz`, frontend HTTP, and each
worker's own Redis ready heartbeat. Core `/readyz` requires inbound, outbound,
and scheduler workers; optional message-platform connections are reported and
operated independently.

### Metrics and traces

Each Compose worker exposes Prometheus metrics on private port `9100`; no worker
metrics port is published on the host. The bundled OpenTelemetry collector
scrapes the API and the inbound, outbound, scheduler, and optional WeChat bridge
targets. A worker launched outside Compose keeps this endpoint disabled by
default (`WORKER_METRICS_PORT=0`); set a positive port and an appropriate bind
address only on a trusted monitoring network.

Compose application processes export traces to `http://otel-collector:4317` by
default. Trace resources include `service.name`, `service.instance.id`,
`process.role`, and `deployment.environment`, so spans from scaled worker
replicas remain distinguishable. Override `COMPOSE_OTEL_EXPORTER_OTLP_ENDPOINT`
only when routing to another trusted collector. OTLP and worker metrics ports
must remain private service-network endpoints.

For database or Redis maintenance, prefer `docker compose exec` or a controlled
administration network instead of publishing a port.

### Optional WeChat SDK adapter

`COMPOSE_WXBOT_API_TOKEN` is intentionally not a core production requirement.
The original SDK does not require it. Set it only when the companion has its
optional HTTP API authentication enabled:

```powershell
$env:COMPOSE_WXBOT_SDK_URL = 'http://host.docker.internal:5080'
$env:COMPOSE_CHANNEL_CONNECTION_ID = '<connection-id-from-/channels>'
docker compose --project-name agent-console-prod --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml --profile app --profile wxbot up -d --build
```

The optional bridge is healthy only when the SDK is reachable, remotely
authorized, and has resolved its bot identity. Its failure does not change core
readiness. See [`message-platform-deployment.md`](message-platform-deployment.md)
for Docker Desktop networking, secret references, legacy environment migration,
and rollback.

## 5. Rotate and recover

- Rotate the admin bootstrap token after operator changes and invalidate active
  sessions by restarting the API after the rotation.
- For an enabled WeChat connection, stage a new secret reference, verify it on
  the companion, switch the active reference, and then revoke the old token.
  Never copy the raw token into the connection database or UI.
- Rotate tenant and outbound HMAC secrets with an overlap window in the external
  sender/receiver when those systems support dual keys.
- Back up the named Postgres and Qdrant volumes before image or schema upgrades.
- Never use `docker compose down -v` during a normal rollout.
