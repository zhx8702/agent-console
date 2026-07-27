# Agent Console Frontend

独立前端管理台，基于 React + TypeScript + Vite。

## 启动

```bash
cd frontend
npm install
npm run dev
```

默认开发地址是 `http://127.0.0.1:5173`。浏览器始终通过当前站点的
`/api/*` 前缀访问后端，Vite 在开发环境将该前缀重写并代理到
`http://127.0.0.1:8000`。因此 `/plugins` 和 `/plugins/marketplace`
保留为前端页面路由，可以直接打开和刷新。

## 环境变量

复制 `.env.example` 为 `.env` 后按需覆盖：

```bash
cp .env.example .env
```

主要变量：

- `VITE_DEV_API_TARGET`（仅本地开发代理目标）

租户来自后端 `/v1/admin/auth/me` 的已签名身份响应，群聊只能从后端 roster
选择，不接受构建参数或浏览器存储覆盖。管理员令牌仅保存在当前 React 内存状态中，
不会写入 localStorage；刷新后的登录状态由后端 HttpOnly 会话 Cookie 恢复。
