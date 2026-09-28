# Codex State Kit 新手指南 — codex降智交流群知识库

## 一句话概述

Codex State Kit 是本机桌面助手（Tauri / Rust / React）。它在本机代理 Codex 的上游请求，管理多个 ChatGPT 账号、出站线路和虚拟设备，记录用量与费用，并按官方客户端的信号标记被降智的请求。仓库：https://github.com/DouDOU-start/codex-state-kit

效果受账号、网络和上游服务影响，不保证消除过载或提升模型能力。

**292 / 312 打票已经失效。** 不要再按票据长度判断质量，也不要再教人「打 292」「等 312」。现在的 Kit 不再采集或注入 Turn-State 票据。

---

## 现在怎么判断降智

判断方式和官方 Codex 客户端一致，只看上游自己给出的信号。在「使用记录」里点标记可看依据。

| 标记 | 含义 |
| --- | --- |
| **已降级** | 响应头里的 `openai-model` / `x-openai-model` 与请求模型不一致，官方会提示请求被改到备用模型 |
| **疑似降智** | 触发安全缓冲、账号被建议验证，或只有响应体里的模型名不一致 |

`x-codex-turn-state` 长度等旧指标只作参考记录，**不参与判断**。有人再问 292 / 312，直接说明这套打票已经没了，改看使用记录里的降智标记。

---

## 动态代理（出站线路）

出站线路现在走的是 **Codex 业务请求、登录和价格同步**，不是打票探针。每个账号绑定自己的线路，切换账号时一起换。

### 推荐：Cliproxy 住宅动态流量

官网：https://cliproxy.com

**入门档：**
- 选 **Residential Dynamic Traffic**（住宅动态流量）
- 最低档常见为 **2GB / $2.40**（以官网实时价为准）
- 支持支付宝
- 流量会随 Codex 实际请求消耗，不再是「只打几条探针」

**购买步骤：**
1. 打开 https://cliproxy.com/pricing/residential-proxies/
2. 注册并付款
3. 后台用用户名密码认证，拿到代理 URL

**地区：**
- 常见做法是选全球（Global），或按服务商支持的会话出口填写
- 需要会话轮换时，可把 session 段写成 `{session}`，由 Kit 生成，例如：
  `socks5://user-region-DE-sid-{session}-t-120:password@host:port`
- 不要再按「美国打不出 292」来选地区

**地址格式：**
- `socks5h://用户名:密码@主机:端口`
- `http://用户名:密码@主机:端口`
- 特殊字符要 URL 编码

其他动态住宅代理也可以，关键是出口能轮换、账号之间尽量分开。

---

## 安装和配置

### 1. 安装 State Kit

1. 从 https://github.com/DouDOU-start/codex-state-kit/releases 下载最新正式版
2. 支持 Windows（MSI / EXE）和 macOS
3. 同一时间只能跑一个 Kit

### 2. 确认 Codex 工作目录

「Codex 工作目录」必须和 Codex 客户端一致，一般是用户目录下的 `.codex`（Windows：`%USERPROFILE%\.codex`）。

### 3. 添加 ChatGPT 账号

在「Codex 接入」点「添加账号」：

- 浏览器回调（默认）
- 授权码
- Refresh Token
- Access Token（不能自动刷新，过期要重新导入）

登录请求走当前出站线路。新账号会分配独立虚拟设备，并沿用当前线路。

### 4. 配置出站网络

在「出站网络」里二选一：

**手动代理：** 填完整 URL，失焦后保存。只开了 Clash 系统代理、没开 TUN 时，打开「经系统代理连接手动代理」。

**订阅节点：** 填 Clash / Mihomo 订阅 URL、本地文件或分享链接，用内置内核选节点。

线路不可用时业务请求返回 502，不会自动改直连。

### 5. 接入后重启 Codex

界面显示「已接入」后，重启 Codex 客户端加载本机路由。使用期间保持 Kit 运行。正常退出后再重启 Codex，会恢复退出前的官方账号和本地配置。

---

## 主要功能（给新手看的）

- **多账号切换**：列表或托盘一键切换，立刻生效，不用重启 Codex。每个账号绑定自己的虚拟设备和出站线路。
- **使用记录**：记录请求模型、实际上游模型、耗时、Tokens 和费用。可筛选「只看降智请求」。
- **降智识别**：按上面的官方信号标记「已降级」或「疑似降智」，新标记会弹提醒。
- **强制绑定模型**：可指定上游模型 ID，下游无论请求什么都改成该值再转发。
- **虚拟设备**：转发时替换客户端身份，账号之间不混用。

---

## 网络路径

```text
Codex 客户端 ── 本机代理（Kit，正式版默认 127.0.0.1:8787） ── 出站线路（手动代理 / 订阅节点） ── Codex 上游
```

业务请求和官方客户端一样走上游 WebSocket；握手失败时回退 HTTP SSE。没有单独的「打票线路」。

---

## 常见问题 FAQ

### Q: 292 / 312 / 打票还能用吗？
A: 不能。票据采集已经失效。不要再等 292，也不要按 312 判断降智。打开 Kit 的使用记录，看「已降级」或「疑似降智」。

### Q: 现在怎么知道降智了？
A: 看使用记录。`openai-model` 和请求模型不一致是「已降级」；安全缓冲或账号验证建议是「疑似降智」。点标记看依据。

### Q: 还要买动态代理吗？
A: 需要稳定出口时仍然要配出站线路。代理现在转发的是 Codex 业务流量，不是打票。Cliproxy 住宅动态 2GB 档仍可当入门，流量按实际使用消耗。

### Q: 代理地区怎么选？
A: 按服务商支持选全球或指定地区。需要会话轮换时用 `{session}`。不要再按「锁美国打不出票」来选。

### Q: WARP 还能当出站吗？
A: 可以当一条出站线路试，但不保证质量。连不上就换手动代理或订阅节点。

### Q: HTTP 401 / 403 / 过载怎么处理？
- **401**：检查账号授权，必要时重新授权
- **403**：检查模型和网络出口
- **server_is_overloaded / server_error**：稍后重试

### Q: 退出 Kit 后 Codex 连不上？
A: 正常退出会恢复路由。异常退出时，先启动 Kit 再正常退出。仍不行就备份 `config.toml`，对照路由恢复记录还原，不要删其他提供方配置。

### Q: 已接入但没有请求记录？
A: 确认 Codex 和工作目录一致，并重启客户端。自定义模型提供方的独立 `base_url` 可能绕过 Kit。使用记录可按账号筛选，切到「全部账号」再看。

### Q: 代理地址怎么填？
A:
- `socks5h://user:pass@host:port`
- `http://user:pass@host:port`
- 特殊字符 URL 编码
- 会话出口可用 `{session}`

### Q: 只有 API Key 能自动接入吗？
A: 不能。当前自动接入要求 ChatGPT 登录。

---

## 相关链接

- Codex State Kit 仓库：https://github.com/DouDOU-start/codex-state-kit
- 出站代理：https://github.com/DouDOU-start/codex-state-kit/blob/master/docs/outbound.md
- 使用记录与降智识别：https://github.com/DouDOU-start/codex-state-kit/blob/master/docs/usage-records.md
- 常见问题：https://github.com/DouDOU-start/codex-state-kit/blob/master/docs/troubleshooting.md
- Cliproxy：https://cliproxy.com
- Cliproxy 住宅代理定价：https://cliproxy.com/pricing/residential-proxies/
