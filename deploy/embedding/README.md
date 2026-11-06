# 可配置的构建机 Embedding 服务

该目录提供通用部署文件，不包含任何生产地址、凭据或私人验收数据。项目可以安装在任意目录。`/root/.ollama/models`、`/service/worker.py`、`/run/secrets/embedding_token` 是容器内部路径，宿主机源路径通过配置传入。

## 1. 构建机

需要 Docker Compose GPU 支持及可用的 NVIDIA GPU。把本目录复制到选定位置，然后：

```bash
cp .env.example .env
python3 - <<'PY'
import os
import secrets
with open(os.open('embedding.token', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
    f.write(secrets.token_urlsafe(48) + '\n')
PY
```

编辑 `.env`：

| 配置 | 含义 |
| --- | --- |
| `EMBEDDING_MODELS_DIR` | 已准备好的独立 Ollama 模型目录，包含 `blobs/`、`manifests/` |
| `EMBEDDING_TOKEN_PATH` | 上面生成的私有 Token 文件 |
| `EMBEDDING_WORKER_PATH` | Worker 源文件路径，默认本目录 `./worker.py` |
| `EMBEDDING_PORT` | 构建机本地监听端口，默认 11435 |
| `EMBEDDING_MODEL` | 模型标签，默认 `bge-m3:latest` |
| `EMBEDDING_DIMENSIONS` | 实际输出维度，BGE-M3 为 1024 |
| `OLLAMA_IMAGE` / `PYTHON_IMAGE` | 使用的镜像，生产建议填写验证过的 digest |

相对源路径按照 `compose.yaml` 所在目录解析。模型目录必须在启动前准备好；服务以只读方式挂载它，不会自行下载模型。可使用临时 Ollama 容器将模型拉取到选定目录，再停止临时容器；不要直接把其他服务仍在修改的模型目录当作固定快照。

```bash
docker compose --env-file .env -f compose.yaml config --quiet
docker compose --env-file .env -f compose.yaml up -d
```

启动后访问 `http://127.0.0.1:<EMBEDDING_PORT>/readyz`，等待 `ready=true`。该代理的短查询预热按 BGE-M3 tokenizer 验证；修改模型名称、维度不意味着自动兼容任意模型。更换模型需要验证预热、向量输出并重新进行召回验收。

## 2. 构建机至生产的隧道

如果模型与应用不在同一可信内网，可使用 SSH 反向隧道。配置 `tunnel.env.example` 中的生产地址、SSH 端口、专用用户、密钥、known_hosts、远程绑定地址及两端端口。确认服务器主机指纹后准备 known_hosts；不关闭主机密钥校验。

`SSH_REMOTE_BIND` 必须是应用容器可达的生产机接口地址。示例的 `172.17.0.1` 只是常见 Docker 网桥地址，部署者应检查自己的网络，不能照搬。端口也应选择未占用的端口。

生产 sshd 的专用用户需要允许对应的远程转发，并用 `PermitListen` 限定相同的绑定地址和端口。非 loopback 绑定需要服务器允许 `GatewayPorts clientspecified`。专用账号和公钥应禁用交互终端、代理转发等不需要的能力。

将编辑后的配置与服务模板安装到选定位置，例如：

```bash
sudo install -d -m 700 /etc/agent-console
sudo install -m 600 tunnel.env /etc/agent-console/embedding-tunnel.env
sudo install -m 644 embedding-tunnel.service.example /etc/systemd/system/embedding-tunnel.service
sudo systemctl daemon-reload
sudo systemctl enable --now embedding-tunnel.service
```

先从 `tunnel.env.example` 复制并编辑 `tunnel.env`。如不用 `/etc/agent-console`，同时修改 unit 中的 `EnvironmentFile`。SSH 可执行文件位置也可按发行版调整。WSL 构建机还需要由 Windows 的启动安排保证 WSL 在线；systemd 只能在 WSL 已启动后维护服务。

## 3. 应用端

在应用 Compose 使用的私有环境文件中填写以下配置，所有地址和名称均可自行指定：

```dotenv
COMPOSE_LLM_EMBED_PROVIDER=openai
COMPOSE_LLM_EMBED_BASE_URL=http://host.docker.internal:17435/v1
COMPOSE_LLM_EMBED_API_KEY=<与构建机Token文件内容一致>
COMPOSE_LLM_EMBED_MODEL=bge-m3:latest
COMPOSE_LLM_EMBED_TIMEOUT_SECONDS=4
COMPOSE_LLM_EMBED_BACKGROUND_TIMEOUT_SECONDS=30
COMPOSE_MEMORY_VECTOR_EMBED_MODEL=bge-m3:latest
COMPOSE_MEMORY_VECTOR_SIZE=1024
COMPOSE_MEMORY_VECTOR_COLLECTION=memory_bge_m3_v1
COMPOSE_MEMORY_VECTOR_INDEX_ENABLED=true
COMPOSE_MEMORY_HYBRID_RETRIEVAL_ENABLED=true
COMPOSE_MEMORY_VECTOR_TIMEOUT_SECONDS=4
```

这里的 endpoint 是格式示例，替换为应用容器实际可达的地址。如果使用上面的隧道，URL 端口须与 `SSH_REMOTE_PORT` 一致。仓库 Compose 已为应用服务配置 `host.docker.internal:host-gateway`；使用其他编排方式时需要自行保证解析及路由可达。

直接运行 Python 应用时使用对应的不带 `COMPOSE_` 前缀变量。若后台已有运行时模型覆盖配置，也需要同步更新 Embedding provider/model；后台覆盖优先于环境默认值。`openai` 表示 OpenAI 兼容协议，并不要求调用 OpenAI 官方服务。

模型名称应与构建机一致，维度必须与实际模型一致。切换模型时使用新集合并重建索引，即使维度相同也不能混用不同模型的向量。聊天 endpoint 和密钥不需要跟着修改。

## 4. 自动补偿与验收

先从 `reconcile.env.example` 复制并编辑 `reconcile.env`，设置应用目录、环境文件、Compose 文件、Compose 项目名和 API 服务名。补偿服务通过 Compose 找到运行中的服务，不依赖 `agent-console-api-1` 这样的生成容器名。

```bash
sudo install -d -m 700 /etc/agent-console
sudo install -m 600 reconcile.env /etc/agent-console/memory-vector-reconcile.env
sudo install -m 644 memory-vector-reconcile.service.example /etc/systemd/system/memory-vector-reconcile.service
sudo install -m 644 memory-vector-reconcile.timer.example /etc/systemd/system/memory-vector-reconcile.timer
sudo systemctl daemon-reload
sudo systemctl enable --now memory-vector-reconcile.timer
sudo systemctl start memory-vector-reconcile.service
sudo journalctl -u memory-vector-reconcile.service --no-pager -n 20
```

模板默认每日北京时间 02:00，可编辑 timer 的 `OnCalendar` 调整时间和时区。使用其他配置目录时同步修改 `EnvironmentFile`。如果部署依赖多个 Compose 文件，在 service 的命令中按本实例需要增加 `-f` 参数。命令只在已有容器内执行补偿，不启动或重建应用。

使用本实例有权读取的历史记录，冻结查询、预期记录 ID 和内容哈希，生成私有 fixture。运行 `scripts/evaluate_memory_recall.py --cases <私有样本路径> --output <报告路径>` 会直接使用实际运行配置。候选集合可通过 `--candidate <新集合>` 指定，模型和维度默认继承配置，或显式使用 `--candidate-model`、`--candidate-dimensions`；provider/endpoint/key 始终来自本实例配置。

fixture 顶层需要 `minimum_recall_at_5`、`minimum_paraphrase_recall_at_5` 和 `cases`。每个 case 包含唯一 `id`、`kind`（`keyword` 或 `paraphrase`）、`query`、`expected_ids`、`source_sha256` 以及 `scope`；scope 使用本实例的 `tenant_id`、`channel`、`user_id`、`session_id`、`request_session_kind`。当前隔离探针面向私聊记忆，不能把群聊可见记忆直接混入同一个私聊隔离测试集。

服务离线时会回退关键词检索，语义召回能力会下降。不要把某个实例的历史样本通过率当作另一实例的验收结果。私人样本、Token、模型文件及实例 `.env` 不应提交到开源仓库。
