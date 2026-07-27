# 插件市场与热插拔迁移方案

本文档记录当前插件系统向“插件市场 + 配置级热启停 + 安装后软重启”演进的实施方案。目标是先把现有插件纳入统一状态管理和可视化管理，再逐步支持安装、升级、卸载和受控重启。

## 背景

当前插件系统已经具备基础扩展点：

- 插件基类：`app/plugin/base.py`
- 插件发现与初始化：`app/plugin/registry.py`
- Pipeline Hook：`app/plugin/hooks.py`
- Agent Tool Registry：`app/agent/registry.py`
- 插件路由挂载：`app/main.py`
- 插件概览接口：`/v1/admin/plugins/summary`
- 插件前端概览页：`frontend/src/pages/PluginsPage.tsx`

现有插件通过 `plugins/{name}/plugin.py` 暴露 `plugin` 实例，启动时发现并初始化。插件可以提供：

- API router
- Pipeline hooks
- Agent tools
- Capability engines
- 后台任务
- 自有 store 和配置接口

当前缺口：

- 插件只有启动时加载，没有统一状态存储。
- Hook、Agent tools、Commands 缺少按插件 owner 反注册能力。
- 插件路由通过 `FastAPI.include_router()` 启动时挂载，不适合运行时安全卸载。
- 前端插件页对插件 runtime 状态和配置页有较多硬编码。
- 安装新插件代码后当前架构仍需要重启才能可靠加载。

## 目标

第一阶段目标不是直接实现任意 Python 插件真热加载，而是实现：

- 已有插件统一纳入 PluginStateStore。
- 现有插件默认保持启用，当前业务行为不变。
- 支持全局插件启停状态；租户/群级启停只预留数据模型，后续阶段再接执行链路。
- 支持 Hook / Agent Tool / Command owner 化注册与反注册。
- 插件可以暴露 config schema 和 runtime status。
- 前端提供插件市场/已安装插件入口。
- 新插件安装或升级后标记 `restart_required`，通过一键软重启生效。
- 配置级启停尽量热生效，代码级安装升级先走软重启。

非目标：

- 第一阶段不做不可信第三方 Python 代码沙箱。
- 第一阶段不做任意插件代码无重启真热替换。
- 第一阶段不强行统一所有插件业务配置表。
- 第一阶段不删除已有插件自有配置 API。

## 设计原则

1. 兼容优先

   所有已有插件首次接入状态管理时默认 `installed=true`、`enabled=true`，避免影响当前微信、积分、高德、绘图等链路。

2. 状态和业务配置分离

   `PluginStateStore` 管插件安装、启用、错误、是否需要重启。插件自己的业务配置继续由原 store 管理，例如 credits、moderation、repeater、commands 等。

3. 先逻辑启停，后物理卸载

   第一版可以通过 enabled 状态过滤 hooks/tools/commands 的执行与展示，不强制 shutdown 插件对象。等 owner 反注册完善后，再支持真正 disable 时清理注册物。

4. 系统插件保护

   `commands` 和 `wxbot` 属于基础设施插件，第一版应标记为 `system=true`。`commands` 是 hard system plugin，禁用和卸载接口必须返回 409；`wxbot` 第一版也默认禁止禁用，后续如开放给 super admin，需要单独定义强确认和降级语义。

5. 安装代码需要软重启

   新插件代码安装、升级、卸载后设置 `restart_required=true`。通过管理端按钮重启相关服务，使启动时 discover/init 流程重新执行。

## 当前已有插件处理策略

| 插件 | 类型 | 第一版默认状态 | 处理策略 |
|---|---|---|---|
| `commands` | system | enabled | 命令中心。固定启用，不允许普通禁用。先做 owner 化命令注册。 |
| `wxbot` | system | enabled | 微信渠道适配。第一版固定启用，可显示状态，禁用能力后续单独设计。 |
| `credits` | normal | enabled | 接入状态和 runtime status。保留现有积分配置表。 |
| `draw` | normal | enabled | `/draw`、`/redraw` 命令按 owner 注册，禁用后命令和 agent tools 不可用。 |
| `amap` | normal | enabled | Agent tools 多，禁用后工具不出现在工具目录。 |
| `memory` | normal | enabled | hooks 按 owner 注册，禁用后不注入记忆上下文、不持久化。 |
| `moderation` | normal | enabled | 保留每群配置，增加插件级启停。 |
| `persona_extract` | normal | enabled | 暴露 profiles/jobs/running_jobs runtime status。 |
| `repeater` | normal | enabled | 保留每群配置，增加插件级启停。 |

首次启动时 reconcile 逻辑：

```python
for plugin in discovered_plugins:
    state = await store.get(plugin.name)
    if state is None:
        await store.create(
            plugin_name=plugin.name,
            version=plugin.version,
            source="builtin",
            installed=True,
            enabled=True,
            system=plugin.name in {"commands", "wxbot"},
            status="active",
            restart_required=False,
        )
    else:
        await store.update_discovered_metadata(
            plugin_name=plugin.name,
            version=plugin.version,
            description=plugin.description,
        )
```

### 启动顺序

当前真实链路是 `app/main.py` 中先 discover 插件代码，再 initialize 插件，最后 include 插件路由。接入状态管理后必须把状态决策插入 discover 和 initialize 之间，顺序固定为：

```text
discover plugin code
-> reconcile plugin_state
-> filter installed/enabled plugins for initialize/register
-> initialize active plugins in dependency order
-> register hooks/tools/commands/capabilities
-> mount routes for active plugins
```

约束：

- `discover plugin code` 只负责找到可 import 的插件对象和 `PluginMeta`，不注册业务能力。
- `reconcile plugin_state` 只 upsert 已发现插件的元数据，不覆盖管理员手动设置的 `enabled=false`。
- `installed=false` 的插件跳过 initialize、hooks、tools、commands、capabilities 和 routes。
- `enabled=false` 的插件第一版跳过 initialize 和业务注册；如果插件需要展示只读 metadata，只能来自 `PluginStateStore` 或 manifest，不能执行插件业务代码。
- 插件 initialize 抛异常时只标记该插件 `status=failed`，其他插件继续启动。
- routes 在启动期一次性挂载；运行时 disable 不承诺从 FastAPI 路由表移除。

## 数据模型

### plugin_state

全局插件安装状态。

```sql
CREATE TABLE plugin_state (
    plugin_name        VARCHAR(128) PRIMARY KEY,
    version            VARCHAR(64) NOT NULL DEFAULT '',
    source             VARCHAR(64) NOT NULL DEFAULT 'builtin',
    installed          BOOLEAN NOT NULL DEFAULT TRUE,
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    system             BOOLEAN NOT NULL DEFAULT FALSE,
    status             VARCHAR(32) NOT NULL DEFAULT 'active',
    restart_required   BOOLEAN NOT NULL DEFAULT FALSE,
    last_error         TEXT NOT NULL DEFAULT '',
    metadata_json      JSONB NOT NULL DEFAULT '{}',
    installed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

字段语义：

- `installed`：插件是否安装到系统状态中。`false` 表示不参与启动和管理页默认已安装列表，但仍可保留审计和业务数据。
- `enabled`：管理员是否允许插件运行。`false` 表示禁止 initialize 和业务能力注册。
- `status`：当前运行态，只描述 runtime lifecycle，不重复表达安装事实。

建议状态值：

- `active`：已安装、已启用，当前进程初始化和注册成功。
- `disabled`：已安装但管理员禁用，当前进程不应执行业务能力。
- `failed`：初始化、注册或启用失败，`last_error` 必须记录原因。
- `pending_restart`：状态或代码变更已写入，但当前进程需要重启才能完全反映。
- `incompatible`：manifest 或 runtime meta 与当前 `PLUGIN_API_VERSION` 不兼容。
- `unknown`：前端兜底展示值，不应由后端主动写入。

### plugin_scope_state

后续用于租户/群级启停。

```sql
CREATE TABLE plugin_scope_state (
    tenant_id      VARCHAR(128) NOT NULL,
    session_id     VARCHAR(256) NOT NULL DEFAULT '',
    plugin_name    VARCHAR(128) NOT NULL,
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    config_json    JSONB NOT NULL DEFAULT '{}',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, session_id, plugin_name)
);
```

生效规则：

```text
global enabled=false -> 插件不可用
global enabled=true + no scope row -> 默认可用
global enabled=true + scope enabled=false -> 该租户/群禁用
global enabled=true + scope enabled=true -> 该租户/群启用
```

### plugin_events

用于审计安装、启用、禁用、升级、重启。

```sql
CREATE TABLE plugin_events (
    id              BIGSERIAL PRIMARY KEY,
    plugin_name     VARCHAR(128) NOT NULL,
    event_type      VARCHAR(64) NOT NULL,
    status          VARCHAR(32) NOT NULL DEFAULT 'ok',
    actor_id        TEXT NOT NULL DEFAULT '',
    actor_type      VARCHAR(32) NOT NULL DEFAULT 'admin',
    request_id      VARCHAR(128) NOT NULL DEFAULT '',
    ip_address      VARCHAR(64) NOT NULL DEFAULT '',
    message         TEXT NOT NULL DEFAULT '',
    metadata_json   JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### plugin_marketplace_cache

用于缓存 marketplace manifest，避免每次打开市场页都访问远端源。第一版如果只使用本地 manifest，可以先不建表，直接从文件读取。

```sql
CREATE TABLE plugin_marketplace_cache (
    plugin_name       VARCHAR(128) PRIMARY KEY,
    version           VARCHAR(64) NOT NULL DEFAULT '',
    manifest_json     JSONB NOT NULL DEFAULT '{}',
    source_url        TEXT NOT NULL DEFAULT '',
    checksum          VARCHAR(128) NOT NULL DEFAULT '',
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

建议索引：

```sql
CREATE INDEX ix_plugin_events_plugin_created ON plugin_events(plugin_name, created_at DESC);
CREATE INDEX ix_plugin_events_type_created ON plugin_events(event_type, created_at DESC);
CREATE INDEX ix_plugin_scope_state_plugin ON plugin_scope_state(plugin_name);
```

`status`、`source`、`event_type`、`disable_mode` 如果不用数据库 enum，后端必须集中定义常量，禁止在 router、store、frontend 中散落字符串。

## Marketplace Manifest 规范

市场插件需要有稳定 manifest，供后端展示、兼容性判断、权限确认和安装校验使用。第一版建议使用本地文件：

```text
config/plugin-marketplace.yaml
```

后续再支持远端 marketplace index。manifest 建议结构：

```yaml
items:
  - name: amap
    display_name: 高德地图
    version: 0.1.0
    description: AMap personal map, POI search, and route planning agent tools
    author: builtin
    source: builtin
    package:
      type: builtin
      uri: plugins/amap
      checksum: ""
      signature: ""
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
      python: ">=3.11"
    dependencies:
      - name: commands
        version: ">=0.1.0"
        required: true
    permissions:
      - id: network:amap
        level: external
        description: 访问高德地图 API
      - id: storage:plugin
        level: local
        description: 写入插件本地缓存和生成文件
    capabilities:
      routes: ["/plugins/amap"]
      hooks: []
      agent_tools: ["amap_geo", "amap_text_search"]
      commands: []
    config_schema:
      type: object
      properties:
        api_key:
          type: string
          title: API Key
          secret: true
    restart_policy: required_after_install
```

字段要求：

- `name`：插件唯一标识，只允许小写字母、数字、下划线，必须和 Python package 名一致。
- `version`：插件版本，第一版必须符合 PEP 440，后端使用 `packaging.version.Version` 比较；非法版本视为 `invalid_manifest`。
- `source`：插件来源，枚举为 `builtin` / `marketplace` / `local`。
- `package.type`：包获取和安装方式，第一版支持 `builtin` 和 `local_archive`，后续扩展 `git`、`wheel`、`container`。
- `checksum`：安装包校验值。远端插件必须提供，内置插件可为空。
- `signature`：后续用于签名校验。第一版可预留字段但不强制。
- `compatibility.core_api`：声明插件依赖的主系统插件 API 版本。
- `dependencies`：声明运行依赖，依赖未安装或 disabled 时禁止启用当前插件。
- `permissions`：安装前必须展示给管理员确认。
- `restart_policy`：`none` / `required_after_install` / `required_after_upgrade` / `always_required`。`none` 第一版只适用于不加载新代码的配置级变更，不适用于 Python 插件安装或升级。

manifest 与 `PluginDescriptor` / runtime introspection 结果要做一致性校验：`name`、`version`、`capabilities` 不一致时，市场页显示告警，安装/升级流程应拒绝继续或标记为 `failed`。

依赖规则：

- 启动初始化顺序必须按 required dependencies 拓扑排序，依赖插件先 initialize。
- 依赖未安装：禁止启用当前插件，返回 409 `plugin_dependency_not_installed`。
- 依赖已安装但 disabled：禁止启用当前插件，返回 409 `plugin_dependency_disabled`，并提示先启用依赖。
- 禁用被其他 enabled 插件依赖的插件：返回 409 `plugin_has_enabled_dependents`，第一版不支持 `force` 联动禁用。
- 卸载被其他 installed 插件依赖的插件：返回 409 `plugin_has_dependents`。
- 可选依赖 `required=false` 不阻塞启用，但 runtime view 必须展示 missing optional dependency。

## 数据库迁移策略

表结构不建议只依赖运行时 `ensure_tables()` 静默创建。第一版可以保留 `ensure_tables()` 作为本地开发兜底，但生产环境应通过正式 migration 创建：

- 新增 `plugin_state`、`plugin_scope_state`、`plugin_events` migration。
- `plugin_marketplace_cache` 只有启用远端市场缓存时再建。
- 启动时 `ensure_tables()` 只做幂等校验和缺表提示，不应在生产环境自动修改已有表结构。
- migration 只建表、索引和必要约束，不 import 运行时代码，不 discover `plugins/`，不触发插件 import 副作用。
- startup reconcile 负责把当前 discovered plugins upsert 到 `plugin_state`，并且不得重置用户禁用状态。
- 可选 seed 只能针对 known builtin plugin names 做静态初始行；即使做 seed，也必须以 startup reconcile 的 runtime metadata 为准。
- 状态字段新增值时要保持向后兼容，未知状态在前端显示为 `unknown` 并提示检查后端版本。

## PluginStateStore

建议新增：

```text
app/plugin/state.py
```

核心接口：

```python
class PluginStateStore:
    async def ensure_tables(self) -> None: ...
    async def reconcile_discovered(self, plugins: list[PluginDescriptor]) -> None: ...
    async def list_states(self) -> list[PluginState]: ...
    async def get_state(self, plugin_name: str) -> PluginState | None: ...
    async def set_enabled(self, plugin_name: str, enabled: bool) -> PluginState: ...
    async def mark_failed(self, plugin_name: str, error: str) -> None: ...
    async def mark_restart_required(self, plugin_name: str, required: bool = True) -> None: ...
    async def append_event(self, plugin_name: str, event_type: str, **kwargs) -> None: ...
```

第一版建议使用 Postgres。DB 不可用时按环境区分：

- dev/test：允许退化到 `InMemoryPluginStateStore`，只读展示和本地测试可继续；写操作必须返回 warning，说明状态不可持久化。
- prod：插件管理写操作必须返回 503 `plugin_state_store_unavailable`；只读接口可以返回 degraded view，但必须带 `state_store_available=false`。

Scope 状态第一版只预留表结构，不进入验收；真正实现租户/群级启停时再补接口：

```python
async def get_effective_state(plugin_name: str, tenant_id: str, session_id: str = "") -> PluginEffectiveState: ...
async def set_scope_enabled(plugin_name: str, tenant_id: str, session_id: str, enabled: bool) -> PluginScopeState: ...
async def clear_scope_state(plugin_name: str, tenant_id: str, session_id: str) -> None: ...
```

同时必须定义 hooks/tools/commands 执行时如何从消息上下文取得 `tenant_id` 和 `session_id` 做过滤。

## PluginManager

建议新增：

```text
app/plugin/manager.py
```

职责边界：

- `PluginRegistry` 是低层注册表，负责 discover/load/import 插件代码，并提供 owner-aware hooks/tools/commands/capabilities 注册表。
- `PluginManager` 是状态决策层，负责根据 `plugin_state`、manifest 和依赖关系决定哪些插件 active，聚合 admin API view，并编排 enable/disable/install/upgrade/uninstall。
- `AdminRouter` 只调用 `PluginManager`，不直接读写 `PluginRegistry` 或 `PluginStateStore`。
- `PluginManager` 持有 active plugin map 和 runtime view；`PluginRegistry` 不自行判断管理员启停状态。
- 插件 initialize/shutdown 由 `PluginManager` 编排，具体注册动作仍委托 `PluginRegistry`。
- routes 列表由 `PluginRegistry` discover 阶段暴露，是否 mount 由启动期 `PluginManager` active 决策决定。

核心接口：

```python
class PluginManager:
    async def reconcile_startup(self) -> None: ...
    async def list_installed(self) -> list[PluginRuntimeView]: ...
    async def get_plugin(self, name: str) -> PluginRuntimeView | None: ...
    async def enable(self, name: str, actor: PluginActor) -> PluginRuntimeView: ...
    async def disable(self, name: str, actor: PluginActor) -> PluginRuntimeView: ...
    async def reload(self, name: str, actor: PluginActor) -> PluginRuntimeView: ...
    async def marketplace(self) -> list[MarketplaceItem]: ...
    async def install(self, request: PluginInstallRequest, actor: PluginActor) -> PluginRuntimeView: ...
    async def uninstall(self, name: str, actor: PluginActor) -> PluginRuntimeView: ...
```

第一版 `install/uninstall` 可以只落状态和 `restart_required`，实际包安装后续再接。

核心 schema 草稿：

```python
@dataclass
class PluginDescriptor:
    name: str
    display_name: str
    version: str
    description: str
    system: bool
    source: str
    capabilities: dict[str, list[str]]
    dependencies: list[PluginDependency]
    routes: list[str]

@dataclass
class PluginRuntimeView:
    name: str
    display_name: str
    version: str
    source: str
    installed: bool
    enabled: bool
    system: bool
    status: str
    restart_required: bool
    disable_mode: str
    disable_note: str
    last_error: str
    capabilities: dict[str, list[str]]
    runtime_status: dict[str, Any]
    config_schema: dict[str, Any]
    admin_ui: dict[str, Any]
    dependencies: list[PluginDependencyView]

@dataclass
class MarketplaceItem:
    name: str
    display_name: str
    version: str
    source: str
    package_type: str
    installed: bool
    installed_version: str
    compatible: bool
    status: str
    permissions: list[PluginPermission]
    dependencies: list[PluginDependency]
    restart_policy: str
    warnings: list[str]
```

API 响应统一使用 `runtime_status` 字段，避免后端返回 `runtime`、前端使用 `runtime_status` 的命名分裂。

## 安装、升级、卸载流程

### 安装流程

第一版市场安装可以先只支持内置 manifest 和本地插件包，不直接拉取任意远端代码。`local_archive` 第一版只支持 `.zip`，暂不支持 `.tar.gz`，降低 symlink、hardlink 和 device file 处理复杂度。`local_archive.uri` 只允许引用服务端已存在的 staging/upload 文件，不支持任意 URL 或任意文件路径；上传接口另行定义，或由运维预置文件。

本地 zip 最低安全要求：

- 解压前枚举全部 entry，拒绝绝对路径、空路径、包含 `..` 的路径和 Windows drive 路径。
- 拒绝 symlink、hardlink、device file、FIFO 等非普通文件或目录 entry。
- 限制总文件数、单文件大小和总解压大小。
- archive root 必须是单一 `plugin_name/` 目录，且必须包含 `plugin_name/plugin.py`。
- `plugin_name` 必须和 manifest `name`、Python package 名一致。
- 临时目录和正式 `plugins/` 目录必须在同一 filesystem，以便 atomic rename；如果无法保证，只能使用 copy + fsync + final rename，并在文档和事件中标记 `atomic_move=false`。
- checksum 校验必须在解压前基于原始 zip 文件执行；解压后还要校验目录结构。

推荐流程：

1. 读取 marketplace manifest。
2. 校验 `name`、`version`、`compatibility`、`permissions`。
3. 检查是否已安装；已安装同版本直接返回当前状态。
4. 记录 `plugin_events: install_requested`。
5. 如果是 `builtin`，只写入 `plugin_state`。
6. 如果是 `local_archive`，先解压到临时目录，校验 checksum 和 manifest，再移动到插件目录。
7. 写入 `plugin_state(installed=true, enabled=false, restart_required=true, status=pending_restart)`。
8. 记录 `plugin_events: install_succeeded`。
9. 返回安装结果和重启提示。

失败处理：

- 校验失败：不写入 `plugin_state`，记录 `install_failed`。
- 写文件失败：清理临时目录，保留原插件目录不变。
- 已存在旧版本：不得覆盖原目录，升级必须走 upgrade 流程。
- DB 写入失败：不得移动插件包到正式目录，避免代码和状态不一致。

### 升级流程

升级必须可回滚，不能直接覆盖当前可用版本：

1. 检查当前插件是否 installed。
2. 使用 `packaging.version.Version` 校验目标版本大于当前版本，除非管理员显式选择 reinstall。
3. 下载或读取新包到 staging 目录。
4. 校验 checksum、manifest、兼容性和权限变化。
5. 权限增加时要求管理员再次确认。
6. 将当前版本目录标记为 backup，新版本放入 pending 目录。
7. 更新 `plugin_state.version`、`restart_required=true`、`status=pending_restart`。
8. 重启后 discover 成功再清理 backup。
9. 重启后 discover 失败则回滚到 backup，并写入 `last_error`。

第一版如果不做真实文件升级，可以只实现状态级 upgrade preview：返回目标版本、权限变化、兼容性结果和 `restart_required`。

### 启用/禁用流程

disable 事务语义：

1. 校验插件 installed、enabled、system、dependents 和 `restart_required`。`commands` 必须返回 409 `system_plugin_cannot_be_disabled`。
2. 写入 `plugin_events:disable_requested`，包含 actor、request_id 和调用来源。
3. DB 设置 `enabled=false`、`status=disabled`。
4. 调用 `plugin.on_disable()`，再按 owner 反注册 hooks/tools/commands；capabilities 和 routes 按下方边界处理。
5. 运行时清理全部成功：写入 `disable_succeeded`，返回 `disable_mode=runtime_filtered`。
6. 部分清理失败或存在无法卸载的 routes/capabilities：保留 `enabled=false`，写入 `disable_partial`，返回 `disable_mode=partial` 或 `restart_required`，并记录 `last_error`。
7. disable 不因为运行时清理失败而自动回滚 `enabled=true`，避免管理员已禁用的插件继续执行新请求。

里程碑 1-3 的 enable 事务语义：

1. 校验 installed、compatible、依赖已安装且 enabled，且不存在该插件未处理的 `restart_required`。
2. 写入 `plugin_events:enable_requested`。
3. DB 设置 `enabled=true`、`status=pending_restart` 或临时 enabling 状态。
4. 如果插件对象在当前进程尚未 initialize，必须先调用 `plugin.initialize(ctx)`；成功后再调用 `plugin.on_enable()` 并重新注册 hooks/tools/commands。如果无法安全 initialize，返回 `disable_mode=restart_required` 或设置 `restart_required=true`，要求进程重启。
5. 全部成功：DB 设置 `status=active`，写入 `enable_succeeded`。
6. 失败：DB 回滚 `enabled=false`、`status=failed`，写入 `enable_failed` 和 `last_error`。

运行时禁用边界：

- hooks/tools/commands：owner 化后可运行时移除或过滤。
- capability_engines：第一版如果仍 merge 到全局 map，disable 后返回 `disable_mode=restart_required`；后续需要 `CapabilityRegistry.unregister_owner()` 才能运行时移除。
- api routes：第一版 disable 不会从 FastAPI 路由表移除。禁用后插件路由必须通过统一依赖检查 `enabled`，或者返回 `disable_mode=restart_required` 表示需要重启才完全生效。
- 后台任务：第一版 disable 不强制取消已运行任务；插件可以 best-effort cancel 自己的 pending jobs。runtime view 应展示 `draining` 或 `pending_jobs`，后续再补完整 lifecycle。

### 卸载流程

卸载分两种语义：

- `disable`：保留代码和配置，只让插件不可用。
- `uninstall`：移除插件代码或标记不再安装，保留审计事件。

第一版建议卸载内置插件时只设置 `installed=false`、`enabled=false`、`restart_required=true`，不删除代码。市场插件后续再支持删除插件目录。

卸载保护：

- `system=true` 插件默认不允许卸载。
- 被其他插件依赖时返回 409，并列出依赖方。
- 卸载不默认删除插件业务数据，例如 credits 积分表、memory 记忆表。
- 如需删除业务数据，应提供单独的危险操作，并要求二次确认。

## 权限与安全模型

第一阶段不做不可信 Python 沙箱，因此市场默认只应接入可信来源插件。即使如此，也需要把权限声明和审计先设计进去。

Admin 权限第一版只实现 bearer token 管理员，不引入多角色授权模型。文档中提到的 `super_admin`、`force`、`strong_confirm` 暂不进入阶段 1-3；涉及 system 插件禁用、删除业务数据、强制联动禁用 dependents 的接口第一版统一返回 409。

审计 actor 规则：

- bearer token 模式下 `actor_id` 可写 token 名称、配置中的 admin 标识或空字符串。
- `actor_type` 第一版固定为 `admin`。
- `request_id` 优先读取请求头 `X-Request-ID`，没有则由后端生成。
- `ip_address` 记录调用方 IP；无法可信取得时留空。

权限分类建议：

| 权限 | 含义 | 示例 |
|---|---|---|
| `network:*` | 访问外部网络服务 | `network:amap`、`network:openai` |
| `storage:plugin` | 读写插件私有目录 | draw 文件缓存 |
| `storage:shared` | 读写系统共享存储 | 知识库、会话、消息记录 |
| `agent_tools` | 注册 Agent tools | amap tools、draw tools |
| `commands` | 注册命令 | `/draw`、`/积分` |
| `hooks:pipeline` | 拦截消息流水线 | memory、moderation、credits |
| `billing` | 扣费或发放积分 | credits |
| `admin_api` | 暴露管理端 API | wxbot、commands |
| `runtime:restart` | 请求运行时重启 | 市场管理插件 |

安全要求：

- 安装前展示权限列表，管理员必须确认新增权限。
- 启用插件时再次展示高风险权限，例如 `storage:shared`、`billing`、`runtime:restart`。
- 所有 install、upgrade、uninstall、enable、disable、restart 操作写入 `plugin_events`，至少包含 `actor_id` 和 `request_id`。
- 市场源必须可配置 allowlist。第一版只允许本地 manifest，远端源后续再开放。
- 远端包必须校验 checksum；签名校验作为第二阶段能力。
- 插件管理 API 只能由管理员访问，不能暴露给普通消息入口。
- 插件名不能包含路径分隔符，安装包解压必须防 Zip Slip。
- 插件私有文件访问要限制在插件目录内，禁止通过 `../` 访问系统文件。

## 版本兼容策略

需要给插件系统定义独立 API 版本，例如：

```python
PLUGIN_API_VERSION = "0.1.0"
```

兼容规则：

- manifest 的 `compatibility.core_api` 必须包含当前 `PLUGIN_API_VERSION`。
- 不兼容插件在市场页显示为 `incompatible`，禁止安装和升级。
- 旧插件缺少 `get_config_schema()`、`get_runtime_status()` 等方法时，由 `Plugin` 基类默认实现兜底。
- `PluginMeta` 新增字段必须有默认值，避免旧插件启动失败。
- 删除或重命名插件扩展点前，先提供至少一个小版本的兼容窗口。
- owner 化注册完成前，不应开放“禁用后完全卸载注册物”的承诺。

## 插件接口扩展

在 `Plugin` 基类上增加默认方法，保证旧插件无需立刻实现：

```python
class Plugin:
    def get_config_schema(self) -> dict[str, Any]:
        return {}

    async def get_runtime_status(self) -> dict[str, Any]:
        return {}

    def get_admin_ui(self) -> dict[str, Any]:
        return {}

    def get_permissions(self) -> list[str]:
        return []

    async def on_enable(self, scope: PluginScope | None = None) -> None:
        return None

    async def on_disable(self, scope: PluginScope | None = None) -> None:
        return None
```

后续插件 manifest 可统一映射到这些字段。

## Owner 化注册与反注册

### HookRunner

当前 HookRunner 只存 hook，不存 owner。改造后：

```python
@dataclass
class HookEntry:
    owner: str
    name: str
    point: HookPoint
    priority: int
    hook: PipelineHook
```

新增：

```python
def register(self, hook: PipelineHook, *, owner: str = "") -> None: ...
def unregister_owner(self, owner: str) -> int: ...
def owner_summary(self) -> dict[str, list[str]]: ...
```

`PluginRegistry._register_plugin_hooks()` 改成：

```python
self._hook_runner.register(hook, owner=name)
```

### AgentToolRegistry

当前注册已有 owner metadata，但缺反注册。新增 owner index：

```python
self._owners: dict[str, set[tuple[str, str]]]
```

新增：

```python
def unregister_owner(self, owner: str) -> int: ...
def catalog_by_owner(self) -> dict[str, list[dict[str, Any]]]: ...
```

禁用 `amap` 时应移除该 owner 下所有高德工具。

### CommandRegistryService

当前 command definitions 没有 owner 维度。建议：

```python
def register(self, definitions: list[CommandDefinition], *, owner: str = "") -> None: ...
def unregister_owner(self, owner: str) -> int: ...
```

每个 command token 记录 owner。禁用 `draw` 时移除：

- `/draw`
- `/画图`
- `/redraw`
- `/重绘`

### BillingCoordinator

后续也应支持：

```python
register_provider(provider, owner="credits")
unregister_owner("credits")
```

第一版可以先保留现状，因为 credits 是核心插件，禁用时先逻辑跳过扣费。

### CapabilityRegistry

当前 capability engines 在启动时 merge 到全局 map。第一版不要求运行时反注册 capability engine，禁用提供 capability 的插件时返回 `disable_mode=restart_required` 或 `partial`。

后续建议新增：

```python
def register_engine(name: str, engine: CapabilityEngine, *, owner: str) -> None: ...
def unregister_owner(owner: str) -> int: ...
def active_engines() -> dict[str, CapabilityEngine]: ...
```

## API 设计

### 已安装插件

```http
GET /v1/admin/plugins/installed
```

返回：

```json
{
  "state_store_available": true,
  "items": [
    {
      "name": "amap",
      "display_name": "高德地图",
      "version": "0.1.0",
      "description": "AMap personal map, POI search, and route planning agent tools",
      "source": "builtin",
      "system": false,
      "installed": true,
      "enabled": true,
      "status": "active",
      "restart_required": false,
      "disable_mode": "runtime_filtered",
      "disable_note": "Hooks and tools are owner-aware; routes require startup-time mount decisions.",
      "last_error": "",
      "capabilities": {
        "routes": ["/plugins/amap"],
        "hooks": [],
        "agent_tools": ["amap_geo", "amap_text_search"],
        "commands": []
      },
      "runtime_status": {
        "api_key_configured": true,
        "storage_dir_writable": true,
        "agent_tools": 15
      },
      "admin_ui": {
        "type": "schema_form"
      }
    }
  ]
}
```

### 启用/禁用

```http
POST /v1/admin/plugins/{name}/enable
POST /v1/admin/plugins/{name}/disable
```

系统插件禁用返回 409：

```json
{
  "detail": "system_plugin_cannot_be_disabled"
}
```

### 配置 schema

```http
GET /v1/admin/plugins/{name}/config-schema
```

### runtime status

```http
GET /v1/admin/plugins/{name}/runtime
```

### 市场

第一版可以使用本地 marketplace manifest：

```http
GET /v1/admin/plugins/marketplace
```

后续支持安装：

```http
POST /v1/admin/plugins/install
POST /v1/admin/plugins/{name}/uninstall
POST /v1/admin/plugins/{name}/upgrade
```

`POST /v1/admin/plugins/install` 请求体：

```json
{
  "name": "some_plugin",
  "source": "local",
  "version": "0.2.0",
  "package_type": "local_archive",
  "uri": "uploads/some_plugin-0.2.0.zip",
  "checksum": "sha256:...",
  "confirm_permissions": ["network:example"],
  "confirm_restart_required": true
}
```

第一版不支持 `force`、`strong_confirm` 或安装远端 URL。

安装/升级结果：

```json
{
  "name": "some_plugin",
  "installed": true,
  "enabled": false,
  "restart_required": true
}
```

安装前建议提供 dry-run/preview，用于前端确认权限和兼容性：

```http
POST /v1/admin/plugins/install/preview
POST /v1/admin/plugins/{name}/upgrade/preview
```

返回：

```json
{
  "name": "some_plugin",
  "version": "0.2.0",
  "compatible": true,
  "installed_version": "0.1.0",
  "permission_changes": {
    "added": ["network:example"],
    "removed": []
  },
  "restart_required": true,
  "warnings": []
}
```

错误码建议：

| HTTP 状态 | detail | 场景 |
|---|---|---|
| 400 | `invalid_plugin_name` | 插件名非法 |
| 400 | `invalid_manifest` | manifest 缺字段或格式错误 |
| 403 | `permission_denied` | 非管理员操作 |
| 409 | `system_plugin_cannot_be_disabled` | 禁用/卸载系统插件 |
| 409 | `plugin_has_dependents` | 插件被其他插件依赖 |
| 409 | `plugin_has_enabled_dependents` | 插件被其他 enabled 插件依赖，不能禁用 |
| 409 | `plugin_dependency_not_installed` | 启用插件时 required dependency 未安装 |
| 409 | `plugin_dependency_disabled` | 启用插件时 required dependency 已禁用 |
| 409 | `plugin_restart_required` | 当前已有 pending 变更，需先重启 |
| 422 | `incompatible_plugin_api` | 插件 API 版本不兼容 |
| 503 | `plugin_state_store_unavailable` | 生产环境状态库不可用，写操作被拒绝 |
| 500 | `plugin_install_failed` | 安装过程失败 |

### 软重启

建议新增：

```http
POST /v1/admin/runtime/restart-instructions
```

第一版建议只返回操作指引，不由 API 进程直接杀自己。返回值必须明确不可直接执行：

```json
{
  "actionable": false,
  "restart_required": true,
  "message": "Restart the FastAPI process or container through the deployment system."
}
```

后续如果接入 supervisor/tmux/systemd，再新增可执行的 `POST /v1/admin/runtime/restart`，并定义权限、幂等和超时语义。

#### 重启语义

第一阶段要明确“重启”不是热加载，而是让运行时重新 discover 和注册插件。建议语义：

- `soft restart`：应用内触发当前进程优雅重启插件子系统；第一版可以先不实现。
- `process restart`：重启 FastAPI 进程或容器，是第一版主要路径。
- `restart_required=true`：表示插件状态已经写入，但当前进程注册表还没有完全反映新状态。
- `pending changes`：存在 `restart_required=true` 的插件时，市场页顶部展示全局提示。

重启后的处理：

1. 启动时 discover 所有代码存在的插件。
2. 读取 `plugin_state` 判断 installed/enabled。
3. 对 installed=false 的插件跳过注册；system 插件正常情况下不允许写入 installed=false。
4. 对 enabled=false 的插件执行最小注册或不注册业务能力，具体由 owner 化 registry 决定。
5. 如果插件加载成功，清除对应 `restart_required`。
6. 如果插件加载失败，设置 `status=failed` 和 `last_error`，保留 `restart_required=false`，避免每次启动无限提示。

#### Owner 化注册的边界

禁用插件真正生效依赖 registry 能知道每个 route、hook、tool、command 的 owner。迁移期间要区分两类能力：

- 已 owner 化：禁用后可从注册表移除或过滤。
- 未 owner 化：禁用后需要重启，并且可能只能阻止插件主动逻辑，不能完全移除历史注册物。

前端应展示 `disable_mode`：

```json
{
  "disable_mode": "runtime_filtered",
  "disable_note": "Hooks and commands are owner-aware; routes require process restart."
}
```

可选值：

- `runtime_filtered`：无需进程重启即可禁用。
- `restart_required`：需要重启后完全生效。
- `partial`：部分能力已禁用，部分能力等待 owner 化改造。

## 前端设计

新增或改造插件页为两个视图：

1. 已安装插件
2. 插件市场

已安装插件卡片展示：

- 插件名 / 描述 / 版本
- source：builtin / marketplace / local
- system 标记
- enabled 开关
- status：active / disabled / pending_restart / failed / incompatible
- restart_required 标记
- runtime 摘要
- 配置按钮
- 禁用/启用按钮

第一版保留现有复杂配置页：

- `/amap`
- `/wxbot`
- `/credits`
- `/commands`
- `/memory`
- `/moderation`
- `/persona`
- `/repeater`

市场页先基于统一接口，不再在 `PluginsPage` 里为每个插件硬编码 runtime 请求。

### 前端交互细节

已安装页建议状态文案：

| 状态 | 文案 | 可用操作 |
|---|---|---|
| `active` | 运行中 | 配置、禁用、查看状态 |
| `disabled` | 已禁用 | 启用、配置 |
| `pending_restart` | 等待重启 | 查看变更、重启指引 |
| `failed` | 加载失败 | 查看错误、禁用、重试/重启 |
| `incompatible` | 不兼容 | 查看原因、卸载 |
| `unknown` | 状态未知 | 刷新、查看后端日志 |

市场页安装流程：

1. 点击插件卡片查看详情。
2. 展示描述、版本、兼容性、权限、配置 schema。
3. 点击安装前调用 preview。
4. preview 通过后弹窗确认权限。
5. 安装成功后显示“已安装，重启后生效”。
6. 页面顶部显示 pending restart 横幅。
7. 重启后刷新 runtime 状态。

升级流程：

1. 插件卡片展示当前版本和可升级版本。
2. 点击升级调用 upgrade preview。
3. 如果权限增加或兼容性变化，突出展示差异。
4. 升级成功后显示 pending restart。
5. 重启失败时展示 `last_error` 和回滚状态。

卸载流程：

1. 点击卸载前展示保留业务数据的说明。
2. system 插件禁用卸载按钮并展示原因。
3. 存在依赖方时按钮禁用并展示依赖列表。
4. 卸载成功后从已安装页隐藏，或以 `installed=false` 状态展示在市场页。

前端不应硬编码每个插件的运行状态字段。插件卡片只消费统一字段：

```ts
interface PluginRuntimeSummary {
  name: string;
  display_name: string;
  version: string;
  source: "builtin" | "marketplace" | "local";
  installed: boolean;
  enabled: boolean;
  system: boolean;
  status: "active" | "disabled" | "pending_restart" | "failed" | "incompatible" | "unknown";
  restart_required: boolean;
  disable_mode: "runtime_filtered" | "restart_required" | "partial";
  disable_note: string;
  capabilities: Record<string, string[]>;
  config_schema?: unknown;
  runtime_status?: Record<string, unknown>;
  last_error?: string | null;
}
```

复杂配置继续跳转现有页面；新插件没有专属页面时，使用 schema 自动生成简单配置表单。

## 测试与验收矩阵

### 后端单元测试

- `PluginStateStore`：install、enable、disable、restart_required、status、last_error 持久化。
- `PluginStateStore`：startup reconcile 不重复写事件、不重置用户 `enabled=false`。
- `PluginMarketplaceManifest`：合法 manifest 解析、缺字段报错、非法插件名报错。
- `PluginMarketplaceManifest`：core API 版本兼容判断。
- `PluginRegistry`：owner 化注册后，按 plugin owner 过滤 hook/tool/command。
- `PluginRegistry`：`HookRunner.unregister_owner("draw")` 能移除 draw hooks。
- `PluginRegistry`：`AgentToolRegistry.unregister_owner("amap")` 能移除 amap tools。
- `PluginRegistry`：`CommandRegistryService.unregister_owner("draw")` 能移除 draw commands。
- `PluginRegistry`：重复 register/unregister/register 不产生重复项。
- `PluginAdminRouter`：system 插件不能禁用或卸载，`commands` 禁用返回 409。
- `PluginAdminRouter`：存在依赖时不能禁用或卸载。
- `PluginAdminRouter`：安装 preview 返回权限差异和重启要求。
- `PluginAdminRouter`：安装失败写入 plugin_events。
- `PluginAdminRouter`：禁用带 API router 的插件后，返回 `disable_mode=partial` 或 `restart_required`。
- `PluginAdminRouter`：`restart_required=true` 时再次 install/upgrade/uninstall 返回 409 `plugin_restart_required`。

### 集成测试

- 首次启动后，已发现插件自动写入 `plugin_state`。
- 重复启动 reconcile 不重复写事件、不重置用户 disabled 状态。
- 禁用 `commands` 插件返回 409，命令中心继续可用。
- 通过 admin API 禁用 draw 后，`/draw` 不再可用。
- 通过 admin API 启用 draw 后，`/draw` 恢复。
- 阶段 3 暂不承诺 credits 完整热禁用；禁用 credits 返回 `disable_mode=partial` 或 `restart_required`，等 BillingCoordinator owner 化或 enabled gate 完成后再承诺停止扣减/发放。
- 安装内置 marketplace 插件后，状态为 `pending_restart`、enabled=false、restart_required=true。
- 重启后，新插件进入 active 或 disabled，并清理 restart_required。
- 插件加载失败时进入 failed，并写入 last_error，其他插件仍可启动。
- 升级 preview 能识别新增权限。
- 卸载插件不删除业务数据表。
- entrypoint 插件和 builtin 插件同名时冲突处理稳定，返回明确错误或固定优先级。

### 前端测试

- 已安装页展示 active、disabled、pending_restart、failed，以及 restart_required 标记。
- 市场页展示未安装、已安装、可升级、不兼容状态。
- 安装确认弹窗展示权限和兼容性结果。
- pending restart 横幅在存在任意 restart_required 插件时出现。
- system 插件卸载按钮不可用。
- last_error 能在失败插件卡片中展开查看。

### 手工验收

- 启动服务后打开插件页，现有插件全部可见。
- 禁用非 system 插件，刷新页面后状态保持。
- 重启服务后，禁用状态仍然生效。
- 安装一个内置 marketplace 插件，页面提示需要重启。
- 重启后插件出现在已安装页，runtime 状态正常。
- 模拟 manifest 不兼容，市场页禁止安装并展示原因。

## 实施阶段

### 阶段 1：状态层和只读展示

目标：不改变当前行为，只让已有插件进入状态管理。

任务：

- 新增 `PluginStateStore`。
- 启动时 reconcile discovered plugins。
- 新增 `PluginManager` 聚合 registry + state。
- 新增 `/v1/admin/plugins/installed`。
- 前端展示 installed 插件卡片。
- 所有已有插件默认 `enabled=true`。

验收：

- 当前插件全部显示为 active。
- 微信、高德、绘图、积分、命令等现有功能不受影响。
- 重启后状态持久化。

### 阶段 2：Owner 化注册

目标：为后续禁用插件做清理能力。

任务：

- `HookRunner.register(owner=...)`。
- `HookRunner.unregister_owner()`。
- `AgentToolRegistry.unregister_owner()`。
- `CommandRegistryService.register(owner=...)`。
- `CommandRegistryService.unregister_owner()`。
- 插件注册 hooks/tools/commands 时传 owner。

验收：

- `HookRunner.unregister_owner("draw")` 能移除 draw hooks。
- `AgentToolRegistry.unregister_owner("amap")` 能移除 amap tools。
- `CommandRegistryService.unregister_owner("draw")` 能移除 draw commands。
- 重复 register/unregister/register 不产生重复项。
- 此阶段不要求通过 admin API 禁用插件；admin enable/disable 放到阶段 3。

### 阶段 3：启用/禁用

目标：支持配置级热启停。

任务：

- `POST /v1/admin/plugins/{name}/enable`。
- `POST /v1/admin/plugins/{name}/disable`。
- system plugin 保护。
- 启停事件写入 `plugin_events`。
- 前端启用/禁用开关。

验收：

- `commands` 禁用接口返回 409 `system_plugin_cannot_be_disabled`。
- 通过 admin API 禁用 draw 后，`/draw` 不再可用。
- 通过 admin API 启用 draw 后，`/draw` 恢复。
- 通过 admin API 禁用 amap 后，Agent tool catalog 不再出现 amap tools。
- 禁用包含 API router 或 capability engine 的插件时，返回正确 `disable_mode` 和 `restart_required` 边界说明。

### 阶段 4：Schema 和 Runtime Status

目标：统一插件配置和运行状态展示。

任务：

- `Plugin.get_config_schema()` 默认实现。
- `Plugin.get_runtime_status()` 默认实现。
- 为已有插件补最低限度 runtime status。
- `/v1/admin/plugins/{name}/config-schema`。
- `/v1/admin/plugins/{name}/runtime`。

验收：

- 插件页不再依赖大量硬编码请求也能展示核心状态。
- amap 展示 key/storage/tools。
- draw 展示 configured/storage/commands。
- wxbot 展示 bridge/pending/sessions。

### 阶段 5：市场和软重启

目标：支持安装/升级后提示重启。

任务：

- 本地 marketplace manifest。
- manifest parser、兼容性校验、权限声明校验。
- `/v1/admin/plugins/marketplace`。
- install/upgrade preview 接口。
- 安装/升级/卸载接口先落状态，标记 `restart_required`。
- install/upgrade/uninstall 审计事件。
- 前端市场页、权限确认弹窗、pending restart 横幅。
- `/v1/admin/runtime/restart-instructions` 重启指引接口。

验收：

- 市场能显示可安装插件。
- manifest 不合法时后端返回 `invalid_manifest`。
- 不兼容插件不能安装。
- 安装新插件前能展示权限和重启要求。
- 安装新插件后显示 `restart_required=true`。
- 进程重启后新插件进入 active 或 disabled，并清理 `restart_required`。

## 后续真热加载方向

如果后期必须支持新 Python 插件代码无重启加载，需要进一步改造：

- 动态插件网关替代 `FastAPI.include_router()`。
- 插件 route 由 PluginManager active map 转发。
- 完整 unregister owner 能力。
- 后台任务、HTTP client、DB resource 生命周期清理。
- `importlib.invalidate_caches()` 和 `sys.modules` 清理。
- 新版本加载失败回滚。

更推荐第三方插件采用外部协议：

- HTTP plugin
- MCP plugin
- subprocess plugin
- container plugin

主系统不直接 import 不可信第三方代码，而是通过协议调用，权限和稳定性更可控。

## 预估实施节奏

在当前代码基础上，先做阶段 1 到阶段 3 是比较快的，因为已有插件框架和 summary 接口已经存在。

建议拆成三批提交：

1. 状态层 + installed API + 前端只读展示。
2. owner 化注册 + draw/amap 验证禁用恢复。
3. schema/runtime status + 市场页基础版 + restart_required。

如果不做真热加载，只做上述“状态管理 + 配置级热启停 + 安装后软重启”，改造风险可控，后续推进会比较快。
