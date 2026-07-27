# Docker Compose one-command deployment

This guide is for running the Agent Console core with Docker Compose on Windows.
WeChat Desktop and the standalone `wxbot_client` are optional message-platform
components and are enabled in a separate profile.

## Prerequisites

Install on the target machine:

- Windows 10/11.
- Docker Desktop with Linux containers enabled.
- Git for Windows, or a zip file of this repository.
- PowerShell 5.1 or newer.

Optional:

- A real LLM API key. Without one, the default fake provider is enough for
  stack smoke testing.

## One-command start

Open PowerShell in the repository root.

```powershell
Copy-Item .env.example .env
```

For a local demo, `.env.example` already contains usable Compose defaults. Then
start the core stack:

```powershell
docker compose --profile app up -d --build
```

This starts the default lightweight stack:

- Postgres
- Redis
- OpenTelemetry collector
- database migration job
- API
- inbound worker
- outbound worker
- frontend

No WeChat token or SDK process is required for this command. Message-platform
connections are configured separately and do not block core readiness.

Qdrant is not part of the default stack. Knowledge/vector features are optional
and can be enabled separately when needed. Postgres, Redis, OpenTelemetry, and
Qdrant stay on the private Compose network; the default command does not publish
their ports to the Windows host.

When startup finishes, open:

```text
http://127.0.0.1:4173
```

The API listens on:

```text
http://127.0.0.1:8000
```

## Optional helper script

The raw Compose command above is the primary deployment path. The repository
also includes a small PowerShell wrapper if users prefer named commands:

```powershell
.\scripts\windows-stack.ps1 start
.\scripts\windows-stack.ps1 status
.\scripts\windows-stack.ps1 health
.\scripts\windows-stack.ps1 logs
.\scripts\windows-stack.ps1 open
.\scripts\windows-stack.ps1 stop
.\scripts\windows-stack.ps1 restart
```

`start` and `restart` run the same Compose app profile and create `.env` from
`.env.example` if it does not exist.

## Environment values to edit

The Compose deployment reads `.env` from the repository root. Prefer editing
`COMPOSE_*` variables for Docker deployments because container networking is
different from host networking.

The Windows launcher fills blank `COMPOSE_ADMIN_BEARER_TOKEN`,
`COMPOSE_ADMIN_SESSION_SIGNING_SECRET`, and `COMPOSE_MEDIA_ID_SIGNING_SECRET`
entries with independent high-entropy values before startup, then prints the
administrator token. Direct `docker compose` users must generate and set those
three values themselves; never substitute a shared example credential. The
frontend uses the same-origin API proxy, while tenant and group scope come from
the authenticated backend identity.

For real LLM replies, change:

```dotenv
LLM_PROVIDER=openai
COMPOSE_OPENAI_API_KEY=your_api_key
COMPOSE_OPENAI_BASE_URL=https://api.openai.com/v1
COMPOSE_LLM_EMBED_PROVIDER=fake
```

Defaults that are fine for a local package:

- `COMPOSE_POSTGRES_PASSWORD=compose_dev_postgres_password`
- `COMPOSE_DB_DSN=postgresql+asyncpg://cs:compose_dev_postgres_password@postgres:5432/cs`
- `COMPOSE_REDIS_URL=redis://redis:6379/0`
- `COMPOSE_KNOWLEDGE_FEATURES_ENABLED=false`
- `COMPOSE_FRONTEND_CORS_ORIGINS=http://127.0.0.1:4173,http://localhost:4173`

For a machine shared with other people, replace the default outbound, tenant,
and database credentials before distributing the package. Never copy any
development credential into production. See `production-deployment.md` for
the required secret set and fail-closed production command.

## Optional loopback development ports

The application containers can reach stateful services without publishing any
host ports. When a host-side database client, Redis CLI, Qdrant client, or OTLP
debugger is needed, add the development overlay explicitly:

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile app up -d --build
```

This publishes only to `127.0.0.1`. It never makes the services reachable on
LAN interfaces. Do not include `docker-compose.dev.yml` in production.

## Optional knowledge/vector stack

The default one-command deployment disables knowledge/vector features and does
not start Qdrant. This keeps the Windows deployment lighter for ordinary users.

If you need knowledge features and Qdrant, set this in `.env`:

```dotenv
COMPOSE_KNOWLEDGE_FEATURES_ENABLED=true
COMPOSE_QDRANT_URL=http://qdrant:6333
```

Then start both profiles:

```powershell
docker compose --profile app --profile knowledge up -d --build
```

## Optional WeChat SDK adapter

The bridge is not part of the `app` profile. Start the Windows companion first
and then enable the optional profile:

```powershell
$env:COMPOSE_WXBOT_SDK_URL = 'http://host.docker.internal:5080'
$env:COMPOSE_CHANNEL_CONNECTION_ID = '<connection-id-from-/channels>'
docker compose --profile app --profile wxbot up -d --build
```

The bridge initiates requests to the SDK; the SDK does not need to push normal
messages into Agent Console. Docker's `127.0.0.1` is not Windows loopback, so a
companion listening only on `127.0.0.1:5080` cannot be reached from the
container. Bind it to a Docker-reachable interface and restrict port 5080 with
Windows Firewall. If the companion's optional HTTP API authentication is
enabled, provide the same token as `COMPOSE_WXBOT_API_TOKEN`. See
[`message-platform-deployment.md`](message-platform-deployment.md) for the
network, secret-reference, migration, and rollback contract.

## Daily commands

Start or update the full stack:

```powershell
docker compose --profile app up -d --build
```

Show containers:

```powershell
docker compose --profile app ps
```

Tail logs:

```powershell
docker compose --profile app logs -f --tail=100
```

Stop app containers while preserving data:

```powershell
docker compose --profile app stop
```

Remove containers and network while preserving named volumes:

```powershell
docker compose --profile app down
```

Do not use `down -v` unless you intentionally want to delete Postgres, Redis,
and Qdrant data.

## Health checks

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
Invoke-RestMethod http://127.0.0.1:8000/readyz
Invoke-WebRequest http://127.0.0.1:4173/ -UseBasicParsing
```

The helper script combines these:

```powershell
.\scripts\windows-stack.ps1 health
```

## Updating an installation

If installed from Git:

```powershell
git pull
docker compose --profile app up -d --build
```

If installed from a zip, unpack the new zip into a fresh directory, copy the old
`.env`, then run:

```powershell
docker compose --profile app up -d --build
```

Docker named volumes hold database and queue data. They survive normal source
updates and `docker compose down`.

## Distribution package

A simple Docker Compose distribution zip should include:

- repository files
- `.env.example`
- `docker-compose.yml`
- `docker-compose.dev.yml`
- `docker-compose.production.yml`
- `docs/windows-deployment.md`
- `docs/production-deployment.md`
- `docs/message-platform-deployment.md`
- `scripts/windows-stack.ps1`

Do not include:

- `.env` or any production `.env.*` secret file
- `docker-compose.override.yml`
- Python virtual environments
- `node_modules`
- build caches

User flow:

1. Install Docker Desktop.
2. Unzip the package.
3. Copy `.env.example` to `.env`.
4. Run `docker compose --profile app up -d --build`.
5. Open `http://127.0.0.1:4173`.

## Current limitations

- First run builds Docker images locally, so it can be slow.
- Docker Desktop is still a prerequisite.
- `.env` is not generated by the raw Compose command; copy it once before first
  start, or use `scripts/windows-stack.ps1 start`.
- External clients and adapters use explicit profiles and have independent
  connection health; they are not included in core `/readyz`.
