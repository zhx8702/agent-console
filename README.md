# Agent Console

面向群聊与多消息平台的插件化 AI 运营控制台。

统一管理消息平台连接、LLM、群参与策略、知识与成员记忆、插件能力、消息链路和失败恢复。当前内置 WeChat SDK 适配器，但核心平台不依赖微信，可以独立启动，也可以通过适配器 SPI 扩展其他消息平台。

[快速体验](#快速体验) · [核心能力](#核心能力) · [系统架构](#系统架构) · [源码开发](#源码开发) · [部署文档](#文档导航)

![Agent Console 控制台概览（演示数据）](docs/assets/readme/overview.png)

## 核心能力

| 领域 | 能力 |
| --- | --- |
| 消息接入 | 适配器目录、平台连接、健康探测、会话与参与者同步、租户与群聊作用域 |
| 机器人行为 | LLM 配置、群参与策略、回复风格、复读策略、行为模拟与链路测试 |
| 知识与记忆 | FAQ、文档知识库、混合 RAG、成员记忆、人物档案、群聊关系图 |
| Agent 与插件 | 多轮工具调用、插件 Hook、命令中心、插件安装/启停/升级与作用域策略 |
| 群运营 | 积分、内容审核、主动参与、画图、高德地图和微信管理工具 |
| 运维与恢复 | Redis Streams、事务 Outbox、重试、消息队列、DLQ 重放、指标、日志与追踪 |

管理台按“上线向导、消息接入、机器人行为、知识、群运营、能力与集成、系统运维”组织功能，常见操作无需直接调用 API。

## 基本概念

- **适配器（Adapter）**：让平台能够连接 WeChat 或其他消息提供商的可安装代码。
- **连接（Connection）**：租户实际配置的 SDK、网关或账号实例。安装适配器不等于已经建立连接。
- **会话（Conversation）**：连接下的群、频道或私聊范围。
- **参与者（Participant）**：会话中的消息平台身份。

连接的非敏感配置在控制台“平台连接”页面维护；凭据只保存在部署侧的 Secret Provider 中，控制面仅保存 `secret_ref`。

## 系统架构

```mermaid
flowchart LR
    P["消息平台"] --> A["适配器与连接"]
    A --> I["Ingress API<br/>验签 · 幂等 · 限流"]
    I --> B["Redis Streams"]
    B --> W["Inbound Worker"]
    W --> O["消息编排器"]
    O --> C["FAQ / RAG / LLM / Agent"]
    O <--> X["插件 Hooks 与工具"]
    C --> E["Outbox / Effect Intent"]
    E --> D["Outbound Worker"]
    D --> P

    UI["React 管理控制台"] --> API["FastAPI 管理 API"]
    API --> DB[(PostgreSQL)]
    O --> DB
    C -. 可选 .-> Q[(Qdrant)]
```

核心 API 与 inbound、outbound、scheduler worker 分进程运行。PostgreSQL 保存持久状态，Redis 承担队列、热状态与协调，Qdrant 仅在知识/向量能力启用时需要。

## 快速体验

推荐使用 Docker Compose。你只需要 Docker Desktop（Windows/macOS）或 Docker Engine + Compose v2（Linux）。默认配置使用离线 `fake` LLM，未配置 API Key 也能启动控制台并验证链路。

### Windows

```powershell
.\scripts\windows-stack.ps1 start
```

脚本会在缺少 `.env` 时从 `.env.example` 创建一份本地开发配置，并启动完整核心栈。

### macOS / Linux

```bash
cp .env.example .env
python3 - <<'PY'
from pathlib import Path
import re
import secrets

path = Path(".env")
text = path.read_text(encoding="utf-8")
admin_token = secrets.token_urlsafe(48)
values = {
    "COMPOSE_ADMIN_BEARER_TOKEN": admin_token,
    "COMPOSE_ADMIN_SESSION_SIGNING_SECRET": secrets.token_urlsafe(48),
    "COMPOSE_MEDIA_ID_SIGNING_SECRET": secrets.token_urlsafe(48),
}
for name, value in values.items():
    text = re.sub(rf"(?m)^{name}=.*$", f"{name}={value}", text)
path.write_text(text, encoding="utf-8")
path.chmod(0o600)
print(f"Administrator token: {admin_token}")
PY
docker compose --profile app up -d --build
```

启动完成后访问：

| 服务 | 地址 / 凭据 |
| --- | --- |
| 管理控制台 | <http://127.0.0.1:4173> |
| API | <http://127.0.0.1:8000> |
| API 文档 | <http://127.0.0.1:8000/docs> |
| 本地演示管理员令牌 | 上一步生成并写入 `.env` 的随机令牌 |

Windows 启动脚本会完成同样的生成并显示管理员令牌。三个管理侧
secret 均独立生成且只保存在被 Git 忽略的 `.env` 中；不要提交、共享或复用于生产。

默认 `app` profile 会启动 PostgreSQL、Redis、OpenTelemetry Collector、数据库迁移任务、API、三个核心 worker 和前端；不会启动 Qdrant 或微信 bridge，知识向量能力默认关闭。

常用命令：

```bash
docker compose --profile app ps
docker compose --profile app logs -f --tail=100
docker compose --profile app stop
docker compose --profile app down
```

Windows 也可以使用：

```powershell
.\scripts\windows-stack.ps1 status
.\scripts\windows-stack.ps1 health
.\scripts\windows-stack.ps1 logs
.\scripts\windows-stack.ps1 stop
```

`down` 会保留命名卷中的数据。不要在普通停止或升级时使用 `down -v`，除非你确定要删除 PostgreSQL、Redis 和 Qdrant 数据。

## 首次使用

建议按下面的顺序完成首次上线：

1. 使用本地管理员令牌登录控制台。
2. 在“模型配置”中检查 LLM 状态；本地默认是确定性的 `fake` provider。
3. 在“平台连接”中添加并验证消息平台连接。
4. 同步会话并选择要运营的群聊。
5. 配置群参与策略、回复风格、知识、记忆和所需插件。
6. 使用“链路测试”验证路由和回复结果。
7. 检查队列、DLQ、`/readyz` 与指标后再接入真实流量。

## 启用可选能力

### 真实 LLM

编辑 `.env` 中的 Compose 配置：

```dotenv
LLM_PROVIDER=openai
COMPOSE_OPENAI_API_KEY=your_api_key
COMPOSE_OPENAI_BASE_URL=https://api.openai.com/v1
COMPOSE_OPENAI_API_MODE=responses
```

然后重新创建应用容器：

```bash
docker compose --profile app up -d --build
```

项目默认使用 OpenAI-compatible Responses API，也可以连接兼容网关。Embedding 单独由 `COMPOSE_LLM_EMBED_PROVIDER` 和 `COMPOSE_LLM_EMBED_MODEL` 配置。

#### 使用 Grok（xAI）

Grok 使用同一套 OpenAI-compatible 适配器，切换时只需替换地址、密钥和模型名：

```dotenv
LLM_PROVIDER=openai
COMPOSE_OPENAI_API_KEY=xai-your_api_key
COMPOSE_OPENAI_BASE_URL=https://api.x.ai/v1
COMPOSE_OPENAI_API_MODE=responses
COMPOSE_LLM_MODEL_TIER1=grok-4.6
COMPOSE_LLM_MODEL_TIER2=grok-4.6
COMPOSE_LLM_MODEL_TIER3=grok-4.6
COMPOSE_LLM_EMBED_PROVIDER=fake
```

如果使用已有 Grok 网关，也可以直接设置 `GROK_MODELS_BASE_URL` 和 `XAI_API_KEY`；这两个变量会映射到同一套 OpenAI-compatible 配置，并覆盖对应的 `OPENAI_*` 值。

原生 xAI 地址会自动使用 xAI 的工具格式：Responses 函数工具不发送不兼容的 `strict=false`，Chat 工具结果不附加网关专用的 `call_id`，并将 `web_search_preview` 兼容为 `web_search`。如果启用联网搜索，可将 `COMPOSE_OPENAI_WEB_SEARCH_ENABLED=true`，工具名使用 `web_search` 或 `x_search`。xAI 不提供本项目所需的 Embedding 接口时，知识库保持 `fake` embedding，或另行配置支持 Embedding 的 provider。

### 知识库与 Qdrant

在 `.env` 中设置 `COMPOSE_KNOWLEDGE_FEATURES_ENABLED=true`，配置真实 Embedding provider，然后启动 knowledge profile：

```bash
docker compose --profile app --profile knowledge up -d --build
```

Qdrant 默认只在容器私有网络中可见。需要从宿主机调试 PostgreSQL、Redis、Qdrant 或 OTLP 时，显式叠加 [`docker-compose.dev.yml`](docker-compose.dev.yml)。

### 微信适配器

微信是可选扩展，不影响核心平台就绪状态：

1. 启动 Windows 侧 [`wxbot_client`](wxbot_client/README.md)。
2. 在控制台“平台连接”中创建并启用 `wechat-sdk` 连接。
3. 将连接 ID 写入 `.env` 的 `COMPOSE_CHANNEL_CONNECTION_ID`。
4. 确认 `COMPOSE_WXBOT_SDK_URL` 能从容器访问 companion。
5. 启动微信 bridge：

```bash
docker compose --profile app --profile wxbot up -d --build
```

Docker Desktop 通常使用 `http://host.docker.internal:5080` 访问宿主机 companion。完整的网络、凭据和迁移说明见[消息平台部署文档](docs/message-platform-deployment.md)。

## 源码开发

源码模式适合 Linux、macOS 或 WSL。需要：

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Node.js 20.19+ 或 22.12+
- npm
- Docker Compose v2
- Bash 与 GNU Make（使用仓库的开发脚本时）

先启动可供宿主进程访问的依赖：

```bash
cp .env.example .env
docker compose \
  -f docker-compose.yml \
  -f docker-compose.dev.yml \
  --profile knowledge \
  up -d postgres redis qdrant
```

Compose 中 PostgreSQL 的本地开发密码是 `compose_dev_postgres_password`。源码进程使用宿主地址，因此请将 `.env` 中的连接改为：

```dotenv
DB_DSN=postgresql+asyncpg://cs:compose_dev_postgres_password@127.0.0.1:5432/cs
REDIS_URL=redis://127.0.0.1:6379/0
QDRANT_URL=http://127.0.0.1:6333
```

安装依赖并启动：

```bash
uv sync --all-extras --frozen
cd frontend && npm ci && cd ..
uv run alembic upgrade head
make dev-start
make dev-health
```

源码模式地址：

- 前端：<http://127.0.0.1:5173>
- API：<http://127.0.0.1:8000>
- 本地源码模式管理员令牌：首次启动时生成，保存在 `.run/admin-bearer-token`

源码开发栈默认仅监听 loopback。开发栈进程日志位于 `.runlogs/`，PID
和本地管理 secret 位于被 Git 忽略的 `.run/`。可用
`make dev-logs`、`make dev-restart` 和 `make dev-stop` 管理。若显式设置
`HOST` 对外监听，必须同时通过环境变量提供独立的 32 字符以上管理 secret。

## 测试与质量检查

后端命令与 CI 保持一致：

```bash
uv run --all-extras --frozen ruff check
uv run --all-extras --frozen mypy
uv run --all-extras --frozen pytest tests/unit
uv run --all-extras --frozen pytest -m "not e2e"
uv run --all-extras --frozen pytest -m e2e tests/e2e
```

Integration 与 e2e 测试需要对应的 PostgreSQL、Redis 或完整测试链路。前端检查：

```bash
cd frontend
npm ci
npm run typecheck
npm test
npm run build
npm run test:e2e:install
npm run test:e2e
```

## 常用配置

根目录 [`.env.example`](.env.example) 是完整的开发配置模板。Docker 部署优先修改 `COMPOSE_*` 变量，避免把宿主机的 `localhost` 配置误用到容器中。

| 变量 | 用途 |
| --- | --- |
| `COMPOSE_ADMIN_BEARER_TOKEN` | 管理控制台登录令牌 |
| `LLM_PROVIDER` | `fake` 或 `openai` |
| `COMPOSE_OPENAI_API_KEY` | OpenAI-compatible 服务凭据 |
| `COMPOSE_OPENAI_BASE_URL` | OpenAI-compatible API 地址 |
| `COMPOSE_LLM_EMBED_PROVIDER` | Embedding provider |
| `COMPOSE_KNOWLEDGE_FEATURES_ENABLED` | 启用 FAQ/RAG/知识管理能力 |
| `COMPOSE_CHANNEL_CONNECTION_ID` | 可选 bridge 绑定的平台连接 |
| `COMPOSE_WXBOT_SDK_URL` | 微信 companion 地址 |
| `COMPOSE_WXBOT_DAILY_REPORT_FOOTER` | 可选日报尾注；默认留空，不附加品牌链接 |
| `COMPOSE_TIBO_RESET_ENABLED` | 可选组织 feed 开关；默认关闭 |
| `COMPOSE_TIBO_RESET_API_URL` | 启用组织 feed 时必须显式提供的端点 |
| `COMPOSE_FRONTEND_PORT` / `COMPOSE_API_PORT` | 宿主机监听端口 |
| `COMPOSE_OUTBOUND_WEBHOOK_URL` | 通用 webhook 出站地址 |

组织 feed 和日报尾注都是显式 opt-in：仅设置 URL 不会启动 feed，必须同时将
`COMPOSE_TIBO_RESET_ENABLED=true`；尾注为空时发送内容不会被追加任何项目品牌。

生产环境不要复制开发凭据。使用独立 `.env.production`、生产 overlay、HTTPS 和独立的数据库、管理员会话、媒体签名、租户及出站密钥；生产 Compose 会在管理 secret 缺失时直接拒绝启动，应用也会拒绝已知的开发凭据。

## 安全、隐私与许可证

公开部署或提交问题前，请先阅读[安全策略](SECURITY.md)和[隐私说明](PRIVACY.md)；
许可证见 [LICENSE](LICENSE)，依赖及其他第三方归属见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。安全漏洞请按安全策略中的私密渠道报告，不要公开披露凭据、聊天内容或运行数据。

## 项目结构

```text
app/             FastAPI 核心、消息管线、Agent、知识、可靠性与 workers
plugins/         内置业务插件与 WeChat 适配扩展
frontend/        React + TypeScript 管理控制台
migrations/      Alembic 数据库迁移
config/          路由、安全词、插件市场与 OTel 配置
tests/           unit / integration / e2e 测试
wxbot_client/    可选的 Windows 微信 SDK companion
scripts/         开发栈、评测、校验和部署辅助脚本
docs/            部署、架构、迁移和专项说明
observability/   Prometheus 告警与 Grafana dashboard
```

主要技术栈：FastAPI、Pydantic、SQLAlchemy、PostgreSQL、Redis Streams、Qdrant、React、TypeScript、Vite、OpenTelemetry 与 Prometheus。

## 文档导航

- [Windows Docker Compose 部署](docs/windows-deployment.md)
- [生产环境部署与安全检查](docs/production-deployment.md)
- [消息平台连接、Secret 与微信 bridge](docs/message-platform-deployment.md)
- [插件架构优化说明](docs/plugin-architecture-optimization-2026-07.md)
- [Agent 实现计划与架构说明](docs/agent-implementation-plan.md)
- [群聊关系记忆设计](docs/group-relationship-memory.md)
- [前端开发说明](frontend/README.md)

管理 API 会随启用的能力和插件动态变化，请以运行实例的 `/docs` 或 `/openapi.json` 为准，不在 README 中维护容易过期的端点清单。
