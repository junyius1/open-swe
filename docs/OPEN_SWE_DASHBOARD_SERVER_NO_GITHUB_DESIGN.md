# Open SWE Dashboard 服务器(本地分支·无 GitHub 版)程序设计书

> **目标读者**:AI 编码代理 / 熟悉 LangGraph + FastAPI 的工程师。本文是**自包含**的实现契约:照它就能从零复刻一个「dashboard 服务器」,交付物是**只在本地 git 分支上 commit**的自托管编程 agent 服务器。
> **裁剪范围**:拿掉 **GitHub 登录**、**GitHub 授权(凭据面)**、**提交 PR** 三块。交付收敛为 **本地 git 分支 commit**(push 可选,纯宿主凭据;服务器**不持有任何 GitHub token**)。
> **多用户**:**:2024 运行时与 :8001 dashboard 服务器都必须支持多个人同时登录**——每个用户各自持有会话、各自看到/操作自己的线程与 commit 身份。认证仍是**本地静态 token 面**(不用 GitHub),但**不是"一个 token 一个身份"**:用「共享服务端 token 换 per-user 会话 cookie」+ 可选「本地用户注册表」实现多身份(§3)。两个服务器(同进程/双进程两种部署)都走**同一套**认证与权限代码(§18)。
> **依据**:仓库真实代码(所有模块路径、函数名、env、常量、状态码均可逐条验证) + `docs/OPEN_SWE_SERVER_2024_DESIGN.md`(下称 **2024 设计书**)的架构骨架。未在此重复的 2024 概念(单事件循环铁律、DeltaChannel messages、checkpoint 三表)以 2024 设计书为准,本文在需要处**逐字给出数值与行为**。
> **日期**:2026-10-28

---

## 0. 总览:要复刻的是什么

**Dashboard 服务器 = 单个 Python 进程(`langgraph dev`,in-memory LangGraph 运行时,`localhost:2024`),同时承担三件事:**

1. **LangGraph runtime**:提供 LangGraph SDK 协议的全部 HTTP 面(`POST /threads/{id}/runs`、`GET /threads/{id}/state`、`POST /threads/{id}/history`、SSE 流、threads CRUD/search……),并**执行注册的 graph**(agent 本体)。
2. **Open SWE 业务 API**:通过 `langgraph.json` 的 `"http": {"app": "agent.webapp:app"}` 把 Open SWE 自己的 FastAPI app **挂进同一进程**(`/dashboard/api/*`、`/webhooks/*`、`/health`、`/debug/*`)。
3. **持久化宿主**:checkpointer 与 store 指向 Postgres(`DATABASE_URI`);缺失则降级 InMemory。

```
langgraph dev  (localhost:2024, 单 uvicorn worker, 单事件循环)
├── inmem runtime: REST/SSE 协议面 + run 队列 + stream 管理
│    └── 执行 2 个注册 graph:agent / scheduler
├── FastAPI app (agent.webapp:app → agent.api.app:app)
│    ├── /dashboard/api/…  线程 CRUD、消息、state/history 代理、plan、workflow、schedules、skills
│    ├── /webhooks/run-complete  完成回调(本变体走进程内 loopback)
│    ├── /health  /ok  /debug/queue-probe
├── checkpointer → AsyncPostgresSaver(checkpoint_blobs/checkpoints/checkpoint_writes)
└── store          → AsyncPostgresStore(长期记忆:plan、技能、指令、profile、schedules、队列)
```

前端(TanStack Start / Vite,`localhost:3000`)通过 `VITE_DASHBOARD_API_BASE_URL` 跨源访问 `:2024` 的 `/dashboard/api`(dev 下 CORS 放行)。

**本变体相对 2024 设计书的差异(总表):**

| 域 | 2024 版 | 本变体 | 落点 |
|---|---|---|---|
| 身份/登录 | GitHub OAuth(`oauth.py`)+ PKCE + org 门 | **多用户本地 token 认证**:共享服务端 token 换 per-user 会话(可选本地用户注册表);**不用 GitHub** | `agent/local_auth.py`、`agent/dashboard/auth.py`(新增)、`agent/dashboard/user_registry.py`(新增)、`routes.py` |
| 注册图 | 5 个:agent/reviewer/analyzer/chat/scheduler | **2 个:agent + scheduler** | `langgraph.json`、`agent/graphs/*` |
| 沙箱 | 6 种 provider(langsmith/daytona/modal/runloop/e2b/local)+ 每沙箱注入 GitHub App token 作 `gh` 代理 | **删掉全部 5 种云 provider;唯一 provider = 新增本地 `docker`**(`SANDBOX_TYPE=docker`,每线程一个容器,无任何 token);`local` 仅作裸开发回退 | `agent/integrations/docker.py`(新增)、`agent/utils/sandbox.py`、`agent/server.py` |
| 提交交付 | `open_pull_request` 工具 + PR 守卫中间件 + PR 提示段 | **仅本地分支 commit**;push 可选;`LOCAL_COMMIT_SECTION` 提示 | `agent/prompt.py`、工具列表 |
| Webhook 触发 | Slack/Linear/GitHub + run-complete | **只保留 run-complete**(进程内 loopback) | `agent/webhooks/*`、`agent/dispatch.py` |
| Dashboard 端点 | 117 个,含大量 PR/review/OAuth | 裁剪到 **线程/消息/state-history/plan/workflow/schedules/skills/health** | `agent/dashboard/*` |
| env | 大量 `GITHUB_*`/`SLACK_*`/`LINEAR_*`/OAuth | 最小集(§14) | `.env` |

**明确移除(从上游一起拿掉,强依赖 GitHub/PR):** `reviewer`/`analyzer`/`chat` 三图及工厂、`agent/review/*`、PR CI 监视(`/baby-sit`)、`session_cost`(Slack 成本)、Linear/Slack/Notion 集成与 webhook、`open_pull_request`/`request_pr_review`/review 工具族、`refresh_github_proxy`/`pr_creation_guard`/`workflow_push_guard` 中间件、`fetch_review_diff`/`publish_review` 等。

---

## 1. 铁律:单事件循环(`pin_single_event_loop`)

`agent/utils/event_loop.py` —— **全进程只允许一个事件循环**。`agent/api/app.py` 在**模块 import 时**调用一次,又在 **lifespan 启动时**再调用一次。

实现逻辑(照抄语义):
- 强制 `BG_JOB_ISOLATED_LOOPS=false`。
- 若 `langgraph_api.config` 已**没有** `BG_JOB_ISOLATED_LOOPS` 这个开关(运行时模型变了)→ **直接 `RuntimeError` 拒启动**——"跑在隔离循环上比不启动更糟"。
- 若用户设了 `BG_JOB_ISOLATED_LOOPS=1` → 强制关掉并打 warning。

**原因**:沙箱后端按 thread 缓存在**进程级全局注册表**里,循环亲和(loop-affine)。inmem 后台队列若开隔离循环,一个 run 留在某循环的注册表状态,下一个 run 换了循环就取不到 → **线程永久卡死**。

**推论(性能事故根,决定 §7 的所有"有界读"设计)**:所有 HTTP 处理、graph 执行、checkpoint 反序列化共享这一个循环;任何同步 CPU 重活(大 blob 反序列化)会**冻结整个进程**,包括其他请求、SSE 流、健康检查。

---

## 2. 运行时配置:`langgraph.json`(裁剪后)

```jsonc
{
  "python_version": "3.12",
  "api_version": "0.12.6",
  "graphs": {
    "agent":     "agent.graphs.agent:traced_agent",
    "scheduler": "agent.graphs.scheduler:get_scheduler"
  },
  "dependencies": ["."],
  "http": { "app": "agent.webapp:app" },
  "checkpointer": {
    "path": "agent/postgres_persistence.py:create_checkpointer",
    "ttl": { "strategy": "delete", "sweep_interval_minutes": 60, "default_ttl": 43200 }
  },
  "store": { "path": "agent/postgres_persistence.py:create_store" },
  "webhooks": { "url": { "disable_loopback": false } },   // 本变体关键差异,见 §9
  "env": ".env"
}
```

- `agent/graphs/*.py` 是 **3 行 re-export shim**(`from agent.server import get_agent, traced_agent`)。本变体只留 `agent.py`、`scheduler.py` 两个 shim;**删除** `reviewer.py`/`analyzer.py`/`chat.py` shim 及对应工厂。
- **checkpointer TTL = 12h,删除策略,每 60min 清扫**——不活跃线程的 checkpoint 整体删除(不是截断)。
- `webhooks.url.disable_loopback=false`:官方策略开关(CLI 启动序列化为 `LANGGRAPH_WEBHOOKS` env)。开启后**相对 URL 经进程内 loopback client 直接 POST 到本进程的同一 FastAPI app**——不走网络、不暴露端口、无 https 要求。这是本变体能用**相对** `COMPLETION_WEBHOOK_URL` 的前提(§9.1)。
- 无 `DATABASE_URI` 时 `create_checkpointer/create_store` 降级 `InMemorySaver/InMemoryStore` 并告警(进程重启即丢状态,仅裸开发用)。

---

## 3. 身份与登录(替换 GitHub OAuth,多用户)

**目标**:dashboard 会话认证不再依赖 GitHub,用一个最小、可本地验证的认证面;同时 **:2024 运行时与 :8001 dashboard 服务器都支持多个用户同时登录**——每个用户独立的会话、独立的线程归属、独立的 commit 作者身份。

### 3.1 多用户模型(核心设计)

认证凭据是**一个共享的服务端 token**(`OPEN_SWE_LOCAL_AUTH_TOKEN`,证明"能访问本机"),**身份由登录时的 `username` 决定**,会话按用户隔离:

1. **登录**:`POST /auth/login {username, token}`。
   - `token` 与 `OPEN_SWE_LOCAL_AUTH_TOKEN` 经 `hmac.compare_digest` 比对(防时序),不等 → **401**(不区分"缺"和"错",防探测)。
   - `username` 是**自由字符串**(1–64 字符,`[A-Za-z0-9._-]+`)。若启用了本地用户注册表(§3.1.1)则 username 必须已存在;否则任意新用户名**自动创建**(首次登录即注册)。
   - 成功 → **200** `{sub, name, is_admin}` + `Set-Cookie: osw_session=<按用户签名的会话值, sub=username、exp>`。
2. **会话**:cookie 值**按用户签名**(含 `sub`)——两个浏览器各拿各的 cookie,服务端互不串号。`_SESSION_DEP` 解出 `{sub, name, is_admin, email:null}`。
3. **归属与隔离**(§3.4):thread metadata 的 `local_user` = 会话 `sub`;列表只返回本人线程;commit 作者 = 该用户的 git 身份(§3.5)。**多人可同时在线、同时开 run**——run 本身无用户互斥(一个线程同时只允许一个活跃 run 的 busy 语义保留,但不同线程完全并行;同一线程的多人操作按 §5.2 的 owner 判定)。
4. **admin**:`CONFIGURED_ADMINS` env(逗号分隔的 username 列表)内的用户 `is_admin=true`(可看 `all=true` 的全量列表、`admin/*` 端点);其余用户 `is_admin=false`。本地部署通常把第一个登录的人写进 `CONFIGURED_ADMINS`。

**两种部署、同一套认证**(详见 §18):
- **同进程**(`langgraph dev` 挂了 `http.app`):dashboard 面用会话 cookie;协议面(LangGraph REST/SSE)用 `Authorization: Bearer`——bearer 形态见 §3.1.2。
- **双进程**(运行时 `:2024` + dashboard uvicorn `:8001`):dashboard 进程用会话 cookie 面向浏览器,内部调运行时用**进程间 bearer**(§3.1.2);浏览器**不直接**访问 `:2024` 的协议面(所有读都经 `:8001` 的代理端点,§5.2)。

### 3.1.1 可选:本地用户注册表

- 存储:store 命名空间 `local_users`,key=username,value `{"username","created_at","admin":bool,"git_name"?,"git_email"?}`。**多进程部署下两个服务器共享同一 store(同一 Postgres)** → 用户表天然跨进程一致。
- `OPEN_SWE_USER_REGISTRY=1`(默认开):登录要求 username 已存在(否则 401 `{"detail":"unknown user"}`),另提供 `POST /auth/register {username, token}` 由 admin 或首个用户创建账号(`token` 同样要过共享 token 校验)。
- `OPEN_SWE_USER_REGISTRY=0`(裸开发):任意 username 首登自动建条目。
- 注册表里可存**每用户 git 身份**(`git_name`/`git_email`),用于 §3.5 的 commit 作者;未存则回退宿主默认。

### 3.1.2 协议面 bearer(多用户版 `agent/local_auth.py`)

现网 `agent/local_auth.py` 是"单 token → 固定 `local-user`"。改为**复合 bearer 支持多身份**:

- **形态 A(内部服务间,默认)**:`Authorization: Bearer <OPEN_SWE_LOCAL_AUTH_TOKEN>` → 校验相等后,身份取 **`X-OpenSWE-User: <username>`** 头(dashboard 代理转发请求时注入,§5.2)。无该头 → 身份 `local-service`(仅允许机器级端点)。
- **形态 B(浏览器/SDK 直连协议面,可选)**:`Bearer <token>:<username>`(分隔符 `:`)→ 校验 `token` 相等后身份 = `username`。
- 返回 `{"identity": <username>}`;scheme 非 bearer / token 缺 / 错 / 用户头非法 → **401** `{"detail":"Invalid bearer token"}`。
- 校验必须 `hmac.compare_digest`,且**整个协议面 + dashboard 面共享同一 token**,不引入第二份 secret。

### 3.2 登录入口(替换整个 OAuth 链)

新增 `agent/dashboard/auth.py`(端点挂 `/dashboard/api`):

| 端点 | 行为 |
|---|---|
| `POST /auth/login` | body `{"username","token"}`。token 错 → **401** `{"detail":"invalid token"}`;注册表开启且用户不存在 → **401** `{"detail":"unknown user"}`;成功 → **200** `{"sub","name","is_admin"}` + `Set-Cookie: osw_session=…`。 |
| `POST /auth/register` | body `{"username","token"}`(仅 admin 或首用户可建,见 §3.1.1)。成功 **201** 用户对象;重名 **409**;token 错 **401**。 |
| `POST /auth/logout` | → **200** `{}` + `Set-Cookie: osw_session=; Max-Age=0`。无 cookie 也 200(幂等)。 |
| `GET /me` | → **200** `{"sub","name","email":null,"is_admin":bool}`。会话无效/过期 → **401**。 |
| `GET /users`(admin) | → **200** `[{"username","created_at","admin"}]`;非 admin **403**。 |

会话 cookie `osw_session`:`HttpOnly`、`SameSite=Lax`(http 本地)或 `SameSite=None; Secure`(https)、`Path=/`、`Max-Age=7d`。值用 `DASHBOARD_JWT_SECRET`(HS256)签名,`sub`=username。**不要用进程内存存会话**——双进程部署下 dashboard 与运行时是两个进程,内存会话不共享;签名 cookie 天然跨进程(只要两进程共享 `DASHBOARD_JWT_SECRET`)。

### 3.3 删除的登录模块(全部不要)

`agent/dashboard/oauth.py` 的 GitHub 流程(`GITHUB_AUTHORIZE_URL`、PKCE `state/nonce`、`issue_session`/`decode_session` 的 GitHub 依赖、`enforce_org_login_gate`、`require_same_origin*` 里的 GitHub 分支)、`github_token_auth.py`、`oidc_auth.py`、`slack_oauth.py`、`notion_oauth.py`、`user_mappings.py` 的 GitHub 映射、`profiles.py` 里给 `open_pull_request` 取用户 OAuth token 的 `get_valid_access_token`。

> **CSRF 门保留机制**:原 `require_same_origin_for_mutations` 对非 GET 请求校验 `Origin` 白名单(取自 `DASHBOARD_BASE_URL` + `DASHBOARD_ALLOWED_ORIGINS`)。本变体**保留这个 Origin 白名单校验**(防 CSRF),只是不再为 "bearer-github-token" 开例外;静态 token 的 bearer 请求可沿用同样的豁免。本地开发未配任何 origin 时 no-op。

### 3.4 权限模型(多用户隔离)

保留 `_assert_thread_owner / _assert_thread_readable / _assert_thread_postable` 的**机制**,但**改判据为多用户**:
- owner 判据从 `metadata.github_login == session.login` 改为 **`metadata.local_user == 会话 sub`**(即登录 username)。**不再恒真**——A 用户看不到/改不了 B 用户的线程。
- thread metadata 里 `github_login` 字段**改名为 `local_user`**,值 = 建线程者的 username;`participant_logins` 记录参与过该线程的所有 username。
- **列表隔离**:`GET /threads*` 按 `metadata.local_user == sub` 过滤(`all=true` 仅 admin,§3.1 第 4 点)。
- `404 归一化`保留:线程不存在**或**非本用户(且非 admin)→ 一律 **404**(不泄漏存在性,防枚举他人线程 id)。
- **admin 例外**:admin 可读/可 cancel 任意线程(`admin/*` 端点);普通用户之间完全隔离。
- **并发**:多个用户同时在线无互斥;同一线程同时只允许一个活跃 run(`multitask_strategy="interrupt"` 语义保留,§9.1)——后到用户发消息会**打断**前一个 run,这是产品语义,不是 bug。跨用户同线程场景(共享部署)由 `participant_logins` + 消息署名 `@<username>: ` 前缀(现网 `_attribution_prefix` 机制)区分归属。

### 3.5 沙箱内 git 身份(多用户 commit 作者,经 Docker 容器注入)

现网 `configure_git_identity` 写死 bot 身份(`OPEN_SWE_BOT_NAME/EMAIL`)。本变体改为**按用户、经容器挂载**:
- **每个线程一个 Docker 容器**(`SANDBOX_TYPE=docker`,§4.1);容器启动时把该线程的 `.gitconfig-sandbox` **挂载进容器**并设 `GIT_CONFIG_GLOBAL=/workspace/.gitconfig-sandbox`(机制同 `local.py` 的 `_scoped_git_config_env`,只是作用域从宿主文件变成容器挂载)。
- **`.gitconfig-sandbox` 内容 = 该线程 owner 的 git 身份**:优先取本地用户注册表里该用户的 `git_name`/`git_email`(§3.1.1);未配置则回退宿主机 `~/.gitconfig` 的 `user.name`/`user.email` 值(读出来写进容器文件,不做 `[include]` 宿主文件)。
- **效果**:用户 A 的线程 commit 作者是 A,用户 B 的线程 commit 作者是 B——多用户各自留痕,且互不污染宿主环境。
- 删除强制 bot 身份写入;2024 提示里 "Set the commit identity" 那一步**整段删除**(§11)。
- 注意:这是**线程级**身份(一个线程只有一个 owner),不是请求级——同一线程里多人发消息,commit 仍记 owner 身份,消息归属靠 `@<username>:` 署名区分(§3.4)。

---

## 4. 图(保留 2 个)

### 4.1 `agent`(主编程 agent,`agent/server.py` 工厂)

- 用 **`deepagents.create_deep_agent`** 构建。`system_prompt=""`(运行时由 `PrepareRun` 中间件渲染注入 `rendered_system_prompt` 通道)。
- **后端:每线程一个 Docker 沙箱**(§4.4)。`ensure_sandbox_for_thread(thread_id, repo, ...)` **去掉** `github_proxy_token`/`github_proxy_repositories`/`base_proxy_config` 参数与 `_resolve_proxy_token`/`_create_sandbox_with_proxy`/`_refresh_github_proxy` 整条链——直接 `create_sandbox()`(此时 `SANDBOX_TYPE=docker`)。`backend.start()` 在工厂里调用,run 结束不销毁(容器保持,线程复用)。沙箱缓存/复用/循环亲和不变。
- 子代理:`general-purpose`(完整沙箱工具)保留;`browser` 可选(不依赖 GitHub)。
- plan mode / skills / repo instructions 保留;Linear 工单上下文**删除**。
- 工具列表见 §6。

### 4.2 `scheduler`(cron 扇出,`agent/scheduler.py`,唯一不用 deepagents 的图)

`StateGraph(SchedulerState)`,单节点 `launch`,按 `task` 分派。**裁剪后只保留:**
- `reconcile` → `reconcile_stale_runs()`(清扫僵尸 run)
- `background_tasks` → `monitor_background_tasks(thread_id)`
- 默认 → `launch_scheduled_agent_run(schedule_id)`(定时/唤醒:建/复用 agent 线程并 dispatch)

**删除**:`baby_sit`(`evaluate_watch`,CI 监视,强依赖 GitHub 检查)、`session_cost`(按 Slack 线程刷成本)。`agent/baby_sit.py`、`agent/session_cost.py` 删除。

### 4.3 删除的图

`reviewer` / `analyzer` / `chat` 全删。随之删除:`agent/review/*`、`agent/chat.py`、`agent/analyzer.py`、`agent/reviewer.py`、`agent/dashboard/{review_api,review_styles,review_style_jobs,review_chat_api,pr_diff,pull_request_status,repo_cache,repo_snapshots,enabled_repos,agent_usage}.py`。

### 4.4 本地 Docker 沙箱 provider(新增 `agent/integrations/docker.py`)

**目标**:替代全部 5 种云 provider(langsmith/daytona/modal/runloop/e2b 全删),唯一 provider = `docker`,每线程一个容器,跑在宿主机 Docker daemon 上。**服务器不注入任何云/GitHub token**,只需宿主可达 Docker socket。

**接线**(扩展点,改动极小):
- `agent/utils/sandbox.py` 的 `SANDBOX_FACTORIES` 是纯 dict(`name → (module, function)`):加一行 `"docker": ("agent.integrations.docker", "create_docker_sandbox")`;删掉 `langsmith/daytona/modal/runloop/e2b` 五个条目(以及对应 `integrations/{langsmith,daytona,modal,runloop,e2b}.py` 文件,§16)。
- `validate_sandbox_startup_config()` 加 `docker` 分支:校验 Docker daemon 可达(`docker info` 成功)+ 默认镜像可解析,失败启动即报错(同现有 provider 的 boot-time 校验语义)。
- `SANDBOX_TYPE` 缺省值从 `langsmith` 改为 **`docker`**。

**provider 契约**(`create_docker_sandbox(sandbox_id) -> SandboxBackendProtocol`,参考 `local.py` 的 `LocalShellBackend` 写法):

| 步骤 | 行为 |
|---|---|
| **镜像** | `DOCKER_SANDBOX_IMAGE`(env,缺省 `python:3.12-slim`,需自带 git;镜像由宿主预 pull 或首次创建时 pull)。 |
| **命名/复用** | 容器名 `oswe-sbx-<thread_id 前 12 位>`;`create_docker_sandbox(sandbox_id)`:sandbox_id(= 容器名)已存在且存活 → 复用;已删除 → 报错(`SandboxGoneError`,调用方重建);不存在 → 新建。 |
| **创建参数** | `docker create`:工作目录 `/workspace/<repo>`;`-v <宿主仓库绝对路径>:/workspace/<repo>`(bind mount,git 对象库共享宿主);`-v <宿主线程序列化 .gitconfig-sandbox>:/workspace/.gitconfig-sandbox:ro`;env `GIT_CONFIG_GLOBAL=/workspace/.gitconfig-sandbox`;`-e SANDBOX_THREAD_ID=<thread_id>`;默认 `--network=bridge`(可 `DOCKER_SANDBOX_NETWORK=none` 断网);**不挂** Docker socket、**不注入**任何 API key env。 |
| **start** | 首启 `docker start -d`;之后每次 run 的 `backend.start()` = 检查存活(`docker inspect` 的 `State.Running`),死了则 start,仍失败 → `SandboxGoneError`。 |
| **命令执行** | `execute(cmd)` = `docker exec -i -w /workspace/<repo> <容器> sh -lc <cmd>`;流式 stdout/stderr;超时 = `SIGTERM` 后 `SIGKILL` 该 exec 进程(不杀容器)。 |
| **文件操作** | `ls/read/write/glob/grep` 优先走 **bind mount 的宿主路径直读**(零拷贝、快);仅当仓库不在本地 bind 时回退 `docker exec cat/ls`。 |
| **生命周期** | run 结束**不销毁**(线程复用,同现网);线程删除(`DELETE /threads/{id}`)时 best-effort `docker rm -f`(失败只告警);`recreate_sandbox` 工具 = `docker rm -f` + 重新创建。 |
| **资源** | 可选 `-m`(内存)/`--cpus` 上限,env `DOCKER_SANDBOX_MEM`/`DOCKER_SANDBOX_CPUS`,缺省不限。 |

**多用户/隔离要点**:
- 每个线程的容器独立挂载各自仓库目录与 `.gitconfig-sandbox` → **commit 作者按 owner 隔离**(§3.5)、各线程工作区互不污染。
- 容器内**没有**宿主 `~/.gitconfig`(不 include)、没有 GitHub token、没有 `gh` 代理 → 不变式 7("无 GitHub 凭据")在沙箱层面天然成立。
- push 凭据:宿主仓库目录是 bind mount,push 时容器内 git 走宿主仓库的 credential 配置(如需隔离,可加 `-v <per-user credential 目录>:/home/sbx/.git-credentials:ro`,超出最简形态)。
- **`local` provider 保留为裸开发回退**(`SANDBOX_TYPE=local`,无 Docker 环境时),不删文件,但不再是推荐默认。

---

## 5. Dashboard 业务层

### 5.1 应用组装(`agent/api/app.py`)

```
create_app():
  lifespan: pin_single_event_loop()(二次)
            → validate_sandbox_startup_config()(校验 SANDBOX_TYPE=docker:daemon 可达 + 镜像可解析)
            → validate_local_dev_llm_config()
            退出: close_cached_models()
  add_middleware(QueueDebugMiddleware)  # OPEN_SWE_QUEUE_DEBUG=1 启用每秒事件循环 lag 探针
  CORS(DASHBOARD_ALLOWED_ORIGINS;禁止 "*" + credentials)
  include_router(dashboard_router)         # /dashboard/api/*
  include_router(plan_router)              # /dashboard/api/plan/*
  include_router(workflow_approval_router) # /dashboard/api/workflow-approval/*
  include_router(health_router)            # /health /ok /webhooks/run-complete /debug/queue-probe
  # 删除 slack/linear/github webhook router
```

- **auth 钩子换成 §3 的多用户本地 token 认证**。
- 路由前缀约定:`dashboard_router = APIRouter(prefix="/dashboard/api")`、`plan_router = APIRouter(prefix="/dashboard/api/plan")`、`workflow_approval_router = APIRouter(prefix="/dashboard/api/workflow-approval")`。
- `_SESSION_DEP = Depends(require_session)`:从 `osw_session` cookie 解出**当前登录用户**,无效/过期 → 401。返回 `{"sub": <username>, "name": <username>, "email": None, "is_admin": bool}`(§3.1)。
- **协议面 auth**:挂 `agent/local_auth.py` 的 `Auth.authenticate`(§3.1.2 复合 bearer:共享 token + `X-OpenSWE-User`/`token:username` 定身份)。
- **代理转发身份注入**:dashboard 进程内所有对运行时的 `langgraph_client()` 调用 / httpx 代理(§5.2)统一加 `Authorization: Bearer <OPEN_SWE_LOCAL_AUTH_TOKEN>` + `X-OpenSWE-User: <当前会话 sub>` 头(§3.1.2 形态 A),使运行时侧的 thread 归属/权限与当前用户一致。

### 5.2 线程核心端点(`routes.py` + `thread_api.py`)

**列表/详情/删除/状态**

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `GET /threads` | 📖 **线程列表**(侧栏/首页数据源) | query `limit(≤50,默认50)`/`all?`(admin) → **200** `[ThreadListItem]`;**按 `metadata.local_user == sub` 过滤**(多用户隔离,§3.4;`all=true` 且 admin 才看全量) | 401 未认证 |
| `GET /threads/sidebar` | 📖 侧栏精简列表(active/resolved 两段) | **200** payload + `Server-Timing` 头 | 401 |
| `GET /threads/page` | 📖 分页列表(过滤+排序) | query `limit(≤100,默认25)/offset/all?(admin)/resolved?/viewed?/source?/status?/q?/scope=all\|interactive\|automation/sort_by=updated_at\|created_at` → **200** `{"items":[…],"limit","offset","hasMore":bool}`;无结果 = 200 空 items;**底层 `POST /threads/search` 的 metadata 过滤必须含 `local_user == sub`**(`all=true` 且 admin 时去掉) | 400 参数非法 |
| `GET /threads/{id}` | 📖 **线程详情元数据**(*transcript 不在这里*——前端另调 `/state` hydrate);`mark_viewed=true` 默认置已读 | **200** ThreadListItem 全字段 | **404 归一化**(不存在或非本身份) |
| `DELETE /threads/{id}` | 🗑️ **删线程** + 其 checkpoint/流(不可恢复) | **204** | 404 |
| `POST /threads/{id}/resolve` | ✅ **标记已解决/未解决**(列表分组) | body `{"resolved": bool}` → **200** 更新后列表项 | 404 |
| `POST /threads/{id}/cancel` | 🛑 **取消线程全部活动 run**(`cancel_many(action="interrupt")`) | **200** 列表项;`latest_run_status="interrupted"` | 404;502 运行时不可达 |
| `POST /threads/{id}/runs/{run_id}/cancel` | 🛑 取消指定 run | **200** 透传 | 404;run 已终态 → 400/409 |

`ThreadListItem` 字段:`id, title, repo, branch, model, effort, planMode, planStatus, source, threadCategory, triggerKind, status(idle|running|error|interrupted), viewed, viewedAt, createdAt, updatedAt, lastUserMessage?`。

**发消息 = 触发 run**

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `POST /threads/{id}/messages` | ⚡ **发消息(用户主动交互入口)**。`ThreadMessageBody = {content(≤20000), images[{data(base64),media_type,filename?}], model_id?, effort?, plan_mode: bool}`。 | 见下方流程 → **200** 列表项 | 400 content 空/超 20000;401;502 无法判活/入队失败;409 线程 idle 却走 messages 路径 |

`send_dashboard_message` 流程(照抄语义,去掉 GitHub):
1. `client.threads.get(thread_id)` → 404 归一化。
2. `_assert_thread_postable(metadata, local_user)`。
3. 更新 metadata:`source="dashboard"`, `plan_mode`, `participant_logins`, `model/effort`, `resolved→False`。
4. `get_thread_active_status(thread_id)`(见下)判活:
   - **None(无法判活)→ 502**。
   - **不活跃 → 409** `{"detail":"thread is idle; start a run via the stream commands endpoint"}`(引导走 `/commands` 的 `run.start`,它会 lazy 建线程)。
5. 活跃(busy)→ 把消息**注入 store 队列**:`client.store.put_item(("queue", thread_id), "pending_messages", {"messages": [...cap 100...]})`,由 agent 的 `check_message_queue` 中间件在**下一次 model call 前**读出注入为 human 消息(FIFO,`MAX_QUEUED_MESSAGES=100`,超限丢最旧)。
6. 返回 `_thread_summary(thread)`。

`get_thread_active_status(thread_id)` 语义:线程 `status == "busy"` **且** 有 live `pending`/`running` run 才返回 `True`;`busy` 但无 live run(僵尸,如 dev server 重启残留)→ `False`(允许客户端开新 run 自愈);判活抛异常 → `None`(调用方 502)。

**SDK command 透传 + 富化(前端 `StreamProvider` 主口)**

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `POST /threads/{id}/commands` | ⚡ **SDK command 信封透传 + 服务端富化**(§15.4)。前端主发送口;`run.start` 时服务端注入 `configurable` + `metadata`。 | raw JSON body → 运行时 `{"type":"success","run_id"?}` 时 **200** 原样透传;`{"type":"error",…}` 时 **200 + 同体透传**(*业务错在体里,HTTP 仍 200,前端按 `type` 判*) | 线程不存在且 `method≠run.start` → **404**;非 JSON → **400**;busy 且 `run.start` → **409** `{"detail":"thread is already running; queue message instead"}`;运行时不可达 → 502 |

`proxy_dashboard_thread_commands` 关键逻辑:
- `method=="run.start"` 且线程不存在 → **lazy 建线程**(`creating=True`),调 `_create_dashboard_thread_record(...)` 打全量 metadata(owner=`local_user`、title=prompt[:80]、repo、model、plan_mode)。
- `_enrich_run_start_command`:生成 `prepare_run_id=uuid4()`,把 `injected_dynamic_context_hashes` 去重(从已有 state 的 `<dynamic-context>` 提取 hash),`build_input_messages` 重包 XML 信封(surface=`web`, sender=`<当前会话 username>`),合并 `configurable`(见 §15.3),`params["assistant_id"]="agent"`,`params["stream_mode"]=_DASHBOARD_STREAM_MODES`,`params["stream_resumable"]=True`,写回 thread metadata。
- 成功后异步:更新 `latest_run_status="pending"`/`latest_run_id`,`state_cache.invalidate(thread_id)`,并起一个 TTFT 观测 task(开 `runs/{run_id}/stream?stream_mode=messages` 记录首字延迟)。
- 本变体:`_ensure_dashboard_github_token`(现网要求用户有可用 GitHub OAuth token 才能发 run)→ **删除**,换成静态 token 已通过即放行。

**有界 state/history 代理(主 transcript 读取)**

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `GET /threads/{id}/state` | 🔁 **有界 state 代理**(主 transcript 唯一来源) | **200** ThreadState;**归一化**:线程存在但从未 run → `{"values":{},"next":[]}`(200 非 404) | 404;15s 预算耗尽 → 503;上游 5xx → 502 |
| `POST /threads/{id}/history` | 🔁 **有界 history 代理**(发现/跳转,*禁止全量翻页*) | raw body `{"limit"?,"before"?}` → 无过滤请求 `limit` 强制 clamp ≤5 → 90s 预算 → **200** 透传 history 数组(≤limit,最新在前) | 超时 → 503 `{"detail":"thread history request timed out; retry"}`(退避链见下);415 非 JSON |

`get_dashboard_thread_state` 流程:
1. `threads.get` → 404 归一化。
2. `_refresh_latest_run_metadata`:查 runs 列表拿 `latest_run_status`/`latest_run_id`,若与 metadata 不同则 `threads.update` 回写(denormalize)。
3. 判 `run_in_flight`(`thread.status==busy` 或 latest run pending/running)。
4. **run_in_flight** → `state_cache.invalidate`;否则先查 `state_cache.get(thread_id, version)`(version = `state_updated_at`/`updated_at`)命中则**直接返回内存快照,不进运行时单循环**。
5. miss/运行中 → `asyncio.wait_for(client.threads.get_state(thread_id), timeout=15.0)`:
   - **TimeoutError** → 降级为**线程记录上已有的 denormalized `values`** 包成 `{"values", "next":[], "metadata", ...}`(而非 500),live stream 会后续对账。
   - 成功 → 缓存结果(idle 线程)。
6. `run_in_flight` → **删掉结果的 `next` 字段**(SDK `useStream` 只在 `next` 非空/缺失时开 live 订阅;REST 建 run 后最新 checkpoint 可能还是上一轮的 `next==[]`,删掉让 SDK 认为活跃)。

`proxy_dashboard_thread_history` 的**三层防御**(单循环性能事故的防线,与 GitHub 无关,逐字保留):

| 机制 | 值 | 作用 |
|---|---|---|
| `_DISCOVERY_HISTORY_LIMIT` | **5** | 无 `before/checkpoint/metadata` 过滤的请求 limit 强制 ≤5 |
| `_HISTORY_PROXY_TIMEOUT` | 90s | history 遍历预算 |
| `_HISTORY_COALESCING`(prune at 32) | — | 相同 payload 的并发请求**合并为一次**上游调用(`thread_id:json(payload)` 为 key,`asyncio.Event` 共享) |
| `_FALLBACK_ATTEMPTS` / `_FALLBACK_RETRY_BASE_DELAY` | 3 / 1.5s(指数退避) | 超时后退避:`POST /history {"limit":1}` → 仍败 → 包装 `GET /state` 单条(同 history 元素形状,客户端无感) |
| `_FALLBACK_SNAPSHOT_CACHE_TTL_SECONDS` | 30s | 降级快照按线程缓存(同线程并发去重) |
| `_STATE_PROXY_TIMEOUT` / `_GET_STATE_TIMEOUT` | 30s / 15s | 单快照读预算 |

- 退避耗尽仍败 → **503** `{"detail":"thread history request timed out; retry"}`。
- **已知边界**:客户端超时/放弃**不取消**服务端已入队的遍历;被放弃的遍历仍占唯一事件循环,后续请求(含 fallback)排在其后——所以"有界 + 合并 + 退避"是三层防御。

**SSE 流代理**

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `POST /threads/{id}/stream/events` | 📡 **SSE 透传 v2 事件流**(前端 `StreamProvider` 主口;晚连接可见历史事件——`stream_resumable` 的价值) | **200** `text/event-stream` | 404/415 先于流;上游 ≥400 → 流内 `event: error` 帧后关闭 |
| `GET /threads/{id}/stream` | 📡 **SSE join 线程流**(带 `last-event-id` 头重放) | **200** `text/event-stream` | 404 先于流 |

`_stream_thread_events` 关键行为(照抄语义):
- `httpx.AsyncClient(timeout=None)` 开 `client.stream("POST", {langgraph_url}/threads/{id}/stream/events)`,逐 chunk 透传。
- **客户端断开必须释放上游订阅**:传 ASGI `receive` 进 generator,起 `_watch_for_disconnect` task 消费 `http.disconnect`;`asyncio.wait({read_task, disconnect_task}, timeout=deadline)` 谁先到就 break;deadline = min(`_STREAM_IDLE_TIMEOUT`(env `OPEN_SWE_STREAM_IDLE_TIMEOUT_SECONDS`,默认 60s), `_STREAM_MAX_LIFETIME`(env `OPEN_SWE_STREAM_MAX_LIFETIME_SECONDS`,默认 14400s) 剩余)。客户端断/超 idle/超 lifetime → 取消 read_task、结束流、`finally` 里 `cancel()` 并 `suppress` await。
- 上游 ≥400 → 发 `event: error\ndata: {"status","detail"}` 帧后 return。
- `GET /stream`(join_stream 形态):`client.threads.join_stream(thread_id, last_event_id)` 逐帧转 `id: ...\ndata: {json}\n\n`。

### 5.3 Plan 端点(`plan_api.py`,7 个)— 存储:store 命名空间 `plans`

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `GET /plan/{thread_id}` | 📝 **读当前 plan**(内容+状态+审批人) | **200** `{"content","content_format","status":"draft|pending|approved|rejected","author_login","comments":[…]}` | **404** 该线程无 plan |
| `PUT /plan/{thread_id}` | 📝 **保存/更新 plan**(进入待审批) | body `{"content","content_format":"html|markdown"}` → **200** 更新后对象 | 400 content 空/格式非法;404 线程不存在 |
| `POST /plan/{thread_id}/approve` | ✅ **审批通过**;*前端随后发 `run.start`(`plan_mode=false`),服务端不自动跑* | **200** `status="approved"` | 404 无 plan;已终态 → **409** |
| `POST /plan/{thread_id}/reject` | ❌ **审批拒绝** | **200** `status="rejected"` | 404;409 同上 |
| `GET /plan/{thread_id}/comments` | 📖 **列 plan 评论** | **200** `[{"comment_id","author_login","content","created_at"}]` | 404 无 plan |
| `POST /plan/{thread_id}/comments` | 📝 **加评论** | body `{"content"}` → **200** 新评论 | 404;400 空 |
| `DELETE /plan/{thread_id}/comments/{comment_id}` | 🗑️ **删评论** | **204** | 404;非作者 → 403 |

### 5.4 Workflow 审批端点(`workflow_approval_api.py`,3 个)— 存储:thread metadata `workflow_approvals`

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `GET /workflow-approval/{thread_id}` | 🚦 **列待决/历史审批** | **200** `{"approvals":[{"fingerprint","message","status":"pending|approved|rejected","created_at"}]}`;无 → 空数组 | 404 线程不存在 |
| `POST /workflow-approval/{thread_id}/{fingerprint}/approve` | ✅ 批准 | **200** status=approved | 404;已决 → 409 |
| `POST /workflow-approval/{thread_id}/{fingerprint}/reject` | ❌ 拒绝 | **200** status=rejected | 同上 |

### 5.5 Schedules / Automations(5 个)— 落地:每个 schedule = 运行时一条 cron(`POST /runs/crons`,`assistant_id=scheduler`)

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `GET /schedules` | ⏰ 列定时任务 | **200** `[{"id","name","cron","prompt","enabled","next_fire_at","last_result"?}]` | 401 |
| `POST /schedules` | ⏰ 新建(到点由 scheduler 扇出 `launch_scheduled_agent_run`) | body `{"name","cron","prompt","enabled"}` → **200** 新建(含 `id`/`next_fire_at`) | 400 非法 cron(透传);401 |
| `PATCH /schedules/{id}` | ⏰ 改名/改 cron/启停 | **200** 更新后 | 404;400 非法 cron |
| `DELETE /schedules/{id}` | 🗑️ 删(连带删运行时 cron) | **204** | 404 |
| `POST /schedules/{id}/trigger` | ⏰ 立即触发一次 | **200** `{"triggered":true,"run_id"?}` | 404;502 |

### 5.6 Skills / repo instructions / 用户指令 / profile / options

| 端点 | 存储 | 说明 |
|---|---|---|
| `GET/POST /skills`、`PUT/DELETE /skills/{name}` | store `user_skills` | name 小写字母/数字/单连字符(≤64);description ≤1024;instructions ≤20000。POST 重名 → 409;name 非法 → 400 |
| `GET/POST /organization-skills`、`PUT/DELETE /organization-skills/{name}` | store `organization_skills` | 组织级技能(所有登录用户可见,仅 admin 可写);用户级 `/skills` 按 `sub` 隔离 |
| `GET/POST /agent-instructions`、`GET/PUT/DELETE /agent-instructions/{full_name}` | store `agent_instructions`(键 `owner/name`) | per-repo 指令,注入系统提示。full_name 非 `owner/name` → 400 |
| `GET/PUT/DELETE /me/instructions` | store `user_instructions`(键=local_user) | 用户级指令,跨线程共享面。GET 无则 `{"instructions":""}` |
| `GET/PUT /profile` | store `profiles`(键=local_user) | 模型默认、effort、base_branch 等本地化字段 |
| `GET /options` | 代码常量 `SUPPORTED_MODELS` | **200** `{"models":[{"id","label","effort_options":[…]}],"default_agent_model","default_agent_reasoning_effort"}`;无可用模型 → 200 空数组 |

### 5.7 Health / debug

| 端点 | 功能 | 成功 | 失败 |
|---|---|---|---|
| `GET /health` | 🩺 存活探测 | **200** `{"status":"healthy"}`(仅进程活着即 200;**不**探测 Postgres/运行时) | 连接拒绝 |
| `GET /ok` | 🩺 裸探活 | **200** `ok` | 同上 |
| `GET /debug/queue-probe` | 🩺 事件循环 lag 探针快照(§13 验收"不冻结 >1s"的观测口) | **200** `{"lag_ms":float,"loop_thread":str,"samples":[…]}`;`OPEN_SWE_QUEUE_DEBUG` 未开 → 200 空样本 | 401 |
| `POST /debug/queue-probe` | 🩺 触发/开关即时探针 | body `{"enabled":bool}` → **200** 同 GET | 400 非该 shape |

### 5.8 可选(本地 git 相关,默认不做,需要时加回)

| 端点 | 功能 | 成功 | 失败 |
|---|---|---|---|
| `GET /threads/{id}/working-tree-diff` | 🌿 沙箱内 `git diff` 物化,前端 diff 面板(*纯本地,不依赖 GitHub*) | **200** `{"diff":str,"branch":str}` | 404 线程/沙箱不存在;无 git 仓 → 200 空 diff |
| `GET /threads/{id}/branch-diff` | 🌿 分支 vs base 的 diff | **200** 同上 | 同上 |

### 5.9 删除的端点(✂️,共约 60 个)

`/admin/*` 的 evals/user-mappings(保留 `POST /admin/threads/{id}/cancel` 作为**多用户 admin 跨线程取消**,§3.4)、`/reviews/*` 全部(含 8 个 PR 聊天代理)、`/review-styles/*`、`/enabled-review-repos`、`/repo-snapshots*`、`/sandbox-settings`、`/environments*`、`/voice/*`、`/agent-usage-leaderboard`、`/threads/{id}/{pull-request-status, terminal/connect, recovery.patch, pr-diff}`、§3.2 所列全部 OAuth/凭据/团队端点(`/my-mapping`、`/my-credentials/*`、`/notion/*`、`/slack/*`、`/team-settings*`、`/team-credentials*`、`/repos`)。变体里访问 = 路由不存在 → **404**。

---

## 6. 工具集(裁剪)

**删除(依赖 GitHub/PR/review/Slack/Linear)**:`open_pull_request`、`request_pr_review`、`manage_baby_sit`、`notify_automation_channel`、`fetch_review_diff`、`publish_review`、`add_finding`、`update_finding`、`list_findings`、`read_finding_outcomes`、`reply_to_finding_thread`、`resolve_finding_thread`、`save_review_style`、`list_review_findings`、`admin_gate` + `ADMIN_TOOLS`、全部 `linear_*`、全部 `slack_*`。

**保留(核心编程 + 本地交付)**:
- 文件系统(deepagents 内置):`read_file`、`write_file`、`edit_file`、`delete`、`glob`、`ls`、`grep`、`task`(子代理)、`execute`(shell,含 `git`)。
- 网络/检索:`http_request`、`fetch_url`、`web_search`。
- plan:`enter_plan_mode`、`save_plan`、`approve_plan`。
- 本地会话/技能:`save_user_instructions`、`save_user_skill`、`delete_user_skill`、`schedule_thread_wakeup`、`background_execute`、`background_task`、`recreate_sandbox`、`sandbox_reset`、`report_platform_issue`、`read_user_settings`、`output_iframe`。

> **本地交付说明**:`execute` 工具里跑 `git add/commit/branch/push` 即满足"提交本地 git 分支"。push 是否需要凭据由宿主 `~/.gitconfig`/credential helper 决定,**服务器不注入任何 token**。`read_repo_file`/`search_repo_code` 若现实现走 GitHub API,则改为本变体的**本地沙箱内读取**(或直接删除,交给 `read_file`/`grep`);不要保留任何 GitHub 访问路径。

---

## 7. 持久化与数据模型

**逐字沿用 2024 设计书 §7**(checkpoint 三表 + DeltaChannel messages 存储与读成本、`_load_blobs` 同步重活、**有界读不变式**、thread `metadata` jsonb 业务状态、store 长期记忆、`DATABASE_URI` 缺失降级)。此处只给本变体差异:

- **thread metadata 删除** `github_login`/`triggering_user_email`/`pr`/`review_check_*` 等 GitHub 字段,owner 判据改 `local_user`(§3.4)。
- **`kind` 只会出现 `agent`**(不再有 `review_chat`/`reviewer`)。

---

## 8. 输入契约:XML 信封(`agent/input_messages.py`)

**所有入站消息统一序列化为用户消息里的 XML 信封**(让模型结构化感知发送者/渠道/系统):

```xml
<input-message sender="alice" surface="web" kind="human">
  <content>…正文(或 content blocks)…</content>
</input-message>
```

- `RunInput = {"messages":[RunMessage], "files"?: {...}}`;`RunMessage = {"role":"user"|"system","content": str|blocks,"id"?}`。
- `build_run_input(content, context, people, channels, systems, files)`:先注入**身份介绍消息**(person/channel/system 的 `<dynamic-context>` 块,按内容 hash 去重,`injected_dynamic_context_hashes` 防重复注入),再注入真正的 `human_input`/`system_input`。
- `Surface`(本变体只出现 `web|desktop|automation`);`kind = human|system`;无人类发送者时合成 `system:<source>` 身份。
- 系统提示经 `wrap_system_prompt` 包进 `<system-instructions format="open-swe-v1">`,追加项以增量 `<input-message>` splice(幂等)。
- 本变体:`PersonIdentity` 不再来自 GitHub/Slack 映射;dashboard 来源直接给一个**当前登录 username** 的 `<dynamic-context>`(platform=`open-swe`,id=`<username>`)。多用户下每个用户各自的身份介绍块,按内容 hash 去重(§5 机制不变)。

---

## 9. 触发契约(`agent/dispatch.py`)+ 完成信号

### 9.1 触发:一切走 `create_durable_run`

**本地版触发源只有两个,都不经过任何 webhook**:
1. **dashboard 表单**(同步 `run.start`,走 `POST /threads/{id}/commands`)
2. **scheduler**(cron,`launch_scheduled_agent_run`)

`create_durable_run` 的固定参数(**复刻必须逐字段一致**):

| 参数 | 值 | 原因 |
|---|---|---|
| `multitask_strategy` | `"interrupt"`(后台 follow-up 可 `enqueue`) | 新消息**打断**活跃 run:同步 checkpoint 已存住进度,新 run 带全量历史+新消息重启 |
| `durability` | `"sync"` | **每步之前**写 checkpoint,崩溃/回收从最后 checkpoint 恢复 |
| `if_not_exists` | `"create"` | 线程不存在时自动建 |
| `stream_mode` | `["values","updates","messages","custom","tasks","checkpoints"]` | Protocol v2 全通道(REST 不接受 `tools/lifecycle`,由 v2 标记隐式启用) |
| `stream_subgraphs` | `True` | 子代理命名空间事件 |
| `stream_resumable` | `True` | 事件流可重放——**晚连接的 dashboard 才能看到别人发起的 run** |
| `configurable.__event_streaming_v2` | `True` | v3 流协议标记(镜像 `langgraph_api.event_streaming` 的 key,不 import 它) |
| `configurable.prepare_run_id` | 新 UUID | 关联 PrepareRun 中间件与 run |
| `webhook` | `/webhooks/run-complete?token=…` | 完成/失败回调(§9.2) |

- `dispatch_agent_run(thread_id, content, configurable, *, source=…, assistant_id="agent")`:`source` 本变体只可能是 `web|desktop|automation|schedule`。`_dispatch_input` 里抽 Slack user/channel/时区、GitHub login、Linear email 的逻辑**删除**;发送者身份取 **`configurable.local_user`**(即当前登录 username,§3)。
- **触发 webhook(slack/linear/github)全部删除**——它们本就是外部平台 POST 进来的,本地无外部平台。

### 9.2 完成信号:`agent/completion.py` + `/webhooks/run-complete`

**投递方式 = 进程内 loopback(本变体关键差异)**。上游部署里 `COMPLETION_WEBHOOK_URL` 必须是**公网 absolute https URL**——因为 inmem 运行时默认 `disable_loopback=true`,相对/localhost webhook 会在 `runs.create` 时 422(防 auth-bypass:loopback 走进程内 ASGI transport,挂载在 `root_path="/noauth"`,绕会话认证)。本变体用 §2 的 `webhooks.url.disable_loopback=false` **合法解除**该约束,于是:

- `COMPLETION_WEBHOOK_URL=/webhooks/run-complete`(**相对路径**;secret 仍必须设,拼 `?token=`)。
- `dispatch.py` 的 `_is_loopback_webhook` 降级逻辑**反转**:loopback 策略已开启时,相对 URL **正常挂**(不再降级为不挂);secret 未设时仍不挂(路由本身 fail-closed 503)。
- **token 校验不可省**:loopback 投递恰好绕过会话认证,`verify_run_complete_token` 是这条路由唯一的门。

路由契约(`agent/api/health.py` 的 `run_complete_webhook`):
- `verify_run_complete_token(?token=)` 与 `RUN_COMPLETE_WEBHOOK_SECRET` 经 `hmac.compare_digest` 相等,否则 **401** `{"detail":"Invalid run-complete token"}`。
- `RUN_COMPLETE_WEBHOOK_SECRET` 未设 → 路由整体 **fail-closed**(启动 warning;`verify` 恒 False → 全 401)。
- body 非 JSON → **200** `{"status":"error","message":"Invalid JSON"}`(*不 5xx*——webhook 失败不应触发运行时重试风暴)。
- body 非 object → **200** `{"status":"ignored","reason":"payload not an object"}`。
- `handle_run_completion(payload)`(幂等骨架保留,**业务层简化**):
  - `success` → **无操作或仅记日志**(不再入队 Slack 成本刷新,`session_cost` 已删)。
  - 终态失败(`error`/`timeout`;`interrupted` **不算**失败——`multitask_strategy="interrupt"` 下正常 follow-up 打断前 run 是健康态)→ `_post_failure_reply` 简化:**不往 Slack/PR 发**,改为**落一条 thread 级失败标记**到 thread metadata(`failure_ack_run_ids` + `latest_run_status`),dashboard 线程列表可见。按 `run_id` 幂等去重;`kind=thread_wakeup` 的自动化唤醒失败静默(ignored)。
  - `_settle_failed_reviewer_check` **删除**(reviewer 没了)。
  - 返回 `{"status":"handled"}`(首次)/ `{"status":"deduped","run_id"}`(重复回调,幂等)/ `{"status":"ignored","reason":…}`。

**webhook body 契约**(`call_webhook` 组装;字段全集 = `allowed_fields` 白名单):
```jsonc
{
  "run_id": str, "thread_id": str, "assistant_id": "agent",
  "created_at": iso8601, "updated_at": iso8601,
  "metadata": {…thread/run metadata…},
  "kwargs": {"input": …, "config": …},
  "multitask_strategy": "interrupt",
  "status": "success" | "error" | "timeout",
  "run_started_at": iso8601, "run_ended_at": iso8601, "webhook_sent_at": iso8601,
  "values": {…最终 state(含 messages)…} | null,
  "error": {"error": str, "message": str}   // 仅失败时
}
```
(最大字段 `values` 可在 `webhooks.allowed_fields` 裁掉,省 loopback 内存。)

**验收**:run 终结后,`/webhooks/run-complete` 收到**进程内** POST 且 `?token=` 正确;用错误 token 调同路由得 401/403;全程无公网回调。

---

## 10. 系统提示(`agent/prompt.py`)

装配框架(`construct_system_prompt` 分段拼接、`<system-instructions>` 包裹、幂等 splice)**保留**。逐段改动:

| 段 | 处理 |
|---|---|
| `WORKING_ENV_SECTION` | 保留(工作目录说明) |
| `REPO_SETUP_SECTION` | **改写**:删除 "Set the commit identity" 那步(§3.5)、删除 PR 相关措辞;保留 clone/同步/建分支/读 `AGENTS.md` |
| `TASK_EXECUTION_SECTION` | **改写**:"commit, push, and (when a PR is warranted) open/update the draft PR" → **"commit 到线程稳定分支;push 仅当用户明确要求且宿主机已有该远端凭据;不创建 PR"** |
| `COMMIT_PR_SECTION` | **整段删除**,替换为新的 `LOCAL_COMMIT_SECTION`(见下) |
| `DESKTOP_PR_SECTION` | **删除** |
| 协作署名(PR footer / `Made by [Open SWE]`) | **删除** |
| PR 规则(不 force-push、PR 标题/体格式、`open_pull_request` 失败处理) | **删除**,保留通用 git 规则:不 force-push、不重写远端历史、follow-up 用新 commit |
| 其余(依赖审查、外部不可信评论、plan mode、自我认知、仓库范围) | 保留;仓库范围段里 GitHub 专属措辞改中性 |

**新增 `LOCAL_COMMIT_SECTION`(替代 `COMMIT_PR_SECTION`),要点**:
- 代码改动后:**`git add` 相关文件 → `git commit`(信息简洁,聚焦 why)→ 提交到线程稳定分支**(如 `local/<short-task-slug>`,同线程复用分支,从已有分支而非 base 续做)。
- **默认只 commit 本地,不 push、不建 PR。** push 仅当用户明确说 push 且 `git push` 可用(宿主凭据),否则报告"已提交到本地分支 `<branch>`,未 push"。
- 绝不 `git push --force`;绝不 amend/rebase 已 push 的 commit;普通 push 被拒则 `git pull --rebase` 再推,冲突则报告并停。
- 交付汇报里**只说本地分支名 + commit 范围**,不生成任何 PR/远端链接。

---

## 11. 本地 git 工作流(替代 PR 交付)

- 线程稳定分支:首次 `run.start` 时 `_create_dashboard_thread_record` 打 `branch` 到 thread metadata(如 `local/<slug>`);agent 在沙箱内 `git checkout -b <branch>` 后工作。
- `ensure_sandbox_for_thread(thread_id, repo, …)`:repo 解析为**本地路径**(`agent/utils/repo.py` 仅本地仓库解析;`repo: owner/name` 语法仍可保留,映射到本地路径或宿主 git URL,由 `DEFAULT_REPO_OWNER` 等解析)。
- **服务器不持有任何 GitHub token**;`gh` CLI 在本变体**不代理、不要求登录**。
- **多用户 commit 作者**(§3.5):每个线程的 `.gitconfig-sandbox` 写入该线程 **owner 的 git 身份**(用户注册表 `git_name`/`git_email`,未配则读取宿主 `~/.gitconfig` 的 `user.name`/`user.email` 值写进去),经 Docker 容器挂载生效(§4.4)。push 凭据由宿主自备(容器 bind mount 宿主仓库目录,push 走宿主仓库的 credential 配置;per-user push 凭据可由注册表扩展,超出最简形态)。
- 可选交付增强(前端 diff 面板):§5.8 的 `working-tree-diff`/`branch-diff` 端点(纯本地 `git diff` 物化)。

---

## 12. 环境变量清单(最终)

```
# 持久化
DATABASE_URI=postgres://…            # 缺失则 InMemory 降级 + 告警
# 模型(多 provider 任选其一,make_model 解析)
ANTHROPIC_API_KEY=…                  # 或 OPENAI_*/FIREWORKS_*/gateway
# 完成 webhook(相对路径 → 进程内 loopback 投递,§9.2;token 必填)
COMPLETION_WEBHOOK_URL=/webhooks/run-complete
RUN_COMPLETE_WEBHOOK_SECRET=…
# 认证(§3,多用户)
OPEN_SWE_LOCAL_AUTH_TOKEN=…          # 共享服务端 token;未设 → dashboard 认证面 fail-closed(503)
CONFIGURED_ADMINS=alice,bob          # admin 用户(逗号分隔 username);可空
OPEN_SWE_USER_REGISTRY=1             # 1=登录要求用户已存在(可 /auth/register 建);0=首登自动注册
DASHBOARD_JWT_SECRET=…               # 会话 cookie 签名(两进程部署必须共享)
# 沙箱(§3.5 / §4.4,唯一 provider = 本地 Docker)
SANDBOX_TYPE=docker
DOCKER_SANDBOX_IMAGE=python:3.12-slim   # 需自带 git;宿主可预 pull
# 可选:DOCKER_SANDBOX_NETWORK=bridge|none、DOCKER_SANDBOX_MEM=2g、DOCKER_SANDBOX_CPUS=2
# 裸开发回退(无 Docker 环境):SANDBOX_TYPE=local + LOCAL_SANDBOX_ROOT_DIR=/path/to/work
# 前端跨源
DASHBOARD_ALLOWED_ORIGINS=http://localhost:3000
DASHBOARD_BASE_URL=http://localhost:3000   # CSRF Origin 白名单来源
# 可选调试
OPEN_SWE_QUEUE_DEBUG=1
OPEN_SWE_STREAM_IDLE_TIMEOUT_SECONDS=60
OPEN_SWE_STREAM_MAX_LIFETIME_SECONDS=14400
DASHBOARD_STATE_CACHE_MAX_ENTRIES=256
```

**删除的 env**:`GITHUB_APP_ID`、`GITHUB_APP_PRIVATE_KEY`、`GITHUB_APP_INSTALLATION_ID`、`GITHUB_APP_CLIENT_ID/SECRET`、`GITHUB_TOKEN`、`SLACK_*`、`LINEAR_*`、`ALLOWED_GITHUB_ORGS`、所有 OAuth client/secret、`E2B_API_KEY`/`E2B_TEMPLATE`、`DAYTONA_API_KEY`/`DAYTONA_SANDBOX_SNAPSHOT`、Modal SDK 凭据、`RUNLOOP_API_KEY`、`LANGSMITH_*` 沙箱 token(仅当仍用 LangSmith 追踪时保留,与凭据无关)、`OPEN_SWE_BOT_NAME/EMAIL`(不再强制 bot 身份)。

---

## 13. 关键不变式 + 验收清单

**沿用 2024 设计书 §11 全部 6 条**:
1. 单进程单事件循环;任何同步重活不得上主循环(重活要么有界,要么 `to_thread`)。
2. 消息只增不减(`add_messages`),思考块在**写 checkpoint 前**剥离(保 `signature`)。
3. 所有 run 走 `create_durable_run` 契约(sync 持久化 + v2 流 + resumable + 完成 webhook)。
4. 面向用户的 checkpoint 读必须有界(state 单快照 或 history ≤5);全量遍历只允许后台显式任务。
5. 失败必须幂等、fail-closed(webhook token、404 归一化、auth 未配置 503)。
6. checkpointer TTL 12h 删除;业务状态放 thread metadata / store,不依赖 checkpoint 长存。

**本变体追加**:
7. **进程 env 与运行时内存中 grep 不到** `GITHUB_APP` / `GITHUB_TOKEN` / 任何 OAuth/云沙箱(E2B/Daytona/Modal/Runloop)token 字段;`gh` CLI 不代理、不要求登录;沙箱 = 本地 Docker 容器,git 身份经容器内挂载的 `.gitconfig-sandbox` 注入,容器内无宿主 `~/.gitconfig`、无任何 token。
8. **交付物 = 本地分支 commit**;push/PR 永远不作为默认行为,仅用户显式要求 + 宿主凭据可用时才 push。
9. **认证面单一 + 多用户**:本地 token 面(`hmac.compare_digest`);**不使用 GitHub/OIDC/Slack 任何登录路径**;支持多个用户同时登录,会话/线程/指令/skills/profile **按 username 隔离**;不存在第二份认证 secret。

**最小复刻验收**:
- [ ] `langgraph.json` 仅 `agent` + `scheduler` + `http.app` + Postgres checkpointer/store + TTL + `webhooks.disable_loopback=false` 可启动
- [ ] `pin_single_event_loop` 在 app import 期生效且可拒绝启动(缺 `BG_JOB_ISOLATED_LOOPS` 开关时 RuntimeError)
- [ ] **多用户登录**:`POST /auth/login` 两个不同 username 各自 200 拿各自的 `osw_session` cookie;两浏览器同时在线,A 的 `/threads` **看不到** B 的线程,B 访问 A 的 `/threads/{id}` → **404**;admin(`CONFIGURED_ADMINS`)`all=true` 可看全量
- [ ] token 校验:token 错 → 401;auth 未配置(无 token)→ 503 fail-closed;`X-OpenSWE-User`/`token:username` 复合 bearer 在协议面同样生效
- [ ] 沙箱 `SANDBOX_TYPE=docker`:每线程一个容器(容器名含 thread_id 前缀),run 结束容器不销毁;`docker exec` 里 `git commit` 的 **commit 作者 = 线程 owner 的 git 身份**(注册表 `git_name/git_email`,未配回退宿主 gitconfig 值),**不是**强制 bot 身份;`docker inspect` 的 env/挂载里 grep 不到任何 token
- [ ] 删线程时容器 best-effort 清理(`docker rm -f`);`docker info` 不可达时服务器启动即报 500 级错误(fail-closed)
- [ ] 一次 `agent` run 能:改文件 → `git commit` 到 `local/<slug>` 分支 → 汇报本地分支名,**不** push、**不**建 PR
- [ ] `/state`(单快照 + 15s 预算 + idle 缓存 + run-in-flight 删 `next`)与 `/history`(≤5 + 90s + 合并 + 退避 3×1.5s + `/state` 降级 + 30s 快照缓存)代理
- [ ] 超大线程(≥30MB checkpoint)下:打开线程页 P95 < 10s,期间其他线程请求不被冻结 > 1s(queue-probe 的 `lag_ms` 验证)
- [ ] run 终结经**进程内** POST 到达 `/webhooks/run-complete`(`?token=` 错误 → 401);全程无公网回调
- [ ] SSE 流:客户端断开 → 上游订阅被释放(不泄漏事件循环);晚连接带 `Last-Event-ID` 重放
- [ ] 进程 `env` 与运行时内存 grep 不到任何 GitHub/OAuth 凭据字段

---

## 14. HTTP 协议字段盘点

**14.1 认证与通用头**
- **协议面**(`:2024` 非 `/dashboard/api`):复合 bearer(§3.1.2)——`Bearer <OPEN_SWE_LOCAL_AUTH_TOKEN>` + `X-OpenSWE-User: <username>`(内部转发),或 `Bearer <token>:<username>`(浏览器/SDK 直连);身份 = username,无用户头 = `local-service`(接 `agent/local_auth.py`)。成功 → 注入 `langgraph_auth_user={"identity": <username>}`;失败 → **401** `{"detail":"Invalid bearer token"}`。
- **dashboard 面**:会话 cookie `osw_session`(§3.2 下发,`HttpOnly`、`SameSite=Lax`、`Path=/`)。成功 → `_SESSION_DEP` 注入 `{sub: <username>, name, email: null, is_admin}`;失败 → **401** `{"detail":"unauthenticated"}`(前端据此跳登录表单 `POST /auth/login`)。
- **CORS**(跨 origin 时):`Access-Control-Allow-Credentials: true` + 具体 origin 白名单(`DASHBOARD_ALLOWED_ORIGINS`;**禁止 `*`+credentials**,`create_app` 里 `RuntimeError` 强校验)。Origin 不在白名单 → 无 `Access-Control-Allow-Origin` 头(浏览器拦)。
- **CSRF**:非 GET 请求校验 `Origin` 白名单(§3.3);bearer-only 请求豁免。
- **全部 JSON 端点**:`Content-Type: application/json`(proxy 端点显式校验,非 JSON → **415**)。

**统一状态码语义(全端点适用)**:

| 码 | 含义 | body 约定 |
|---|---|---|
| **200** | 成功(含"幂等无变化") | 资源对象 / 列表 / `{status…}` |
| **201/202** | 建 run / 入队成功(异步) | `{"run_id",…}` |
| **204** | 成功无返回体(DELETE/logout) | — |
| **400** | 请求体/参数非法 | `{"detail":"<字段>: <原因>"}` |
| **401** | 未认证 / 凭证错 | `{"detail":"…"}` |
| **403** | 已认证但无权(非作者删评论等) | `{"detail":"forbidden"}` |
| **404** | 不存在**或**非 owner(归一化,不泄漏存在性) | `{"detail":"not found"}` |
| **409** | 冲突(busy、已终态再操作、技能重名、idle 走 messages) | `{"detail":"… conflict"}` |
| **415** | Content-Type 非 JSON | `{"detail":"unsupported media type"}` |
| **422** | 协议层校验失败(运行时 webhook 策略等) | `{"detail":"…"}` |
| **502** | 上游(运行时)不可达 | `{"detail":"runtime unreachable"}` |
| **503** | 有界读超时 / fail-closed(认证未配置、secret 未设) | `{"detail":"… timed out; retry"}` / `{"detail":"… not configured"}` |

**14.2 线程 metadata 契约(thread jsonb,业务状态的唯一载体)**

```jsonc
{
  "kind": "agent",                  // 本变体恒为 agent
  "local_user": "<建线程者 username>",   // owner 判据(替代 github_login)
  "participant_logins": ["<username>", …],
  "source": "dashboard|web|automation|schedule",
  "origin": "dashboard",
  "thread_category": "interactive",
  "trigger_kind": "user",
  "repo": {"owner"?, "name"},       // 或 repo_owner/repo_name;本地路径解析见 §11
  "title": str,
  "branch": str,                    // 线程稳定分支名(local/<slug>)
  "base_branch": "main",
  "status": "idle|running|error|interrupted",  // 展示态,由 latest_run_status 推导
  "latest_run_status": "pending|running|success|error|timeout|interrupted",
  "latest_run_id": str,
  "viewed": bool, "viewed_at_ms": int,
  "resolved": bool, "resolved_at_ms"?: int,
  "model": str, "effort"?: str, "resolved_model"?: str, "resolved_effort"?: str,
  "plan_mode": bool, "plan_status"?: "draft|pending|approved|rejected",
  "sandbox_id"?: str,               // 沙箱注册表键(仅进程内有效)
  "failure_ack_run_ids"?: [str],    // 完成 webhook 幂等去重
  "injected_dynamic_context_hashes"?: [str],
  "updated_at_ms": int
}
```

**14.3 `configurable` 契约(runs/commands 的 `config.configurable`)**

| 键 | 值 | 说明 |
|---|---|---|
| `thread_id` | uuid | 恒有 |
| `source` | `dashboard\|web\|automation\|schedule` | 决定 Surface 与提示分段 |
| `local_user` | `<当前登录 username>` | 替代 `github_login`;thread 归属/owner 判据(§3.4) |
| `repo` | `{"owner","name"}` | 可缺;`repo_explicitly_none` 显式无 repo |
| `agent_model_id` / `agent_effort` | str | 用户选模型(缺省走 profile/全局默认) |
| `plan_mode` | bool | plan mode 开关 |
| `prepare_run_id` | 新 uuid(每次 run) | PrepareRun 中间件关联 |
| `__event_streaming_v2` | `true` | **必须置位**(v3 流协议标记,镜像 langgraph_api 的 key,不 import) |
| `sandbox_id` | str | 复用线程沙箱 |

**14.4 SDK command 信封(`POST /threads/{id}/commands` 的 body)**

```jsonc
// 前端(@langchain/react)发送:
{ "id": "cmd-<uuid>", "method": "run.start" | "input.respond" | "input.inject" | "state.fork",
  "params": {
    "input"?: {"messages": [RunMessage]},
    "config"?: {"configurable": {…用户可选字段…}},
    "metadata"?: {…},
    "stream_mode"?: […], "stream_subgraphs"?: bool
  } }
// 服务端富化后透传;运行时响应:
{ "type": "success", "run_id"?: str } | { "type": "error", "error": {…} }
```

**14.5 SSE 事件流(GET/POST `…/stream*` 的响应)**
- `Content-Type: text/event-stream`;帧 `id: <event_id>\nevent: <type>\ndata: <json>\n\n`。
- 通道:`values`/`updates`/`messages`/`messages-tuple`/`tools`/`checkpoints`/`events`(v2 标记下另含 lifecycle 与子代理命名空间事件)。
- 断线重连:客户端带 `Last-Event-ID` 头(或 `last_event_id` query)→ 从该 event 重放(依赖 `stream_resumable=True`)。
- **成功**:HTTP 200 + `text/event-stream`,首响应头后立即推帧;run 正常结束 → 终止帧后**主动关闭**(客户端收到流结束,不是错误)。
- **失败**:
  - 认证/权限失败 → 建流**前** 401/404(普通 JSON,不进流)。
  - 流运行中上游 run 崩溃 → 流内 `error`/终止帧后关闭(HTTP 层仍 200)。
  - 代理侧上游不可达 → 502(建流前)。
  - **客户端断开**:服务端必须释放上游订阅(§5.2 SSE 流的 disconnect watcher)——这是**资源不变式**,违反即泄漏事件循环。
- **验收口径**:用 `langgraph-sdk` 的 `client.runs.stream` 与前端 `StreamProvider` 双端消费通过,不逐帧比对 schema。

---

## 15. 协议路由(`langgraph_api` 提供,不实现,只消费)

**不需要写的代码**:起 `langgraph dev` 即全部就位。列出 dashboard 侧实际消费的子集与契约,实现 dashboard 时按这些字段调。

**threads(核心)**

| 端点 | 功能 | 请求 / 成功响应 | 失败定义 |
|---|---|---|---|
| `POST /threads` | ✏️ 建线程记录(可指定 id) | `{"thread_id"?: uuid, "metadata": {…}}` → **200** thread 对象 | 400;id 冲突 → 409 |
| `GET /threads/{id}` | 📖 取线程记录 | → **200** `{thread_id, status, metadata, created_at, updated_at, state_updated_at?}` | 不存在 → **404** |
| `PATCH /threads/{id}` | ✏️ 合并更新 metadata(_merge 语义) | `{"metadata": {…}}` → **200** 更新后对象 | 404 |
| `DELETE /threads/{id}` | 🗑️ 删线程 + checkpoint/流 | → **200/204** | 404 |
| `POST /threads/search` | 🔍 搜线程(metadata 过滤+排序+分页) | `{"metadata": {…}, "limit", "offset", "sort_by", "sort_order"}` → **200** `[thread…]`(≤limit) | 400;无匹配 → 200 空数组 |
| `GET /threads/{id}/state` | 📖 **单快照读**(前端主 transcript 唯一来源) | → **200** ThreadState `{"values": {messages,…}, "next": […], "configurable": {…}, "metadata": {…}}` | 404;从未 run → 200 空快照 |
| `GET/POST /threads/{id}/history` | 📜 **有界历史**(发现/跳转,*禁止全量翻页*) | query/body `limit/before/after/filter` → **200** `[{checkpoint_id, parent_checkpoint_id, values, metadata, timestamp, next}, …]`(≤limit,最新在前) | 404;超大线程遍历慢 → 调用方(有界代理)超时降级,本端点不主动 503 |
| `GET /threads/{id}/stream` | 📡 **SSE join 线程流** | query `last_event_id`、`stream_mode` → **200** SSE | 404;上游 run 结束 → 终止帧正常关闭 |
| `POST /threads/{id}/stream/events` | 📡 **v2 事件流**(前端主口) | body 同 stream 参数 → **200** SSE | 同上 |

**runs(触发契约)**

| 端点 | 功能 | 成功 | 失败 |
|---|---|---|---|
| `POST /threads/{id}/runs` | ⚡ 建 run(durable 触发) | **200/202** `{"run_id","thread_id","status":"pending",…}`;`if_not_exists="create"` 自动建线程 | 404 线程不存在且未给 if_not_exists;422 webhook 策略;busy 按 `multitask_strategy`(interrupt → 打断旧 run 成功;reject → 409) |
| `POST /threads/{id}/runs/stream` | ⚡ 建 run + 直连 SSE | **200** SSE | 同上 |
| `POST /threads/{id}/runs/wait` | ⚡ 建 run 阻塞等终态 | **200** 终态结果 | 超时/失败在 body |
| `GET /threads/{id}/runs` | 📖 列 runs(新在前) | **200** `[run…]` | 404 |
| `GET /threads/{id}/runs/{run_id}` | 📖 查 run 状态 | **200** run 对象 | 404 |
| `DELETE /threads/{id}/runs/{run_id}` | 🗑️ 删 run 记录 | **200/204** | 404 |
| `POST /threads/{id}/runs/{run_id}/cancel` | 🛑 取消 run(置 cancelled,不删 checkpoint) | **200** run 对象(status=cancelled) | 404;已终态 → 409/400 |
| `GET /threads/{id}/runs/{run_id}/stream` | 📡 单 run SSE join(`last_event_id` 重放) | **200** SSE | 404;已结束无重放 → 立即终止帧 |
| `POST /threads/{id}/commands` | ⚡ SDK command 信封 | **200** `{"type":"success","run_id"?}` | `{"type":"error","error":{…}}`(业务错,200 体);404 线程不存在且 method≠run.start;400 信封非法 |
| `POST /runs/crons` 等 | ⏰ cron 面(scheduler 定时任务注册/查/改/删) | 建 **200** cron 对象;查/改/删 **200/204** | 400 非法 cron;404 |

`POST /threads/{id}/runs` 的 body 契约(复刻必须逐字段一致):
```jsonc
{
  "input": {"messages": [RunMessage], "files": {…?}},
  "config": {"configurable": {…见 §14.3…}, "recursion_limit"?},
  "metadata": {"kind": "agent", "source": "dashboard|web|automation|schedule",
               "local_user": "…", "repo": "…", "title": "…", "latest_run_id"? …},
  "multitask_strategy": "interrupt",
  "durability": "sync",
  "if_not_exists": "create",
  "stream_mode": ["values","updates","messages","custom","tasks","checkpoints"],
  "stream_subgraphs": true,
  "stream_resumable": true,
  "webhook": "/webhooks/run-complete?token=…",
  "after_seconds": null
}
```

**store(长期记忆,`AsyncPostgresStore`)**

| 端点 | 功能 | 成功 | 失败 |
|---|---|---|---|
| `PUT /store/items` | ✏️ 写长期记忆(plan、指令、skills、schedules、队列) | `{"namespace": [str…], "key": str, "value": {…}}` → **200** | 400 namespace 空 |
| `GET /store/items` | 📖 按 (namespace,key) 精确读 | query `namespace`、`key` → **200** item;无 → **404** | 404 |
| `DELETE /store/items` | 🗑️ 删 | **200/204** | 404 |
| `POST /store/items/search` | 🔍 按 namespace 前缀+分页搜 | `{"namespace"?: […], "prefix"?, "limit", "offset"}` → **200** `{items, …}` | 400 |
| `POST /store/namespaces` | 📖 列命名空间(调试) | **200** 列表 | — |

**其余协议端点**(`assistants/*`、`/mcp/`、`/a2a/*`、无线程形态 runs):**不用**,不消费、不管。

---

## 16. 主要文件索引(删 / 留 / 改)

| 状态 | 文件 |
|---|---|
| **改** | `langgraph.json`(两图 + `webhooks.disable_loopback=false`)、`agent/api/app.py`(auth 钩子 + 路由裁剪)、`agent/prompt.py`(本地提交段)、`agent/server.py`(沙箱去 proxy + 工具列表)、`agent/dispatch.py`(source 收窄 + loopback 反转)、`agent/completion.py`(失败回执简化)、`agent/scheduler.py`(去 baby_sit/cost)、`agent/dashboard/routes.py`(auth 换 token + 端点裁剪)、`agent/dashboard/thread_api.py`(`github_login`→`local_user`、去 GitHub token 校验) |
| **留** | `agent/webapp.py`、`agent/utils/event_loop.py`、`agent/postgres_persistence.py`、`agent/input_messages.py`、`agent/utils/sandbox.py`、`agent/integrations/local.py`、`agent/utils/sandbox_paths.py`、`agent/utils/queue_debug.py`、`agent/utils/thread_ops.py`、`agent/api/health.py`、`agent/dashboard/{thread_api,plan_api,plan_store,workflow_approval_api,schedules,skills,agent_instructions,user_instructions,options,profiles}.py`、`agent/middleware/{sanitize,tool_error,exclude_tools,model_call_limit,model_call_timeout,strip_thoughts,prepare_run,check_message_queue}.py`、`agent/tools/{web_search,fetch_url,http_request,save_plan,approve_plan,enter_plan_mode,schedule_thread_wakeup,background_execute,background_task,recreate_sandbox,sandbox_reset,report_platform_issue,read_user_settings,save_user_instructions,save_user_skill,delete_user_skill,output_iframe}.py` |
| **改语义** | `agent/local_auth.py`(从死代码 → 接上,改为**复合 bearer 多身份**:共享 token + `X-OpenSWE-User`/`token:username`)、`agent/utils/repo.py`(仅本地仓库解析) |
| **删** | `agent/{reviewer,analyzer,chat,baby_sit,session_cost}.py`、`agent/review/*`、`agent/graphs/{reviewer,analyzer,chat}.py`、`agent/utils/{github_app,github_checks,github_ci,github_comments,github_feedback,github_http,github_org_membership,github_proxy,github_token}.py`、`agent/middleware/{refresh_github_proxy,pr_creation_guard,workflow_push_guard,settle_review_check}.py`、`agent/webhooks/{slack,linear,github,common}.py`(留 run-complete)、`agent/dashboard/{oauth,github_token_auth,oidc_auth,slack_oauth,notion_oauth,user_mappings,review_api,review_styles,review_style_jobs,review_chat_api,pr_diff,pull_request_status,agent_usage,repo_cache,repo_snapshots,enabled_repos,team_credentials,team_settings,admin}.py`、`agent/tools/{open_pull_request,request_pr_review,manage_baby_sit,notify_automation_channel,fetch_review_diff,publish_review,add_finding,update_finding,list_findings,read_finding_outcomes,reply_to_finding_thread,resolve_finding_thread,save_review_style,list_review_findings,admin_gate,linear_*,slack_*}.py`、`agent/integrations/{langsmith,daytona,modal,runloop,e2b}.py`(5 种云 provider 全删;`local.py` 保留为裸开发回退)、`test_github_oauth.py` |
| **新增** | `agent/integrations/docker.py`(本地 Docker 沙箱 provider,§4.4)、`agent/dashboard/auth.py`(多用户登录/登出/`/me`/`/users`)、`agent/dashboard/user_registry.py`(本地用户注册表,store `local_users`) |


---

## 17. 实现顺序建议(给 AI 编程代理)

1. **锁骨架**:改 `langgraph.json` 两图 + `webhooks.disable_loopback=false`;`agent/api/app.py` 换 auth 钩子(§3 多用户)+ 裁剪路由;`agent/dashboard/auth.py` + `user_registry.py` 落地;确保空实现也能 `langgraph dev` 起 + `/health` 200 + 两个 username 能分别登录。
2. **打通 Docker 沙箱 + 本地提交**:新增 `agent/integrations/docker.py`(§4.4)+ `SANDBOX_FACTORIES` 加 `docker` 条目 + `SANDBOX_TYPE=docker`;`agent/server.py` 去 proxy 链;接 `LOCAL_COMMIT_SECTION` 提示;跑通一次"改文件 → 容器内 `git commit` 到 `local/<slug>`"。
3. **接持久化与有界代理**:Postgres checkpointer/store(§7)+ `thread_api.py` 有界 state/history(§5.2)。
4. **接完成 webhook**(§9.2)+ `scheduler` 裁剪(§4.2)。
5. **提示词收尾**(§10)+ env 清单(§12)。
6. **逐条过 §13 验收清单**,尤其"超大线程不冻结"与"内存无 GitHub 凭据"两条。

---

## 18. 两种部署拓扑(:2024 与 :8001,都要支持多用户并发登录)

本变体的 dashboard 服务器与 LangGraph 运行时可以是**一个进程**,也可以是**两个进程**。两种拓扑**共用同一套认证、权限、代理代码**(`agent/api/app.py` + `agent/dashboard/*`),多用户并发登录在两种拓扑下都成立:

### 18.1 拓扑 A:同进程(单部署)

```
langgraph dev (localhost:2024, 单事件循环)
├── inmem runtime(协议面 :2024)
├── FastAPI app(http.app 挂进同进程 → /dashboard/api/* 也在 :2024)
└── Postgres(checkpoint/store/用户注册表)
```
- 浏览器只连 `:2024`(`/dashboard/api/*` 与 SSE 协议面同端口,同源,**无 CORS 问题**)。
- 多用户并发:会话 cookie 按用户签名(§3.1),浏览器 A/B 各持各的 cookie,同进程内天然并发;`state_cache`、`_HISTORY_COALESCING` 等进程内缓存按 thread_id 键,跨用户不串。

### 18.2 拓扑 B:双进程(推荐,对应现网 `make dashboard`)

```
langgraph dev (localhost:2024)            只跑 runtime + 图
        ▲  Authorization: Bearer <token> + X-OpenSWE-User: <sub>
        │  (进程间 bearer,§3.1.2 形态 A)
uvicorn agent.webapp:app --port 8001      只跑 dashboard FastAPI app
        ▲  osw_session cookie(§3.2)
        │
浏览器 :3000(VITE_DASHBOARD_API_BASE_URL=http://localhost:8001/dashboard/api)
```
- dashboard 进程(`:8001`)面向浏览器,用**会话 cookie** 认证(`_SESSION_DEP`)。
- dashboard 进程内部对运行时的**所有**调用(`langgraph_client()` = `get_client(url=LANGGRAPH_URL)`,缺省 `http://localhost:2024`;httpx 代理同理)**统一注入** `Authorization: Bearer <OPEN_SWE_LOCAL_AUTH_TOKEN>` + `X-OpenSWE-User: <当前会话 sub>`(§5.1)→ 运行时 `agent/local_auth.py` 据此以**该用户身份**执行(§3.1.2 形态 A)。
- **浏览器不直连 `:2024` 协议面**:所有 transcript/流/历史读取都走 `:8001` 的代理端点(§5.2 的 `/state`、`/history`、`/stream/events`、`/commands`),由 dashboard 进程带用户身份转发。
- **多用户并发**:两进程都无状态地验签同一 `DASHBOARD_JWT_SECRET`(§3.2)→ 两个浏览器各持各的签名 cookie,并发登录/并发 run 互不影响;用户注册表、线程、store 都在**共享 Postgres** 里,跨进程一致。
- CORS 配在 `:8001` 的 dashboard app 上(`DASHBOARD_ALLOWED_ORIGINS=http://localhost:3000`);`:2024` 协议面只接受进程间 bearer(无浏览器 cookie)。
- 启动:`make dev`(`uv run langgraph dev`)+ `make dashboard`(`uv run uvicorn agent.webapp:app --port 8001`)。双进程下 `agent/api/app.py` import 期的 `pin_single_event_loop()` 只约束**各自进程**内的单循环(铁律不变,作用域是进程)。

### 18.3 多用户并发验收(两种拓扑各过一遍)

- [ ] 两个浏览器分别 `POST /auth/login`(不同 username)→ 各自 200 + 各自 cookie。
- [ ] 各自建线程、发 run,同时执行:互不阻塞,transcript 各自只见本人线程(§3.4)。
- [ ] A 用 B 的 thread id 直连 `:8001/dashboard/api/threads/{id}` → 404;直连 `:2024` 协议面但 `X-OpenSWE-User` 不匹配 → 运行时侧同样 404 归一化。
- [ ] 拓扑 B 下 dashboard 进程 kill 掉,`:2024` runtime 仍存活,run 继续跑(loopback webhook 在 runtime 进程内,§9.2 不受 dashboard 进程影响)。

---

## 附录 A:前端契约

- 前端 hydrate 用 `GET /state`(主 transcript)+ 有界 history 发现页,`VITE_DASHBOARD_API_BASE_URL` 指向 dashboard 服务器的 `/dashboard/api`(同进程部署为 `:2024`,双进程部署为 `:8001`,§18)。
- **删除**所有 PR/review/OAuth 页与组件,只留 Agents 线程列表 + 聊天 + plan 审批。
- **多用户登录页**:表单收集 `username` + `token` → `POST /dashboard/api/auth/login` → 成功 `Set-Cookie: osw_session` 后跳回 Agents 列表;`GET /me` 的 `is_admin` 决定是否显示 admin 菜单;`POST /auth/logout` 清 cookie。
- **多用户隔离**:线程列表/详情只返回本人线程(后端按 `local_user` 过滤);admin 可看 `all=true` 全量。前端不自行拼身份,一切以 cookie 会话为准。
- SSE 用 `@langchain/react` 的 `StreamProvider`/`StreamController`(v2 事件流 `POST …/stream/events`);stop 按钮调 `POST …/cancel`。
