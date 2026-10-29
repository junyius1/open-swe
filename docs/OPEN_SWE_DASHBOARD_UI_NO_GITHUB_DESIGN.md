# Open SWE Dashboard UI 程序设计书(无 GitHub 变体)

> **文档定位**:本文是给 **AI 编程 agent** 实现的 dashboard UI(`ui/` 目录,前端应用层)程序设计书。
> 它描述:把现网 dashboard UI 中 **GitHub 登录 / PR / 审查 / GitHub 授权** 全部拿掉后,
> 保留 **本地 git 分支提交** 交付形态、并与两份已定稿后端设计书完全对齐的最终形态。
>
> **前置依赖文档(必须先读,本文不重复其内容)**:
> 1. `docs/OPEN_SWE_DASHBOARD_SERVER_NO_GITHUB_DESIGN.md` —— **后端(dashboard 服务器)设计书**,
>    本文引用的所有后端端点契约(路径、状态码、body 形状、cookie 属性)都以它为准。
> 2. `docs/OPEN_SWE_SERVER_2024_DESIGN_NO_GITHUB.md` —— **2024 运行时(服务器)设计书**,
>    本文引用的运行时不变量(有界 state/history 代理、SSE、LLM gate、Docker 沙箱)都以它为准。
>
> **交付形态(本文的北极星)**:
> - 登录 = **本地 token 登录页**(共享 token + username),**无 GitHub OAuth、无任何 OAuth 跳转**。
> - 代码交付 = agent 把改动 **commit 到沙箱内本地 git 分支 `local/<slug>`**;
>   **不创建 PR、不推送远端(可选 push 由宿主凭据在容器外完成,与 UI 无关)、不做 GitHub 授权**。
> - 沙箱 = **Docker**(后端已定);UI 不再有任何"云沙箱 / 云终端 / 快照构建"概念。
> - 多用户:登录按 `username` 隔离会话与线程;admin 由后端 `CONFIGURED_ADMINS` 决定,UI 只读 `is_admin`。
>
> **范围声明**:本文只改 `ui/` 下的代码(含 `ui/server/` 的 Nitro 服务端代码)。
> `agent/` 后端的改动属于后端设计书的范围,本文只在"契约引用"处提及,不重新设计。

---

## 目录

1. [目标与范围](#1-目标与范围)
2. [现状分析:ui/ 代码结构与 GitHub 耦合点清单](#2-现状分析)
3. [目标架构总览](#3-目标架构总览)
4. [Nitro 服务器端逻辑(ui/server + vite.config.ts)](#4-nitro-服务器端逻辑)
5. [路由表:删 / 改 / 留](#5-路由表删--改--留)
6. [认证与会话模型(登录页 / SSR / 多用户)](#6-认证与会话模型)
7. [API 客户端改造(`ui/src/lib`)](#7-api-客户端改造)
8. [Agents 功能层(线程 / 发消息 / 流 / 运行状态)](#8-agents-功能层)
9. [Git 面板与本地分支 UI](#9-git-面板与本地分支-ui)
10. [设置页 / 侧边栏 / 杂项](#10-设置页--侧边栏--杂项)
11. [环境变量表](#11-环境变量表)
12. [文件级 留 / 改 / 删 索引](#12-文件级-留--改--删-索引)
13. [实现顺序(AI 执行步骤)](#13-实现顺序ai-执行步骤)
14. [验收清单](#14-验收清单)
15. [与后端设计书的契约对齐表](#15-与后端设计书的契约对齐表)

---

## 1. 目标与范围

### 1.1 技术栈(不变)

| 层 | 技术 | 说明 |
|---|---|---|
| 构建/打包 | Vite 8 + `@tanstack/react-start/plugin/vite` | 不变 |
| 服务端 | Nitro(`nitro/vite` 插件,Node runtime) | 部署形态:SSR + 后端反向代理 |
| 路由 | TanStack Start 文件路由(`ui/src/routes/`) | 路径不变,裁剪条目 |
| 状态/数据 | TanStack Query(v5,`queryOptions` + hooks) | 不变 |
| Agent 流 | `@langchain/react`(`StreamProvider`)+ `@langchain/langgraph-sdk`(`Client`) | **核心交互不变**:v2 事件流 + `run.start` 命令 |
| UI | React 19 + React Compiler(`viteReact({ compiler: true })`)+ Tailwind CSS v4 + shadcn-style `ui/` 组件 | 不变 |
| 语言/测试 | TypeScript;Vitest | 不变;`ui/` 必须用 **pnpm**(见 `ui/AGENTS.md`,禁用 npm/bun) |

**不变的原因**:UI 与运行时的核心契约(`POST /threads/{id}/commands` 的 `run.start`、
`POST /threads/{id}/stream/events` v2 SSE、`POST /threads/{id}/messages` 入队)在后端设计书中
被原样保留。UI 的"发消息-收流-看结果"主链路**一行协议都不改**,改的只是:登录、
交付展示、以及所有 GitHub 周边。

### 1.2 范围内(本文要做的)

1. **Nitro 服务端**:生产代理 handler 去 OAuth;dev 代理前缀裁剪。
2. **登录**:GitHub OAuth 跳转 → 本地 token 表单(§6.3)。
3. **路由裁剪**:删除 PR/审查/使用量/集成/云/evals 页面(§5)。
4. **API 客户端**:`ui/src/lib/api.ts` 与 `ui/src/features/agents/lib/api.ts` 去 GitHub 化(§7/§8)。
5. **Agents 功能层**:发送流、运行状态、流 provider、git 面板、计划审批,去 PR 化(§8/§9)。
6. **设置页/侧边栏**:删 PR 偏好与 GitHub 连接,admin 页改为本地用户面(§10)。
7. **类型与常量清理**:所有 `github_*` / `pr_*` / `Installation` / `Review*` 类型(§12)。

### 1.3 范围外(明确不做)

- 不修改 `agent/` 任何文件(后端设计书负责)。
- 不修改 TanStack Start / React / SDK 的用法模式。
- **桌面(Electron)客户端**:UI 代码里的 desktop-local 分支(`window.openSweDesktop`、
  `open-swe:` 协议、`/agents/local/*` 路由)**原样保留**——桌面壳独立分发,其本地模式
  天然无 GitHub。本文不设计桌面侧改动,只保证 web 代码删除 GitHub 逻辑后不破坏 desktop 编译。
- 不做新的品牌/视觉改版;沿用现有 `AppShell`/卡片/配色体系。

### 1.4 硬性不变量(违反即失败)

- **pnpm only**(`ui/AGENTS.md`):任何安装/脚本走 `pnpm`;不产生 npm/bun lockfile。
- **不引入新依赖**。本文所有改动用现有依赖完成;若实现中认为需要新包,停下并在 PR 描述中单列理由
  (仓库规则:新依赖必须人审)。
- **保留 SSR 登录墙语义**:未登录访问非公开路径,服务端在首字节前重定向到 `/login?redirect=<path>`;
  已登录访问 `/login` 跳回目标路径(§6.4 给出精确规则)。
- **保留 `toAbsoluteApiUrl` 行为**:`@langchain/langgraph-sdk` 的 `Client` 用
  `new URL(apiUrl + path)` 拼 URL,相对 base 会回退到 `http://localhost:8123`;
  同 origin base 必须提升为绝对 URL(现 `AgentThreadStreamProvider.tsx` 已有此逻辑,保留)。
- **不改变 SSE/命令协议的客户端超时假设**:流空闲超时 60s、最大存活 14400s 是后端值
  (env `OPEN_SWE_STREAM_IDLE_TIMEOUT_SECONDS` / `OPEN_SWE_STREAM_MAX_LIFETIME_SECONDS`),
  UI 不自设更短的断流阈值,避免误杀长模型调用。
- **commit 规范**:每个 commit 追加 trailer `Co-authored-by: open-swe[bot] <open-swe@users.noreply.github.com>`。

---

## 2. 现状分析

### 2.1 代码结构(现状)

```
ui/
├── vite.config.ts                  # dev 代理 + 生产 Nitro routeRules + shiki 预打包 + shell 预渲染
├── server/
│   └── backend-proxy.ts            # 生产运行时反向代理 handler(h3 proxyRequest)
└── src/
    ├── routes/                     # TanStack Start 文件路由
    │   ├── __root.tsx              # QueryClient + SSR 会话解析 + 主题
    │   ├── login.tsx  index.tsx  my-settings.tsx
    │   ├── agents.tsx              # /agents 布局(Sidebar + StreamProvider + RequireLogin)
    │   ├── agents/                 # index / threads / $threadId / $threadId_.plan
    │   │   ├── skills.tsx  local/$sessionId.tsx  automations/{index,new,$scheduleId}.tsx
    │   │   └── reviews/            # ← 删
    │   ├── agents_/{environments,instructions,snapshots}.tsx
    │   ├── admin.tsx  admin_.evals.tsx
    │   ├── review.tsx  review_.{styles,repositories.$owner}.tsx
    │   ├── $owner.$repo.pull.$number.tsx      # PR 聊天页 ← 删
    │   ├── usage.tsx  integrations.tsx  cloud-agents.tsx   # ← 删
    ├── features/
    │   ├── agents/                 # 主功能:组件 + lib(api/queries/stream/diff/terminal/…)
    │   ├── automations/            # 定时任务(留)
    │   ├── reviews/                # ← 整删
    │   └── settings/components/    # Account/Preferences/PullRequests/Environments/Connections/PersonalInstructions
    ├── components/                 # AppShell / AppSidebar / RepoSnapshotsPanel / ReviewStylesPanel / ui/*
    └── lib/                        # api.ts(976L 中央客户端)/ session / session-ssr / auth-redirect* /
                                    # dashboard-fetch / api-base / plan / repo / repoCache /
                                    # desktop-local-mode / notifications / datadog / hydration / query
```

**运行时形态**(与后端设计书 §"两进程拓扑"一致):
- `:2024` = LangGraph 运行时(`langgraph dev`,FastAPI 同 app,`/dashboard/api/*` 由 dashboard 路由组提供)。
- `:8001` = 本 UI 的 Nitro 部署端口(SSR + 代理)。UI **只把 `/dashboard/api/**` 代理到**
  `DASHBOARD_API_URL`(缺省 `http://localhost:2024`),浏览器与 UI 之间**同 origin**,
  `osw_session` cookie 因此是同 origin cookie,SSR 也能读到(§6.4)。

### 2.2 GitHub 耦合点清单(现状,逐文件)

| 位置 | 耦合内容 | 处理 |
|---|---|---|
| `src/routes/login.tsx` | "Continue with GitHub" → `loginUrl()` 跳后端 OAuth | **改**:本地 token 表单(§6.3) |
| `src/lib/api.ts:22-48` | `reviewImageProxyUrl()`(GitHub 图片代理)+ `GITHUB_IMAGE_HOST_RE` | 删 |
| `src/lib/api.ts:60-64` | `isGithubReauthError()` | 删(401 语义统一为"会话失效") |
| `src/lib/api.ts` 类型 | `Installation/ReposPayload/Repository`(GitHub App 安装)、`UserMapping`、全部 `Review*`/`ReviewerEval*`、`UsageLeaderboard*`、`PRTraceResolutionResult` | 删(§7.2) |
| `src/lib/api.ts` `api` 对象 | `repos`、`listReviewStyles…`、`listReviews…`、`usageLeaderboard`、`myMapping`、`adminListUserMappings…`、`listAutoReviewRepos…`、`getReviewerEval` | 删(§7.2) |
| `src/lib/api.ts` 尾 | `loginUrl()`(OAuth 跳转)、`slackConnectUrl()`、`notionConnectUrl()` | 删 |
| `src/lib/api.ts` `Profile/ProfileUpdate` | `default_repo/base_branch/branch_prefix/auto_fix_ci/draft_prs/review_draft_prs` | 裁剪(§7.3) |
| `src/lib/api.ts` `SessionUser` | `slack_oauth_enabled?` | 删字段 |
| `src/lib/repo.ts` / `repoCache.ts` | GitHub 仓库 URL 归一化 + `/repos` 的 localStorage 缓存 | 删 |
| `server/backend-proxy.ts` | `redirect: "manual"`(为 OAuth 3xx 保留地址栏) | 简化(§4.2) |
| `vite.config.ts` `BACKEND_PREFIXES` | `/webhooks /mock /control /fake-gh /fake-slack /static /ok` | 裁剪(§4.3) |
| `src/lib/datadog.ts` | Datadog RUM | 删(后端无此面) |
| `src/routes/admin.tsx` | GitHub 用户映射管理 | **改**:本地用户列表 + 跨线程取消(§10.4) |
| `src/routes/{admin_.evals,review*,usage,integrations,cloud-agents,$owner.$repo.pull.$number}.tsx` | evals/审查/使用量/集成/云/PR 页 | 整删(§5) |
| `src/features/reviews/**` | 审查功能 | 整删 |
| `src/features/agents/lib/api.ts` | `getThreadPullRequestStatus`、`connectCloudTerminal`、`downloadThreadRecoveryPatch`、`ThreadPrDiffFile/ThreadBranchDiff` 的 PR 命名 | 裁剪(§8.6) |
| `src/features/agents/lib/queries.ts` | `pullRequestStatus`/`workflowApprovals` query keys | 裁剪(§8.6) |
| `src/features/agents/components/*` | PR 状态轮询、"Fix CI/PR" 按钮、云终端、PR 图片代理 | 裁剪(§8/§9) |
| `src/routes/my-settings.tsx` | `PullRequestsSection`、`ConnectionsSection`(Slack/Notion/Currents/Datadog) | 删两节(§10.3) |
| `src/components/AppSidebar.tsx` | `/usage /cloud-agents /review /admin` 导航项 | 裁剪(§10.5) |
| `src/features/agents/components/AgentThreadView.tsx` | `fixPullRequest`、PR 面板 | 裁剪(§9) |

### 2.3 与后端已定决策的耦合核对(现状 → 目标)

| 后端决策(已推送设计书) | UI 现状 | UI 目标 |
|---|---|---|
| 登录 = `POST /auth/login {username, token}` → `osw_session` cookie | 跳 `/auth/login?redirect_to=…` OAuth | 表单直 POST 同路径(§6.3) |
| 无 `/my-mapping` `/my-credentials/*` `/notion/*` `/slack/*` `/team-settings*` `/team-credentials*` `/repos` | 有对应客户端方法 | 全删 |
| 无 `/reviews/*` `/review-styles/*` `/enabled-review-repos` `/repo-snapshots*` `/sandbox-settings` `/environments*` `/voice/*` `/agent-usage-leaderboard` | 有对应客户端方法/页面 | 全删 |
| 无 `/threads/{id}/{pull-request-status, terminal/connect, recovery.patch, pr-diff}` | git 面板调用 PR status | 删;git 面板改读 `branch-diff`+`working-tree-diff`(§9) |
| 保留 `/threads/{id}/{working-tree-diff, branch-diff}` | `AgentsSidebar`/`ChangesPanel` 已在用 | 保留,PR 语义改本地分支(§9) |
| 保留 plan 7 端点 + workflow-approval 3 端点 + schedules 5 端点 + skills/org-skills/agent-instructions + `/me/instructions` | 已有完整 UI | **不动**(仅 `agent-instructions` 的仓库选择器来源从 `/repos` 改为本地输入,§10.6) |
| 保留 `POST /threads/{id}/messages`(busy 入队,409 引导 `run.start`) | `useSubmitAgentMessage` 双路发送 | **协议不动**(§8.4) |
| 保留 `POST /threads/{id}/commands`(`run.start` 富化 + lazy 建线程,409=busy) | SDK `StreamProvider` | **不动**(§8.5) |
| 保留 SSE `POST /threads/{id}/stream/events` + `GET /threads/{id}/stream` | SDK 流 + join | **不动**(§8.5) |
| LLM 并发 gate(`OPEN_SWE_LLM_MAX_CONCURRENCY`,默认 1 串行;`GET /debug/llm-gate` 观测) | 无 UI | 可选只读观测页(§10.7,默认不做) |
| Docker 沙箱(无云) | 云终端/快照 UI | 删云终端与快照构建 UI(§9/§12) |
| 多用户:`local_user` 隔离,404 归一化,admin `all=true` | 无多用户概念 | 会话/线程按 cookie 用户;admin 页(§6.5/§10.4) |

---

## 3. 目标架构总览

```
浏览器
  │  同 origin(部署形态:8001)
  ▼
Nitro(SSR + 静态 + /dashboard/api/** 反向代理)
  │  fetch(DASHBOARD_API_URL + path + search),cookie 原样透传
  ▼
FastAPI dashboard(:2024,同 app 也服务 :8001 代理之外的直连)
  ├── /dashboard/api/*      线程 CRUD / messages / state·history 有界代理 / SSE /
  │                         plan / workflow-approval / schedules / skills / agent-instructions /
  │                         auth(login·register·logout·me·users) / me·instructions / profile / options
  └── 运行时内部(langgraph 协议面)
       ├── 复合 bearer:<OPEN_SWE_LOCAL_AUTH_TOKEN> + X-OpenSWE-User:<username>(dashboard 侧注入,UI 无感)
       ├── LLMGateMiddleware(默认串行;UI 无感)
       └── Docker 沙箱(容器 oswe-sbx-<thread_id 前缀>;UI 无感)
```

**UI 的两个"面"**(实现时时刻区分,二者 URL 构造方式不同):
1. **REST 面**(TanStack Query/mutation 调用):`dashboardApiUrl(path)` =
   `<dashboardApiBase()> + "/dashboard/api" + path`;浏览器侧 base 为相对(同 origin),
   SSR 侧为 `DASHBOARD_API_URL` 绝对值 + 手动转发 cookie(`ui/src/lib/dashboard-fetch.ts`,保留)。
2. **SDK 流面**(`@langchain/langgraph-sdk`):`new Client({ apiUrl: toAbsoluteApiUrl(<base>+"/dashboard/api") })`
   —— SDK 把 `apiUrl + /threads/{id}/commands`、`.../stream/events` 直接发给后端(经 UI 代理),
   所有请求强制 `credentials: "include"`(经 `onRequest` + `overrideFetchImplementation`,保留)。

---

## 4. Nitro 服务器端逻辑

### 4.1 原则

- 部署形态 = **单镜像前端 Nitro**:容器里只有 Node;`DASHBOARD_API_URL` 指向后端的 pod 地址
  (本地开发缺省 `http://localhost:2024`)。**"代理哪个后端"是部署属性,按请求读取,不构建时固化。**
- UI 只代理一个前缀:`/dashboard/api/**`。后端其余面(运行时协议面、`/ok`)由后端进程自己服务。

### 4.2 `ui/server/backend-proxy.ts`(改)

现状为 OAuth 保留了 `redirect: "manual"`(防止代理吃掉 3xx 跳转,让浏览器地址栏完成 OAuth 环)。
无 GitHub 变体里 `/dashboard/api/*` 不再有 3xx 认证环(login 是同页表单 POST,200/401 都留在本页),
简化如下:

```ts
import { proxyRequest } from "h3"

// 按请求读取:代理哪个后端是部署属性(pod 环境变量),不是构建属性。
function backendOrigin(): string {
  const configured = (process.env.DASHBOARD_API_URL ?? "").replace(/\/$/, "")
  if (!configured) {
    throw new Error(
      "DASHBOARD_API_URL is not set. It is the backend this dashboard fronts; " +
        "there is no default because a fallback would be production's backend."
    )
  }
  return configured
}

export default async function backendProxy(
  event: Parameters<typeof proxyRequest>[0]
) {
  const url = new URL(event.req.url)
  // 透传 method/body/headers(含 cookie);无认证 3xx 环,默认 follow 即可。
  return proxyRequest(event, `${backendOrigin()}${url.pathname}${url.search}`)
}
```

要点:
- **保持"未设置 `DASHBOARD_API_URL` 直接抛错"的 fail-closed 语义**(注释原样保留)。
- 删除 `fetchOptions: { redirect: "manual" }` 及其 OAuth 注释;无其他行为差异。
- h3 的 `proxyRequest` 已转发请求头(含 `cookie`)与 body,**多用户 cookie 隔离在此自动成立**
  (每个浏览器各自 cookie,代理不重写身份头——身份头注入是 dashboard 进程内部对运行时做的事,UI 代理不涉及)。

### 4.3 `ui/vite.config.ts`(改)

1. **生产 routeRules**:`handlers` 只保留 `/dashboard/api`:

```ts
handlers: IS_PRODUCTION
  ? ["/dashboard/api"].map((prefix) => ({
      route: `${prefix}/**`,
      handler: "./server/backend-proxy.ts",
    }))
  : [],
```
   (现状是 `["/dashboard/api", "/webhooks"]`;`/webhooks` 后端在本变体只保留 run-complete 内部面,
   UI 不需要代理,删。)

2. **dev 代理前缀**:`BACKEND_PREFIXES` 裁剪为:

```ts
const BACKEND_PREFIXES = ["/dashboard/api", "/ok"]
```
   删除 `/webhooks /mock /control /fake-gh /fake-slack /static`
   (`/fake-gh`、`/fake-slack` 是 mock harness 的假 GitHub/Slack;`/static` 代理会遮蔽 nitro 静态资源——
   原注释已说明;`/mock`、`/control` 属 harness 控制面)。
   同步更新文件头注释:部署只服务 `/dashboard/api`;`/ok` 为健康检查。

3. **`mockHarnessProxy()` 保留**但仅当 `E2E_HARNESS` 设置时生效(其前缀列表复用裁剪后的
   `BACKEND_PREFIXES`;Yjs WebSocket 代理代码不动——plan 协同仍可能用 harness)。

4. **不变**:`optimizeDeps.include`(streamdown/shiki/@pierre/*)、`SHIKI_LANGS` 预打包、
   `nitro({ inlineDynamicImports: true })`(LangGraph SDK 的 CJS chunk 环,§2.1 注释保留)、
   `SHELL_PAGE` 预渲染(Electron/离线壳)、`legacy.skipWebSocketTokenCheck`、React Compiler 插件。

5. dev 代理目标缺省值保持 `http://localhost:2024`(后端设计书:2024 是运行时端口)。

### 4.4 SSR 会话解析(`ui/src/lib/session-ssr.ts`,保留 + 微调)

保留 `resolveSessionOnServer` 的三段语义,仅按新认证面校准注释与正则:

1. `TSS_PRERENDERING === "true"` → 直接返回(预渲染无 cookie,401 不得变重定向,否则构建没有壳)。
2. **跨 origin API base**(`isCrossOriginApiBase(VITE_DASHBOARD_API_BASE_URL, 请求 origin)`)→ 返回,
   认证交给客户端。原因不变:`osw_session` 种在 API origin 上,跨 origin 的 SSR 请求带不了它。
   (本地/同 origin 部署:base 为空字符串或同 origin,不走此分支。)
3. 否则 `queryClient.ensureQueryData(sessionQueryOptions)`;`user !== null`(含**加载失败但非 401**)
   或 `isPublicPath(href)` → 放行;否则 `throw redirect({ to: "/login", search: { redirect: sanitizeAuthRedirect(href) } })`。

`PUBLIC_PATH_RE` 维持 `/^\/(?:login|dashboard\/api|_serverFn)(?:[/?#]|$)/`。
`__root.tsx` 的 `beforeLoad` 调用点不动。

---

## 5. 路由表:删 / 改 / 留

### 5.1 删除(整文件移除,并清路由树/导航/测试)

| 路由文件 | 原因 |
|---|---|
| `routes/$owner.$repo.pull.$number.tsx` | PR 聊天页(后端 `/reviews/*` 已删) |
| `routes/review.tsx`、`routes/review_.styles.tsx`、`routes/review_.repositories.$owner.tsx` | 审查中心/风格/仓库 |
| `routes/admin_.evals.tsx` | reviewer evals(后端 `/admin/evals` 已删) |
| `routes/usage.tsx` | 使用量排行(后端 `/agent-usage-leaderboard` 已删) |
| `routes/integrations.tsx` | Slack/Linear 集成(无 webhook 面) |
| `routes/cloud-agents.tsx` | 云 agent 设置(无云面) |
| `routes/agents/reviews/` 目录 | agents 子树内的审查入口 |
| `routes/agents_.snapshots.tsx` | 仓库快照构建页(后端 `/repo-snapshots*` 已删) |
| `routes/agents_.environments.tsx` | 环境选择页(后端 `/environments/*` 已删;沙箱镜像是部署级 `DOCKER_SANDBOX_*` 配置) |
| `features/reviews/` 整目录 | 审查功能组件 |
| `components/RepoSnapshotsPanel.tsx`、`components/ReviewStylesPanel.tsx` | 快照构建/审查风格面板(后端端点已删) |

删除后必须同步:`AppSidebar` 导航(§10.5)、`AppCommandPalette` 条目、`AppShortcutReference`
快捷键说明、以及所有 `to="/review…"`、`to="/usage"`、`to="/cloud-agents"`、`to="/integrations"`
的 `Link`。TanStack 文件路由是静态树,**删文件即删路由**,无注册表。

### 5.2 修改

| 路由文件 | 改动 |
|---|---|
| `routes/login.tsx` | GitHub OAuth 按钮 → **本地 token 登录表单**(§6.3 完整规格) |
| `routes/admin.tsx` | GitHub 用户映射管理 → **本地用户页**:`GET /users` 列表 + 跨线程 `admin-cancel`(§10.4) |
| `routes/my-settings.tsx` | 删 `PullRequestsSection`、`ConnectionsSection`;保留 Account/Preferences/PersonalInstructions;`EnvironmentsSection` 降级只读(§10.1) |
| `routes/agents.tsx` | `activeThreadId` 派生逻辑中的排除名单同步(删 `"reviews"`;保留 `local/threads/automations/skills` 排除) |
| `routes/agents_.instructions.tsx` | per-repo 指令保留;仓库选择器由 `/repos` picker 改本地文本输入(§10.6) |

### 5.3 保留(不动或仅微调)

| 路由 | 说明 |
|---|---|
| `__root.tsx` | QueryClient、SSR 会话解析、主题初始化、API warmup——不动 |
| `index.tsx` | `/` → `Navigate /agents`——不动 |
| `agents/index.tsx`(`AgentsHome`)、`agents/threads.tsx`(线程列表/过滤) | 保留;`source` 过滤器枚举里去掉非 dashboard 触发源(§8.3) |
| `agents/$threadId.tsx`、`agents/$threadId_.plan.tsx` | 线程详情/计划审批页——核心,不动(组件层裁剪见 §8/§9) |
| `agents/automations/{index,new,$scheduleId}.tsx` + `features/automations/` | 定时任务保留;表单字段去 `slack_channel_id`/`slack_notification_mode`(§8.7) |
| `agents/skills.tsx` | 技能管理保留(后端 `/skills`、`/organization-skills` 保留) |
| `agents/local/$sessionId.tsx` | 桌面本地路由保留(desktop-only;web 端 `Navigate /agents` 的守卫不动) |
| `my-settings.tsx` | 见 5.2 |

---

## 6. 认证与会话模型

### 6.1 后端契约(引用,不重复设计)

- `POST /dashboard/api/auth/login` body `{"username","token"}`:
  - `token` 与 `OPEN_SWE_LOCAL_AUTH_TOKEN` 做 `hmac.compare_digest`;不等 → **401** `{"detail":"invalid token"}`。
  - 注册表开启(`OPEN_SWE_USER_REGISTRY=1`,默认)且用户不存在 → **401** `{"detail":"unknown user"}`。
  - 成功 → **200** `{"sub","name","is_admin"}` + `Set-Cookie: osw_session`(HttpOnly、`SameSite=Lax`
    (http)/`SameSite=None; Secure`(https)、`Path=/`、7 天、HS256 `DASHBOARD_JWT_SECRET` 签名、`sub`=username)。
- `POST /dashboard/api/auth/register` body `{"username","token"}`:仅 admin 或首用户可建;
  成功 **201**,重名 **409**,token 错 **401**。
- `POST /dashboard/api/auth/logout` → **200** `{}` + 清 cookie(幂等)。
- `GET /dashboard/api/me` → **200** `{"sub","name","email":null,"is_admin":bool}`;会话无效/过期 → **401**。
- `GET /dashboard/api/users`(仅 admin)→ **200** `[{"username","created_at","admin"}]`;非 admin **403**。

### 6.2 会话客户端(保留)

`ui/src/lib/session.ts` 不动:`sessionQueryOptions`(key `["session"]`,`api.me()`,
401 → `null`,`staleTime: 60_000`,`retry: false`)+ `useSession()`。
`SessionUser` 类型按 §7.2 裁剪为 `{ login: string; email: null; is_admin: boolean }`
(后端返回 `sub`,客户端字段名沿用 `login`——`api.me()` 的解析处做一次映射:
`{ login: body.sub, email: null, is_admin: body.is_admin }`,保持全站 `user.login` 引用面不变)。

### 6.3 `routes/login.tsx`(改,完整规格)

布局沿用现有 Card 居中样式(`w-full max-w-md`),内容:

1. **状态机**(仅用 `useQuery`/本地 state + TanStack `useMutation`,不引新依赖):
   - `useSession()` 加载中 → 骨架屏(现状保留)。
   - 已登录(`session.data` 非空)→ `ClientRedirect` 到 `intendedPath`(现状保留:
     `redirect` 查询参数经 `rememberAuthRedirect`/`consumeAuthRedirect` 处理,`auth-redirect-core.ts` 不动)。
   - 未登录 → 表单。
2. **表单字段**:`username`(文本,必填)+ `token`(password,必填)。
   提交 = mutation `POST /dashboard/api/auth/login`(走现有 `request()` 封装:
   `credentials: "include"`、JSON body、错误时抛 `ApiError(status, detail)`)。
3. **错误映射**(直接展示 `ApiError.message`,即后端 `detail`):
   - 401 + `invalid token` → "共享 token 不正确"。
   - 401 + `unknown user` → "用户不存在;需要由管理员创建账号"(并**显示注册表单**,见 4)。
   - 其余按 message 原文显示。
4. **注册入口**(可选,仅当后端 401 `unknown user` 时出现):同页折叠区
   "由管理员创建账号",字段 `username` + `token`,mutation `POST /auth/register`;
   201 → 提示创建成功并回到登录;409 → 重名提示;403 → 无权限提示(非 admin 且非首用户)。
5. **成功处理**:mutation onSuccess → `queryClient.setQueryData(["session"], {login, email:null, is_admin})`
   + `window.location.replace(intendedPath)`(整页跳,确保 SSR 下一跳带 cookie;
   不用软导航——避免 SPA 内残留的旧缓存与 SSR 登录墙语义打架)。
6. **桌面分支**:保留现有 `isDesktop && "Continue in local mode"` 按钮(`enableDesktopLocalMode`,
   不动)。
7. **文案**:标题 "Sign in to Open SWE";描述改为
   "使用部署方分发的共享 token 登录。登录名决定你的会话与线程归属。"
   **删除** "Use your GitHub account… Slack/Linear/GitHub triggered runs" 一句。

### 6.4 登录墙行为矩阵(SSR + 客户端,验收对照用)

| 场景 | 行为 |
|---|---|
| 未登录 SSR 访问 `/agents` | 服务端 302 → `/login?redirect=%2Fagents`(首字节前) |
| 未登录 SSR 访问 `/login` | 直接渲染登录页(公开路径) |
| 未登录客户端导航到受保护路由 | `RequireLogin` → `Navigate /login?redirect=…`(`auth-redirect.tsx` 不动) |
| 登录后访问 `/login?redirect=/agents/$id` | `session.data` 就绪 → `ClientRedirect` 到 `/agents/$id` |
| 会话过期(7 天后)访问任意页 | SSR:401 → 重定向 `/login`;客户端:query 401 → `null` → `RequireLogin` 接管 |
| 跨 origin 部署(`VITE_DASHBOARD_API_BASE_URL` 异 origin) | SSR 跳过会话解析(`isCrossOriginApiBase`),客户端 `credentials:"include"` 直连 API origin;登录表单 action 指向 `${API_BASE}/dashboard/api/auth/login`(现有 `request()` 已处理 base) |
| 预渲染(`TSS_PRERENDERING=true`) | 跳过会话解析,产出 `_shell.html` |

### 6.5 多用户并发(无需额外 UI 机制)

- 每个浏览器一个 `osw_session`(sub=username);后端按 `local_user` 过滤 `/threads*`、
  非本人线程 404 归一化。**UI 侧不传任何用户参数**——身份 100% 来自 cookie。
- 实现约束:全站 **不得**出现手动构造 `X-OpenSWE-User` / `Authorization` 头的代码
  (那是 dashboard 进程内部对运行时的事,§4.2 的 UI 代理也不加)。
- admin 差异:仅 (a) `admin` 页可见性(§10.4),(b) 线程列表 `all=true` 开关
  (`ThreadsPageParams.all` 已有字段,仅 `is_admin` 时渲染勾选框)。

---

## 7. API 客户端改造(`ui/src/lib`)

### 7.1 保留并微调

| 文件 | 处理 |
|---|---|
| `api-base.ts` | 不动(`open-swe:` 协议 → base 为 `""`,desktop 语义保留) |
| `dashboard-fetch.ts` | 不动(SSR cookie 转发 = 同 origin 部署的会话可见性基础) |
| `session.ts` / `session-ssr.ts` / `auth-redirect*.ts` | 不动(§6.2/§4.4) |
| `plan.ts` | 不动(plan 后端 7 端点保留;其 `PlanUser.login` 等字段与后端一致) |
| `hydration.ts` / `query.ts` / `notifications.ts` | 不动 |
| `desktop-local-mode.ts` | 不动(desktop) |

### 7.2 `src/lib/api.ts`(改,精确增删)

**删除的函数**:`reviewImageProxyUrl` + `GITHUB_IMAGE_HOST_RE`、`isGithubReauthError`、
`loginUrl`、`slackConnectUrl`、`notionConnectUrl`、`reviewChatApiBase`、
`transcribeAudio`(后端 `/voice/*` 已删)及其测试引用。

**删除的类型**:`PRTraceResolutionResult`、`UserMapping`、`UserMappingsPage`、
`UsageLeaderboard*`、`ReviewerStats*`、`Installation`、`Repository`、`ReposPayload`、
全部 `Review*`/`ReviewerEval*`、`DatadogConnectBody`、`LangSmithConnectBody`、
`CurrentsCredentialStatus`/`CurrentsConnectBody`、`NotionCredentialStatus`、
`TeamCredentialsStatus`、`ProviderCredentialStatus`。

**删除的 `api` 方法**:`repos`、`listReviewStyles/createReviewStyle/getReviewStyle/
saveReviewStylePrompt/analyzeReviewStyle/cancelReviewStyle/deleteReviewStyle`、
`listAutoReviewRepos/setAutoReviewRepo`、`usageLeaderboard`、`myMapping`、
`adminListUserMappings/adminDeleteUserMapping`、`listReviews/getReview/getReviewDiff/
getReviewChat/listReviewChatThreads/deleteReviewChatThread/reReview/resolveTrace/
createReviewComment/listReviewComments/updateReviewComment`、`getReviewerEval`、
`getTeamSettings/saveTeamSettings/saveTranscriptionModel/getTeamCredentials/
connectDatadog/disconnectDatadog/connectLangSmith/disconnectLangSmith/
getMyCurrentsStatus/connectCurrents/disconnectCurrents/getMyLangSmithStatus/
connectMyLangSmith/disconnectMyLangSmith/getMyNotionStatus/disconnectNotion`、
`getSandboxSettings/saveSandboxSettings`、`listRepoSnapshots/createRepoSnapshot/
getRepoSnapshot/getRepoSnapshotTemplate/saveRepoSnapshot/buildRepoSnapshot/
deleteRepoSnapshot`、`listEnvironmentOptions`(后端 `/environments*` 全删;
新线程不选环境,Docker 镜像是部署级配置(§11);`EnvironmentsSection` 降级为只读说明,§10.3)。

**保留的 `api` 方法**(与后端保留端点 1:1):
`me`(映射 `sub`→`login`)、`options`、`profile`/`saveProfile`、
`getMyInstructions`/`saveMyInstructions`/`deleteMyInstructions`、
`listSkills`/`createSkill`/`saveSkill`/`deleteSkill`、
`listOrganizationSkills`/`createOrganizationSkill`/`saveOrganizationSkill`/`deleteOrganizationSkill`、
`listAgentInstructions`/`createAgentInstructions`/`getAgentInstructions`/
`saveAgentInstructions`/`deleteAgentInstructions`、`logout`。
**新增**:`listUsers: () => request<Array<{ username: string; created_at: string; admin: boolean }>>("/users")`
(admin 页用,403 由调用方处理)。

**保留的类型**(微调):`ModelOption`、`OptionsPayload`、`SessionUser`(去
`slack_oauth_enabled?`、`login` 语义改为 username)、`Profile`/`ProfileUpdate`(§7.3)、
`AgentInstructions`、`UserInstructions`、`Skill`/`SkillInput`/`SkillsPage`/`OrganizationSkillsPage`。

### 7.3 `Profile` 裁剪

```ts
export interface Profile {
  login?: string
  email?: null
  default_model?: string
  reasoning_effort?: string
  default_subagent_model?: string | null
  subagent_reasoning_effort?: string | null
  updated_at?: string
}
// ProfileUpdate 同构(去掉 default_repo/base_branch/branch_prefix/auto_fix_ci/draft_prs/review_draft_prs)
```
对应 UI:`PreferencesSection` 只渲染 model/effort/subagent 四项(§10.3)。

### 7.4 整删文件

`repo.ts`、`repoCache.ts`、`datadog.ts`、`reviewImage.ts`(若存在独立文件)、
及其 `*.test.*`。`_components` 里引用它们的仓库选择器见 §10.6。

---

## 8. Agents 功能层

### 8.1 `features/agents/lib/api.ts`(改,精确增删)

**删除**:`getThreadPullRequestStatus` + `AgentPullRequestStatusResponse` 类型、
`connectCloudTerminal` + `CloudTerminalConnection`、`listWorkflowApprovals/approveWorkflowPush/
rejectWorkflowPush` + `WorkflowPushApprovalsResponse`(**workflow-approval 端点后端保留**,
但现 UI 的审批条是为 GitHub workflow push 设计;本地分支变体不产生该类审批——
端点保留、UI 入口删;若后端注入 `workflow_push_guard` 类工具仍触发,审批条作为
**通用机制保留**也合规。**决策:保留这 3 个方法与 UI 审批条,文案去 GitHub 化**
("推送审批" 改为通用 "操作审批")。)、`downloadThreadRecoveryPatch` +
`agentsBlobRequest` + `filenameFromContentDisposition` + `ThreadRecoveryPatch`
(后端 `/threads/{id}/recovery.patch` 已删)。

**保留**:`listSidebarThreads`、`listThreadsPage`、`resolveThread`、
`listSchedules/createSchedule/updateSchedule/triggerSchedule/deleteSchedule`、
`getThread`、`queueMessage`、`cancelThread`、`adminCancelThread`、`deleteThread`、
`getThreadBranchDiff`、`getThreadWorkingTreeDiff`、`streamUrl`、`groupThreads`、
`agentsLangGraphApiUrl`、`agentsRequest`。

**类型改造**:
```ts
// ThreadPrDiffFile → 保留字段,重命名为 ThreadDiffFile(path/previousPath/status/
//   additions/deletions/originalContent/modifiedContent/unrenderable)
// ThreadBranchDiff → 保留,字段语义本地化:
//   { branch: string | null, baseRef: string, baseSha: string, headSha: string,
//     truncated: boolean, files: Array<ThreadDiffFile> }   // prNumber 字段删除
// ThreadTurnDiff → 原样(working-tree diff 契约不变)
// ScheduleCreate/UpdateRequest → 去 slack_channel_id / slack_notification_mode;
//   去 admin_thread? 保留(后端保留该字段则留,以后端 body 定义为准)
//   repo 字段:保留(字符串,自由输入;线程归属哪个本地仓库,agent 据此定位宿主挂载)
```
`SlackNotificationMode` 类型删除;`AgentSource` 枚举裁剪为
`"dashboard" | "schedule" | "local"`(现有 source 过滤器 UI 按此渲染,§8.3)。

### 8.2 `features/agents/lib/types.ts`(改)

`AgentThread` 保留字段:`id/title/repo/branch/model/effort/planMode/planStatus/
source/triggerKind/automationId/automationName/status/viewed/resolved/isOwner/
createdAt/updatedAt/messages/queuedMessages/sandboxId?`。
删除/停用:`traceUrl?`(LangSmith trace,无 tracing 面;保留字段声明但不再渲染链接)、
`sourceUrl?`(仅删渲染,字段可留)。
`branch` 字段语义:**本地分支名 `local/<slug>`**,UI 直接展示(§9.1)。
`AgentStatus` 保留 `idle|running|error|interrupted`。

### 8.3 线程列表与侧栏(改)

- `agents/threads.tsx`:过滤器的 `source` 下拉选项改 §8.1 裁剪后的枚举;
  `status` 过滤保留;`q` 全文搜索保留(后端 `/threads/page` 契约不变)。
  **新增** `all`(看所有人的线程)复选框,仅 `session.data.is_admin` 可见。
- `AgentsSidebar`(布局 `agents.tsx`):active/resolved 两段结构不变
  (`/threads/sidebar` 契约保留);侧栏线程行的 `repo/branch` 徽标保留,
  `branch` 现在显示 `local/<slug>`;`isOwner` 为 false(仅 admin `all=true` 时出现他人线程)
  加 "他人" 角标;admin 对他人线程可 `adminCancelThread`(后端保留 `POST /admin/threads/{id}/cancel`)。
- `sidebarFilter.ts`/`sidebarPrefs.ts`/`threadViews.ts` 不动(纯本地状态)。

### 8.4 发送消息流(`useSubmitAgentMessage`,协议不动,注释去 GitHub 化)

现实现(§2.1 已核)完整保留:
1. `stream.isLoading`(busy)→ **乐观入队**:`POST /threads/{id}/messages`
   (`{content, images, model_id, effort, plan_mode}`),成功在 query cache 追加
   `queuedMessages` 气泡("Queued next" 卡片);失败回滚。
2. idle → 先探测 `POST /messages`(不带乐观气泡);**409**(线程实为 busy)→ 转 1 的乐观路径;
   非 409 错误 → 抛出。
3. 探测 **409 "thread is idle"**(后端语义:idle 线程发消息应走 run.start)→
   `stream.submit({ messages: [{type:"human", content}] }, { config })`,
   `configurable` 注入 `agent_model_id/agent_effort/plan_mode`(非空才带)。
   `stream.submit` **不 await**(run 结束才 resolve);`.catch` 时
   `setAgentThreadStatus(…, "error")`(原注释里 "expired OAuth token → 401" 改为
   "session expired → 401")。
4. `onSuccess` 乐观置 `status: "running"`。

后端侧:`/messages` busy 时写入 store 队列 `("queue", thread_id)/"pending_messages"`,
由 `check_message_queue` 中间件在下一次 model call 前注入(FIFO,上限 100,超限丢最旧)——
UI 无感,保留。`run.start` 经 `POST /threads/{id}/commands` 透传 + 服务端富化
(lazy 建线程、`metadata.local_user` 打 owner、`injected_dynamic_context_hashes` 去重)——
UI 无感,保留。**LLM gate 排队**:默认 `OPEN_SWE_LLM_MAX_CONCURRENCY=1` 时,
`run.start` 会在后端串行等待;UI 表现 = 提交后到首 token 的等待变长,
**不需要**任何 UI 改动(也不加前端队列)。

### 8.5 流 provider 与运行状态(不动 + 微调)

- `AgentThreadStreamProvider.tsx`:保留 `StreamProvider` + `Client` +
  `toAbsoluteApiUrl` + `overrideFetchImplementation(dashboardFetch)`(cookie 同传)+
  `onRequest`(SDK 请求强制 `credentials:"include"`)。
  `assistantId="agent"`;`apiUrl = toAbsoluteApiUrl(agentsLangGraphApiUrl)`。
  `threadId` 为 `null` 时 SDK lazy-create 首条消息建线程(首页新建流程,不动)。
  `onCreated/onCompleted` 的 query 失效逻辑不动。
  **transport 参数**:`"local"` 分支(桌面 `/local-graph`)保留,web 永不使用。
- `agentRunStatus.ts` `isAgentRunLive(serverRunning, streamIsLoading)` **原样保留**
  (服务端轮询真值优先;反向不降级)。轮询源 = `getThread`(详情 query 周期性 refetch,
  现值保留)。
- 停止按钮:`POST /threads/{id}/cancel`(经 `agentsApi.cancelThread`,不动)。

### 8.6 git 面板与 diff 面板(去 PR 化,详见 §9)

`queries.ts` 中 `agentThreadKeys` 删除 `pullRequestStatus`;
`branchDiff`/`workingTreeDiff` 保留;`workflowApprovals` **保留**(§8.1 决策)。
`diffPanelStore.ts`/`rightPanelStore.ts`/`gitPanelPreferences.ts` 保留(纯本地偏好)。
`terminalGroups.ts`/`terminalSession.ts`/`terminalState.ts`/`terminalTabTitle.ts`
**整删**(云终端面删除;本地变体无 web 终端)。

### 8.7 automations(保留 + 字段裁剪)

`features/automations/` 保留(cron 解析 `cron.ts`、模板 `automation-templates.ts`、
未保存离开守卫)。表单字段:`name/prompt/cron/repo?(自由文本)/model_id?/effort?`。
删除 `slack_channel_id`/`slack_notification_mode` 输入与展示。
触发结果 `ScheduleTriggerResult` 保留(`status/schedule_id/thread_id/run_id?`)。

---

## 9. Git 面板与本地分支 UI

> 交付形态:agent 在 Docker 沙箱内把改动 commit 到 `local/<slug>` 分支
> (分支名与 slug 规则由后端 `LOCAL_COMMIT_SECTION` 定义,UI 只展示)。
> **UI 不发起 commit/push**;它只读后端代理的两个 diff 端点。

### 9.1 数据源

| 数据 | 端点(后端保留) | 缓存 key | 刷新策略 |
|---|---|---|---|
| 分支 diff(相对 base 的已提交改动) | `GET /threads/{id}/branch-diff` | `agentThreadKeys.branchDiff(id)` | run 完成(`onCompleted`)失效;面板打开时 refetch |
| 工作树 diff(未提交改动,turn 粒度) | `GET /threads/{id}/working-tree-diff` | `agentThreadKeys.workingTreeDiff(id)` | 同上 |
| 线程元(branch 名) | `GET /threads/{id}` | `detail(id)` | 流生命周期事件 |

`branch-diff` 响应按 §8.1 的 `ThreadBranchDiff`(去 `prNumber`,加 `branch` 字符串)渲染。

### 9.2 `AgentGitPanel.tsx`(改)

结构(现有 tab 布局)保留,内容改:
1. **分支条**:显示 `thread.branch`(形如 `local/fix-login-401`)+ `headSha` 短哈希 +
   `+additions −deletions` 汇总。**删除** PR 号/PR 链接/PR 状态徽章
   (原 `pull-request-status` 轮询整条删除:无"CI 通过/失败"概念)。
2. **"Files changed" tab**:`branch-diff` 的 `files` 列表,行 = 文件路径 + 增删数 +
   状态字母(M/A/D/R);点击 → `DiffFilesView`/`chat/DiffView` 打开单文件 diff
   (shiki 高亮组件**不动**)。
3. **"Uncommitted" tab**:`working-tree-diff` 同构渲染;`status: "missing"` 显示空态
   "工作树干净"。
4. **删除**:"Fix failing checks / 修复 CI" 按钮(依赖 PR status)、
   "Open PR" 按钮(本变体不存在)、recovery patch 下载按钮。
5. **保留**:面板折叠/展开偏好(`readStoredPanelCollapsed`/`writeStoredPanelCollapsed`)、
   消息内文件链接 → 展开面板并定位文件(`handleOpenFile`,不动)。

### 9.3 `ChangesPanel.tsx` / `DiffFilesView.tsx` / `chat/DiffView.tsx`(保留)

纯 diff 渲染组件,契约 `ThreadDiffFile` 不变;仅把 "PR diff" 文案改为 "分支 diff"。

### 9.4 删除的组件/逻辑

- PR 状态轮询 hook/组件(现挂在 `AgentThreadView`)、`fixPullRequest` 回调、
  `AgentPullRequestStatusResponse` 一切消费方。
- 云终端 tab(`CloudTerminal` 相关,§8.6 已删 lib 文件)。
- PR 图片代理调用点(`reviewImageProxyUrl` 消费方,消息渲染里若有则改为直接 src)。

---

## 10. 设置页 / 侧边栏 / 杂项

### 10.1 `features/settings/components/`

| 组件 | 处理 |
|---|---|
| `AccountSection.tsx` | 改:显示 username(= 登录名)+ admin 徽章 + "退出登录"(`api.logout` + 清 `["session"]` cache + 跳 `/login`)。删除 GitHub 头像/`avatar_url`/email 展示(恒 null) |
| `PreferencesSection.tsx` | 改:仅 model / reasoning effort / subagent model / subagent effort 四个选择器(数据源 `api.options()`) |
| `PullRequestsSection.tsx` | **删**(draft PR / 自动修 CI / 审查偏好) |
| `ConnectionsSection.tsx` | **删**(Slack/Notion/Currents/Datadog/LangSmith 连接) |
| `PersonalInstructionsSection.tsx` | 保留(后端 `/me/instructions` 保留) |
| `EnvironmentsSection.tsx` | 降级:后端 `/environments/*`、`/repo-snapshots*`、`/sandbox-settings` 全删后,
  该节改为**只读说明卡片**("沙箱:本地 Docker 容器;镜像与资源限制由部署方在 `DOCKER_SANDBOX_*`
  环境变量配置,无需在此设置。")。无 admin 分支逻辑。 |
| `RepoSelector.tsx` | 删(依赖 `/repos`);替代见 10.6 |

### 10.2 `my-settings.tsx` 组装

保留 `AppShell` 布局;节顺序:Account → Preferences → PersonalInstructions → Environments(只读)。

### 10.3 退出登录统一语义

`SidebarUserMenu.tsx`(保留):头像位改为 username 首字母;菜单 = Settings / Usage?(**删**) /
Sign out。Sign out = `api.logout()`(后端幂等 200)→ `queryClient.removeQueries(["session"])` →
`window.location.replace("/login")`。

### 10.4 `admin.tsx`(改,本地用户页)

- `GET /users`(admin,403 → 渲染 "无权限" 态):表格 `username / created_at / admin`。
- 跨线程操作:表格行不展开;admin 能力入口 = 线程侧栏的 `adminCancelThread`(§8.3)与
  线程列表 `all=true`。本页不再有任何 GitHub 映射 CRUD。
- 保留路由守卫:`session.data.is_admin` 否则 `Navigate /agents`。

### 10.5 `AppSidebar.tsx`(改)

导航项最终态:
- `/agents` "Agents"(保留,含线程/automations/skills 子路由)
- `/my-settings` "Settings"
- `/admin` "Admin"——**仅 `is_admin` 渲染**
删除:`/usage`、`/cloud-agents`、`/review`(及 review 子项)。
`AppCommandPalette.tsx`/`AppShortcutReference.tsx` 同步删对应命令/快捷键行。

### 10.6 仓库选择器替代(threads 新建 / agent-instructions)

`/repos` 删除后,所有"选仓库"处改为**自由文本输入** `owner/name` 形式
(如 `local/my-project` 或任意部署方约定的仓库名):
- 新线程 composer 的 repo 输入(现状若存在 repo picker)→ 文本框 + 本地历史(localStorage,
  key `open-swe.recent-repos`,最近 10 条)。
- `agents_.instructions.tsx`(per-repo 指令)→ 同上;创建/编辑时 `full_name` 文本输入,
  后端校验 `owner/name` 格式(400 时展示后端 detail)。
- 不保留 GitHub URL 归一化逻辑(`repo.ts` 已删)。

### 10.7 可选:LLM gate 观测(默认不做,留设计位)

后端 `GET /debug/llm-gate` 返回 `{mode, max_concurrency, in_flight, waiting:[{thread_id, since}]}`。
若需要,加 `agents/debug.tsx` 只读页(仅 admin,5s 轮询);**不在本期范围**,
仅在实现顺序中预留(见 §13 步骤 7 标注"可选")。

---

## 11. 环境变量表

| 变量 | 作用域 | 值/缺省 | 说明 |
|---|---|---|---|
| `DASHBOARD_API_URL` | Nitro 服务端(构建后运行时读) | 无缺省(未设 → 代理抛错) | 后端地址,缺省建议 `http://localhost:2024`(dev 代理在 vite 层有此缺省) |
| `VITE_DASHBOARD_API_BASE_URL` | 构建时(浏览器 bundle) | `""`(同 origin 部署) | 跨 origin 部署时填 API origin;`open-swe:` 协议下强制 `""`(desktop) |
| `E2E_HARNESS` | dev 进程 | 未设 | mock harness 同 origin 代理开关(保留) |
| — | 浏览器 | — | **UI 不读任何** token/secret;共享 token 只存在于后端 env 与用户手输 |

**UI 侧不新增**任何与 `OPEN_SWE_LOCAL_AUTH_TOKEN`、`DASHBOARD_JWT_SECRET`、
`DOCKER_SANDBOX_*`、`OPEN_SWE_LLM_MAX_CONCURRENCY` 相关的读取——那些是后端进程的事,
前端泄漏即安全事故。

---

## 12. 文件级 留 / 改 / 删 索引

> 相对 `ui/`。"改" 的条目改动量见前文对应小节。

### 删(整文件/整目录)

```
server/                     无
src/routes/$owner.$repo.pull.$number.tsx
src/routes/review.tsx
src/routes/review_.styles.tsx
src/routes/review_.repositories.$owner.tsx
src/routes/usage.tsx
src/routes/integrations.tsx
src/routes/cloud-agents.tsx
src/routes/admin_.evals.tsx
src/routes/agents/reviews/                (目录)
src/routes/agents_.snapshots.tsx
src/routes/agents_.environments.tsx
src/features/reviews/                     (目录)
src/features/agents/lib/terminalGroups.ts
src/features/agents/lib/terminalSession.ts(.test)
src/features/agents/lib/terminalState.ts(.test)
src/features/agents/lib/terminalTabTitle.ts
src/features/agents/components/  云终端相关组件(实现时按 import 图定位)
src/features/settings/components/PullRequestsSection.tsx
src/features/settings/components/ConnectionsSection.tsx
src/features/settings/components/RepoSelector.tsx
src/components/RepoSnapshotsPanel.tsx
src/components/ReviewStylesPanel.tsx
src/lib/repo.ts
src/lib/repoCache.ts
src/lib/datadog.ts
src/lib/reviewImage.ts(若独立存在)
```

### 改

```
vite.config.ts                          §4.3(BACKEND_PREFIXES、生产 handlers)
server/backend-proxy.ts                 §4.2(去 redirect:manual)
src/lib/api.ts                          §7.2/§7.3(大改:删 GitHub/review/云面,留 agent 面)
src/lib/session-ssr.ts                  §4.4(仅注释校准,行为保留)
src/routes/login.tsx                    §6.3(OAuth 按钮 → token 表单 + 注册)
src/routes/admin.tsx                    §10.4(映射管理 → 本地用户页)
src/routes/my-settings.tsx              §10.1/10.2(删两节,Environments 降级)
src/routes/agents.tsx                   §5.2(排除名单)
src/routes/agents/threads.tsx           §8.3(source 枚举 + all 开关)
src/routes/agents_.instructions.tsx     §10.6(repo picker → 文本)
src/features/agents/lib/api.ts          §8.1(删 PR/终端/recovery,分支 diff 类型本地化)
src/features/agents/lib/queries.ts      §8.6(删 pullRequestStatus key)
src/features/agents/lib/types.ts        §8.2(去 trace/sourceUrl 渲染依赖)
src/features/agents/lib/provider/useSubmitAgentMessage.ts   §8.4(仅注释)
src/features/agents/lib/AgentThreadStreamProvider.tsx       §8.5(注释;行为不动)
src/features/agents/components/AgentThreadView.tsx          §9(删 PR 轮询/fixPullRequest)
src/features/agents/components/AgentGitPanel.tsx            §9.2
src/features/agents/components/ChangesPanel.tsx / DiffFilesView.tsx / chat/DiffView.tsx  §9.3(文案)
src/features/agents/components/AgentsHome.tsx / AgentsSidebar.tsx / AgentsThreadsPage.tsx §8.3
src/features/automations/*              §8.7(表单字段裁剪)
src/features/settings/components/{AccountSection,PreferencesSection,EnvironmentsSection,PersonalInstructionsSection}.tsx  §10.1
src/components/AppSidebar.tsx           §10.5
src/components/SidebarUserMenu.tsx      §10.3
src/components/AppCommandPalette.tsx / AppShortcutReference.tsx  §5.1 同步
src/components/AppShell.tsx             §5.1(若内含被删路由链接)
```

### 留(不动)

```
src/routes/__root.tsx, index.tsx, agents/index.tsx, agents/$threadId.tsx,
src/routes/agents/$threadId_.plan.tsx, agents/automations/*, agents/skills.tsx,
src/routes/agents/local/$sessionId.tsx
src/lib/{api-base,dashboard-fetch,session,auth-redirect,auth-redirect-core,
         plan,hydration,query,notifications,desktop-local-mode,utils}.ts(x)
src/features/agents/lib/{queries(部分),streamMessagesToUi,structuredInputMessages,
    messageTimestamps,summaryEvent,contextUsage,queuedMessages,diffPanelStore,
    rightPanelStore,gitPanelPreferences,sidebarFilter,sidebarPrefs,threadViews,
    toolNames,apiWarmup,useRunCompletionNotifier,agentRunStatus,desktopLocal*,
    desktopProjects,provider/*(除删项)}.ts(x)
src/features/agents/components/composer/*, messages/*, 计划审批组件
src/components/ui/*(shadcn 原子组件)
package.json / tsconfig / tailwind 配置 / vitest 配置
```

---

## 13. 实现顺序(AI 执行步骤)

> 每步结束必须:`pnpm --filter open-swe-dashboard typecheck` 与
> `pnpm --filter open-swe-dashboard test`(vitest run)绿;按步提交(每 commit 带
> co-author trailer)。**一次只做一步,先删后改,先 lib 后 UI**。

1. **lib 层去 GitHub**(`ui/src/lib/`):删 §12"删"清单中的 lib 文件;
   改 `api.ts`(§7.2/7.3,含 `me` 的 `sub`→`login` 映射);跑 typecheck,
   按报错逐个清理 `features/` 与 `routes/` 的失效 import(此时页面暂留、编译红)。
2. **删路由与功能目录**:按 §5.1 删 10 个路由文件 + `agents/reviews/` +
   `features/reviews/` + §12 组件;清
   `AppSidebar`/`AppCommandPalette`/`AppShortcutReference` 的失效链接与引用。
   此时 typecheck 应全绿。
3. **登录页**(`login.tsx` §6.3 + `session-ssr.ts` 注释 §4.4):实现表单/注册/错误映射;
   手工验证 §6.4 矩阵中"未登录/已登录/过期"三行。
4. **Nitro 服务端**(`backend-proxy.ts` §4.2 + `vite.config.ts` §4.3):
   `pnpm dev` 起本地栈(`:2024` 后端 + `:3000` UI)冒烟:登录→建线程→发消息→看流。
5. **agents lib 改造**(`features/agents/lib/api.ts` §8.1、`queries.ts` §8.6、
   `types.ts` §8.2):删 terminal 四文件、PR status、recovery patch;
   `ThreadBranchDiff` 本地化。
6. **agents 组件改造**:§9 git 面板(分支条/两 tab/删按钮)、§8.3 侧栏与列表
   (source 枚举、`all` 开关、admin cancel)、§8.7 automations 字段、
   `AgentThreadView` 删 PR 轮询与 `fixPullRequest`;
   settings 的 `EnvironmentsSection` 降级只读(§10.1)。
7. **设置页与 admin**:`my-settings.tsx` 组装(§10.1/10.2)、`AccountSection`/
   `PreferencesSection`/`EnvironmentsSection` 改、`admin.tsx` 本地用户页(§10.4)、
   `SidebarUserMenu` 退出语义(§10.3)。**可选**:LLM gate 观测页(§10.7)。
8. **收尾**:全仓 grep 断言(§14.3)→ `pnpm typecheck && pnpm test` →
   删除孤儿 shadcn 组件(仅被删页面引用的 `ui/` 原子组件,如 `reviewImage` 用过的
   特殊组件;**不删**仍在用的)→ 最终 commit。

---

## 14. 验收清单

### 14.1 功能(手工,本地 `:2024` + `:3000` 双进程)

- [ ] 未登录访问 `/agents` → **302** `/login?redirect=/agents`;表单填错 token → 页内 401 文案;
      正确 `username`+`token` → 回跳,`/me` 返回该用户。
- [ ] 两浏览器不同 username 同时登录:互不可见线程;访问对方线程 URL → 404 态(跳 `/agents`)。
- [ ] 新建线程:composer 提交 → `/commands` `run.start` lazy 建线程 → 流式渲染;
      运行中再发一条 → "Queued next" 气泡,下一次 model call 前注入(后端队列语义)。
- [ ] 停止按钮 → `/threads/{id}/cancel` → 状态 `interrupted`,composer 解锁。
- [ ] 完成后 git 面板:显示 `local/<slug>` 分支 + 文件列表 + 单文件 diff;
      工作树 tab 显示未提交改动;无 PR 徽章、无 CI 状态、无 "Open PR"。
- [ ] plan mode:`/plan` 命令 → 计划页审批(approve/reject/评论)→ 审批后发 run 继续。
- [ ] automations:建 cron 任务、手动 trigger 生成线程。
- [ ] settings:保存 model/effort 后新线程默认生效;退出登录 → cookie 清、跳 `/login`。
- [ ] admin:非 admin 无 `/admin` 入口且 `GET /users` 403 被捕获;admin 可见用户表 +
      线程列表 `all` 开关 + 他人线程 cancel。
- [ ] 会话过期(改 cookie 为过期 JWT)刷新页面 → SSR 重定向登录。

### 14.2 服务端代理

- [ ] 生产构建(`pnpm build` + `node .output/server` 或等价)未设 `DASHBOARD_API_URL` →
      访问 `/dashboard/api/me` 返回 500/502 且日志含 "DASHBOARD_API_URL is not set"。
- [ ] 设置后 `/dashboard/api/**` 全透传(方法、query、body、cookie);
      `/webhooks/…` 请求落到 Nitro 自身 404(不再被代理)。

### 14.3 静态断言(实现完成时全仓 grep 必须为空)

```
ui/src 下(大小写不敏感):
  github            # 允许残留:无(连注释都不留 "GitHub" 字样,desktop 文件除外:ui/src/features/agents/lib/desktop* 与 desktop-local-mode.ts 若有历史注释可留,评审时豁免)
  review-style|reviewStyle|ReviewStyle
  pullRequest|pull_request|PRStatus|pr-diff|prDiff
  fake-gh|fake-slack
  oauth|OAuth       # 允许:auth-redirect* 文件名不含 oauth;代码内不得出现
  slack|notion|currents|datadog|langsmith   # 允许:无
  terminal/connect|connectCloudTerminal
```
(实现时用 `rg -i` 跑上面模式,白名单:测试夹具、desktop 文件、`Co-authored-by` 行。)

### 14.4 工程

- [ ] `pnpm --filter open-swe-dashboard typecheck` 绿;`pnpm ... test` 绿。
- [ ] 无 npm/bun 痕迹(`git status` 无 `package-lock.json`/`bun.lockb`)。
- [ ] 无新增 `package.json` 依赖(diff 为空)。
- [ ] 每个 commit 带 `Co-authored-by: open-swe[bot] <open-swe@users.noreply.github.com>`。

---

## 15. 与后端设计书的契约对齐表

> UI 调用的每个后端端点 → 后端设计书出处。实现时**以两份设计书为准**;
> 若实现中发现本文与后端设计书冲突,以后端设计书为准并回报。

| UI 调用(经 `/dashboard/api`) | 后端设计书位置 | 备注 |
|---|---|---|
| `POST /auth/login`、`POST /auth/register`、`POST /auth/logout` | §3.2 认证端点 | 表单登录(§6.3) |
| `GET /me` | §3.2 | 会话源;401=未登录 |
| `GET /users`(admin) | §3.2 | admin 页(§10.4) |
| `GET /options` | 保留端点清单 | 模型下拉 |
| `GET/PUT /profile` | 保留端点清单 | §7.3 裁剪字段 |
| `GET/PUT/DELETE /me/instructions` | §5(保留) | 个人指令 |
| `GET/POST/PUT/DELETE /skills/*`、`/organization-skills/*` | §5 | 技能页 |
| `GET/POST/GET/PUT/DELETE /agent-instructions*` | §5 | per-repo 指令(full_name 校验 400) |
| `GET /threads`、`/threads/sidebar`、`/threads/page` | §5.2(按 `local_user` 过滤) | 侧栏/列表;`all`(admin) |
| `GET/DELETE /threads/{id}`、`POST /threads/{id}/resolve` | §5.2(404 归一化) | 详情/删除/已解决 |
| `POST /threads/{id}/messages` | §5.2(200 入队 / 409 idle / 502) | §8.4 双路 |
| `POST /threads/{id}/commands` | §5.2(`run.start` 富化、lazy 建线程、409 busy) | SDK 主发送口 |
| `POST /threads/{id}/stream/events`、`GET /threads/{id}/stream` | §5.2 SSE(60s idle / 14400s lifetime) | SDK 流;UI 不自设短超时 |
| `GET /threads/{id}/state`、`POST /threads/{id}/history` | §5.2 有界代理(90s/15s/30s、clamp≤5、退避 3×1.5s、30s 快照缓存) | SDK `getState`/hydrate;UI 透传 503 文案 |
| `GET /threads/{id}/working-tree-diff`、`/branch-diff` | 保留端点 | git 面板(§9) |
| `POST /threads/{id}/cancel`、`POST /admin/threads/{id}/cancel` | §5.2 / §3.4 | 停止/admin 停止 |
| `GET/POST /schedules`、`PATCH/DELETE /schedules/{id}`、`POST /schedules/{id}/trigger` | §5.5 | automations |
| `GET /plan/{thread_id}` 等 7 端点 | §5.3 | 计划审批页 |
| `GET/POST /workflow-approval/{thread_id}…` 3 端点 | §5.4 | 通用审批条(§8.1 决策) |
| `GET /debug/llm-gate` | LLM gate 章 | 可选观测页(§10.7) |

**UI 不再调用(后端已删,调用即 404)**:`/auth/callback`、`/auth/desktop-exchange`、
`/my-mapping`、`/my-credentials/*`、`/notion/*`、`/slack/*`、`/team-settings*`、
`/team-credentials*`、`/repos`、`/review-styles*`、`/reviews/*`、
`/enabled-review-repos`、`/repo-snapshots*`、`/sandbox-settings`、`/environments/*`
(含 `/options` 子路由)、`/voice/*`、`/agent-usage-leaderboard`、`/admin/evals*`、
`/admin/user-mappings*`、`/threads/{id}/pull-request-status`、
`/threads/{id}/terminal/connect`、`/threads/{id}/recovery.patch`、
`/threads/{id}/pr-diff`。

---

*本文档版本:与 `OPEN_SWE_DASHBOARD_SERVER_NO_GITHUB_DESIGN.md`(commit `2cdc2626d` 及之后)
和 `OPEN_SWE_SERVER_2024_DESIGN_NO_GITHUB.md` 同步;后端契约如有演进,§15 表格先行更新。*
