# 将现有 Ollama 审核后端迁移到 GPU 构建机

适配在部署层完成：保留生产网关的公网地址、鉴权和 API，将构建机 GPU 作为唯一上游，停用生产 CPU Ollama。调用方继续使用原来的 OpenAI 或 Ollama API。这里不修改审核标签、提示词或业务层的放行／阻断规则。

```text
外部调用方 → 原生产鉴权网关 → 受限 SSH 隧道 → 构建机独立 Ollama / GPU
构建机不可用 → 返回 HTTP 错误（不启动 CPU 备用）
```

## 模型与容器配置

1. 从生产实例导出模型的完整 manifest 和关联 blobs，包括模板、参数、配置和所有对外模型别名；不要只按同名标签重新下载。对每个 blob 校验 SHA256。
2. 在构建机选定目录放置独立快照。不要覆盖已有 Embedding 或其他 Ollama 服务的模型。
3. 复制 `.env.example` 为 `.env`，填写模型目录、监听端口、镜像、CPU／内存限制及并发参数。使用已验证的镜像 digest，迁移时保持与原服务器相同的 Ollama 版本。还须核对各别名 `/api/show` 返回的实际模板和参数：相同 manifest 在不同运行时可能分别选用旧 Go 模板与 GGUF 内嵌 Jinja 模板，模型摘要相同不能证明推理输入相同。
4. 启动：`docker compose --env-file .env -f compose.yaml up -d`。

`MODERATION_MODELS_DIR` 可为任意宿主机路径；容器内 `/root/.ollama/models` 是固定挂载目标。模型目录只读，容器只监听宿主机 loopback。镜像、端口、内存、CPU、并行数、队列容量和模型常驻时间均可配置。

`MODERATION_MAX_LOADED_MODELS` 默认 2，单模型可调为 1。同一 manifest 的多个别名可能被 Ollama 合并为一个驻留模型，不应按别名数量简单估算显存。需同时评估 Embedding 和审核的 GPU 总占用。

`MODERATION_PROMPT_CACHE_MB` 单独限制主机内存中的提示词缓存，默认 256 MiB，可设为 0 禁用。Ollama 0.32 的 llama-server 默认可能分配最多 8 GiB 的此类缓存，超过 4 GiB 容器限制会在不同长输入持续进入时触发 OOM。应检查启动日志确认 `LLAMA_ARG_CACHE_RAM` 已生效，并用多种长度的输入持续压测；短句测试不能覆盖这个问题。

## 隧道与预热

复制并编辑 `tunnel.env.example`，配置 SSH 主机、账号、密钥、known_hosts、两端端口、Supervisor 文件路径、构建机 Ollama URL 和模型名称列表。所有路径都是实例配置，不要求安装在指定的 `/opt` 目录。

生产 SSH 专用账号应只允许指定 loopback 端口的远程转发，不开放交互终端。使用主机指纹验证，配置 `PermitListen` 与实际地址、端口一致。本方案中网关运行在生产宿主机，故示例转发绑定 `127.0.0.1`；网关若在容器内，需根据容器网络调整绑定、权限与上游地址。

安装示例（先复制并编辑 `tunnel.env`）：

```bash
sudo install -d -m 700 /etc/agent-console
sudo install -m 600 tunnel.env /etc/agent-console/moderation-tunnel.env
sudo install -m 644 moderation-tunnel.service.example /etc/systemd/system/moderation-tunnel.service
sudo systemctl daemon-reload
sudo systemctl enable --now moderation-tunnel.service
```

配置目录可改；同时修改 unit 中的 `EnvironmentFile`。Supervisor 使用 Python 标准库，构建机需 Python 3.10+。WSL 构建机还需由 Windows 的启动安排保证 WSL 本身在线。

`tunnel_supervisor.py` 先通过并发合成请求预热模型，再确认所需模型的 digest 在 GPU 上驻留，随后启动 SSH。它每两秒检查一次；模型消失或检查失败时撤下隧道并重新预热。不同别名指向相同 digest 时允许 Ollama 合并驻留记录。冷启动期间网关返回服务不可用，不会把刚启动但尚未加载模型的进程当成已就绪的 GPU 服务。

Supervisor 不转发、保存或改写业务正文，SSH 直接承载原有 API，保持流式输出协议。健康检查与预热不能保证任意新输入都没有首次编译或缓存带来的延迟，仍需在本实例验证实际负载。

## 生产网关

`upstreams.caddy.example` 提供的是**原鉴权路由内部的 reverse_proxy 块**，不是可以单独公开的新入口。保持原监听、TLS、密钥匹配和拒绝策略，只替换上游配置。

- `MODERATION_GPU_UPSTREAM`：生产机上的隧道监听地址。
- 模板仅连接 GPU，检查 `/api/version`；模型未就绪时 Supervisor 不开放该隧道。
- 仅对列出的只读发现和推理路径允许连接故障重试，不对模型修改接口做重试。
- 构建机不可用时返回 HTTP 错误，不生成“审核通过”的伪造响应。已开始输出的流不能在中途无缝切换后端，调用方仍需处理流中断。

示例地址和端口均可替换。Caddy 的变量须在其服务环境中配置，或在部署时替换成实际地址。修改前保存原配置；先做 `caddy adapt` 检查生成的上游列表与鉴权路由，再做 `caddy validate`，通过后才 reload。不要因为语法校验通过就省略实际鉴权和转发测试。

确认公网流量已由 GPU 承担、CPU 请求已排空后，停止并禁用生产 Ollama 服务，以释放模型和缓存的内存。生产网关不配置 CPU 上游，不会因构建机离线而重新启动 CPU 推理。权重可保留供人工回滚；如确需回滚，必须明确重新启用 CPU 服务后再恢复旧路由。

## 迁移验收与回滚

在正式切换前用本地候选监听验证：

1. 错误密钥仍被拒绝，两个协议的模型发现和原有模型别名可用。
2. OpenAI 非流式／SSE、Ollama 非流式／NDJSON 均能正常结束。
3. 使用相同的冻结测试集，在串行、并发及混合负载下比较 CPU 与 GPU 的标签和延迟，不为提高分数而修改样本。切换后再次验证，出现判定退化立即回滚；不能通过反复重试挑选一次通过的结果。
4. 在审核并发负载下复验现有 Embedding／记忆召回。
5. 停止 GPU 模型，确认 Supervisor 撤下隧道、网关返回 HTTP 错误；恢复模型后自动恢复 GPU 服务。
6. 在候选路由中模拟隧道不可用，确认返回错误而不是放行，且生产 CPU 服务保持停用。

通用合成探针：

```bash
python scripts/evaluate_moderation_backend.py \
  --url http://127.0.0.1:11434 --model YOUR_MODEL \
  --timeout 60 --output cpu-report.json
python scripts/evaluate_moderation_backend.py \
  --url http://127.0.0.1:YOUR_CANDIDATE_PORT --model YOUR_MODEL \
  --api-key-file /path/to/private/key --concurrency 4 \
  --baseline-report cpu-report.json --output gpu-report.json
```

探针的 URL 是 API 根路径，不含 `/v1`。它使用 20 条合成安全／风险样本，只能验证迁移行为和这个小样本的表现，不能代表生产内容审核的全面准确率。默认要求全部请求返回有效结构、至少 90% 与预期标签一致；指定 CPU 对照时先强制核对运行时版本、模型摘要、实际模板哈希和参数，再要求至少 10 条可比样本、CPU 原本正确的样本不能退化，且任何有效 CPU 判定不能从 Unsafe 降为 Controversial／Safe，或从 Controversial 降为 Safe，即使原判定与样本预期不一致也如此。`--fingerprint-only` 只采集配置指纹。报告只记录样本 ID、分类和耗时，不写入密钥。

本目录适配的是现有外部审核网关。仓库中独立的 `plugins/prompt_audit` 模块是否被业务调用，需单独核对；部署这个网关不会自动将审核接入主应用的每条消息链路。

首次迁移时，在 CPU 服务仍运行的验收窗口中保留自动回滚：真实请求或鉴权检查失败就恢复原网关文件并 reload；全部通过后再停止并禁用 CPU 服务。正常运行中构建机故障只返回服务不可用，不自动恢复 CPU。私人生产报告放在本地 `docs/`、`artifacts/`，不要提交实例 `.env`、SSH 私钥或真实审核正文。
