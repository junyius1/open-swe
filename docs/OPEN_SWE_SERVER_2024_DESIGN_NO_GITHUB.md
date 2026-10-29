# Open SWE「2024 服务器」本地无 GitHub 版 程序设计书

> 目标读者:AI 编码代理 / 熟悉 LangGraph 的工程师。
> 本文件是 `docs/OPEN_SWE_SERVER_2024_DESIGN.md`(下称**基础设计书**)的**裁剪变体**:拿掉 GitHub 登录、GitHub 授权(凭据)与「提交 PR」三块能力,把交付物收敛为**只在本地 git 分支上提交**(可选 push 到用户自备远端)的自托管编程 agent 服务器。
> **未覆盖的架构一律沿用基础设计书**:单事件循环铁律、五个图(本变体保留 `agent` + `scheduler`)、`deepagents` 共同底座、XML 输入信封、`create_durable_run` 触发契约、Postgres checkpoint/store 与 DeltaChannel messages、完成 webhook、有界 state/history 代理、验收不变式——这些**逐字不变**,下文只标注差异。
> 依据:仓库 `local` 分支真实代码;所有要删/改/新增的模块路径、函数、env、SQL 均可逐条验证。

---

## 0. 裁剪目标与边界

**保留**:本地 agent 服务器(`langgraph dev`,inmem 运行时,`:2024`)+ 每线程沙箱 + dashboard Web 业务层 + 本地 git 工作流。
**移除**:
1. **GitHub 登录/身份**:GitHub OAuth、`gh` CLI 代理鉴权、GitHub App 凭据、OIDC/Bearer 管理会话。
2. **提交 PR**:`open_pull_request` 工具、`pr_creation_guard` / `workflow_push_guard` 中间件、`COMMIT_PR_SECTION` 系统提示、reviewer 的 PR 审查闭环。
3. **GitHub 授权(凭据面)**:`GITHUB_APP_*`、用户 OAuth token 库、repo 级 App token、`refresh_github_proxy` 中间件、`_configure_github_proxy`。

**新增(替代交付物)**:agent 在**本地 Docker 沙箱**(每线程一个容器)内 `git commit` 到线程稳定分支;远端 push **可选**且**纯本地凭据**(宿主自备 credential),服务器**不持有任何 GitHub/云沙箱 token**。

**明确不在本变体范围**(从上游一起拿掉,因为它们强依赖 GitHub/PR):`reviewer` 图、`analyzer` 图、`chat`(review chat)图、PR CI 监视(`/baby-sit` + 其 scheduler 任务)、Linear/Slack 集成与对应 webhook、`fetch_review_diff` / `publish_review` 等 review 工具。保留范围见 §6 工具表。

---

## 1. 与基础设计书的逐条差异(总表)

| 域 | 基础版 | 本变体 | 落点(代码) |
|---|---|---|---|
| 身份/登录 | GitHub OAuth(`oauth.py`)、`github_token_auth.py`、`oidc_auth.py`、`slack_oauth.py`、`notion_oauth.py`;`agent/local_auth.py` 是未被引用的死代码 | **单一静态 token 认证**(`OPEN_SWE_LOCAL_AUTH_TOKEN`)或**开放本地模式** | `agent/dashboard/auth.py`(新增)、`agent/api/app.py` 依赖注入 |
| 注册图 | 5 个:agent/reviewer/analyzer/chat/scheduler | **2 个:agent + scheduler** | `langgraph.json`、`agent/graphs/*` |
| 沙箱 | 6 种 provider(langsmith/daytona/modal/runloop/e2b/local)+ 每沙箱注入 GitHub App token 作 `gh` 代理 | **删掉 5 种云 provider;唯一 provider = 新增本地 `docker`**(`SANDBOX_TYPE=docker`,每线程一个容器,无任何 token);`local` 仅裸开发回退 | `agent/integrations/docker.py`(新增)、`agent/utils/sandbox.py`、`agent/server.py`(去 proxy 链) |
| 提交交付 | `open_pull_request` 工具 + `COMMIT_PR_SECTION` 提示 + 两个 PR 守卫中间件 | **仅本地分支 commit**;push 可选;新写 `LOCAL_COMMIT_SECTION` 提示 | `agent/server.py` 工具列表、`agent/prompt.py` |
| 系统提示 | `COMMIT_PR_SECTION`/`DESKTOP_PR_SECTION`、协作署名(PR footer)、PR 规则 | 删除全部 PR 段落;`REPO_SETUP_SECTION`/`TASK_EXECUTION_SECTION` 改写为本地提交语义 | `agent/prompt.py` |
| Webhook 触发 | Slack/Linear/GitHub 三个 + run-complete | **只保留 run-complete** | `agent/webhooks/*`、`agent/api/app.py` |
| 中间件 | 含 `refresh_github_proxy`、`pr_creation_guard`、`workflow_push_guard`、`settle_review_check` | 删除 GitHub/PR 相关;保留公共件 | `agent/middleware/*` |
| 完成 webhook | 失败回复按 `kind` 走 Slack/PR 等渠道 | 简化:仅按 thread 落一个幂等失败回执(本地/dashboard 可查) | `agent/completion.py` |
| Dashboard 端点 | 117 个,含大量 PR/review/OAuth | 裁剪到 **线程/消息/state-history/plan/workflow/health** 一组 | `agent/dashboard/routes.py` 等 |
| env | 大量 `GITHUB_*`、`SLACK_*`、`LINEAR_*` | 只保留:`DATABASE_URI`、模型 key、`COMPLETION_WEBHOOK_URL`/`RUN_COMPLETE_WEBHOOK_SECRET`、`OPEN_SWE_LOCAL_AUTH_TOKEN`、`SANDBOX_TYPE=docker`、`DOCKER_SANDBOX_IMAGE` | `.env` |

---

## 2. 身份与登录(替换 GitHub OAuth)

**目标**:dashboard 的会话认证不再依赖 GitHub,用一个最小、可本地验证的认证面。

### 2.1 认证方式(二选一,默认 A)
- **A. 静态 token(推荐,零外部依赖)**:服务端持有 `OPEN_SWE_LOCAL_AUTH_TOKEN`(secret)。前端/SDK 以 `Authorization: Bearer <token>` 调 `/dashboard/api/*` 与 LangGraph 协议面。服务端用 `hmac.compare_digest` 比对(防时序),身份固定为 `local-user`。
  - 直接**复活并复用**现成的 `agent/local_auth.py`(它已是这个语义:`Auth.authenticate` + `OPEN_SWE_LOCAL_AUTH_TOKEN` + `local-user` 身份),在 `create_app()` 里把它接到 LangGraph 的 auth 钩子上;dashboard 侧用同一个 env 做 `_SESSION_DEP` 的替代。
- **B. 开放本地模式**:当 `OPEN_SWE_LOCAL_AUTH_TOKEN` 未设置且 `OPEN_SWE_ALLOW_ANON=1` 时放行,身份 = `anon`。**仅用于裸开发**,启动时打印显著告警;默认必须设置 token。

### 2.2 删除的登录模块(全部不要)
`agent/dashboard/oauth.py` 的 GitHub 流程(`GITHUB_AUTHORIZE_URL`、PKCE `state/nonce`、`issue_session`/`decode_session` 的 GitHub 依赖)、`github_token_auth.py`、`oidc_auth.py`、`slack_oauth.py`、`notion_oauth.py`、`user_mappings.py` 的 GitHub 映射、`profiles.py` 的 access-token 存取(原本给 `open_pull_request` 取用户 token 用)。

### 2.3 权限模型
thread 级 owner 校验(`_assert_thread_owner/_assert_thread_readable`)**保留机制但改判据**:owner = 固定本地身份(或 thread metadata 里 `github_login` 字段改名为 `local_user` 并恒等于当前身份),即**单用户**语义——本地部署只有一个操作者,所有线程对他可见可改。多用户隔离不在本变体范围。

### 2.4 沙箱内 git 身份(与 GitHub 解耦,经 Docker 容器注入)
`configure_git_identity` 现在写死 bot 身份(`OPEN_SWE_BOT_NAME/EMAIL`)。本变体:沙箱 = **每线程一个本地 Docker 容器**(`SANDBOX_TYPE=docker`,§4.1),容器启动时挂载该线程的 `.gitconfig-sandbox` 并设 `GIT_CONFIG_GLOBAL` 指向它(机制同 `local.py` 的 `_scoped_git_config_env`);**commit 作者 = 宿主 `~/.gitconfig` 里的 `user.name`/`user.email` 值**(序列化写进容器内 `.gitconfig-sandbox`,不做 `[include]` 宿主文件),即 commit 作者 = 操作用户自己的 git 身份。删除 `_configure_git_identity` 的强制 bot 写入;`git config user.name/email` 命令(基础书 §sender-context 里那一步)**整段删除**。

---

## 3. 服务器配置(`langgraph.json`,裁剪后)

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
  "webhooks": { "url": { "disable_loopback": false } },  // 完成 webhook 走进程内 loopback(§8.1)
  "env": ".env"
}
```

- 删掉 `reviewer/analyzer/chat` 三行;`agent/graphs/reviewer.py`、`analyzer.py`、`chat.py` 三个 shim 文件及 `agent/reviewer.py`/`analyzer.py`/`chat.py` 工厂**整体删除**。
- checkpointer TTL、store、`http.app` 与基础书**一致**,不动。
- 无 `DATABASE_URI` 时仍降级 `InMemorySaver/InMemoryStore`(基础书 §7.3 不变)。

---

## 4. 图工厂

### 4.1 保留:`agent`(主编程 agent)
- 后端:**每线程一个本地 Docker 沙箱**(§4.1.1)。`ensure_sandbox_for_thread(thread_id, repo, ...)` 保留签名,但**去掉** `github_proxy_token` / `github_proxy_repositories` / `base_proxy_config` 参数与 `_resolve_proxy_token`、`_create_sandbox_with_proxy`、`_refresh_github_proxy`、`record_proxy_token_expiry` 整条链——直接调 `create_sandbox()`(此时 `SANDBOX_TYPE=docker`)。
- 沙箱缓存/复用/`get_cached_sandbox_backend` 不变(基础书 §3 单循环铁律依旧要求线程级注册表 + 循环亲和)。
- 子代理:`general-purpose`(完整沙箱工具)保留;`browser` 可选,保留与否随意(不依赖 GitHub)。
- plan mode / skills / repo instructions 保留;Linear 工单上下文**删除**。
- 工具列表见 §6。

### 4.1.1 本地 Docker 沙箱 provider(新增 `agent/integrations/docker.py`)

**目标**:替代全部 5 种云 provider(langsmith/daytona/modal/runloop/e2b 全删),唯一 provider = `docker`,每线程一个容器,跑在宿主 Docker daemon 上;不注入任何云/GitHub token。

- **接线**:`SANDBOX_FACTORIES`(纯 dict)加 `"docker": ("agent.integrations.docker", "create_docker_sandbox")`,删 5 个云条目与 `integrations/{langsmith,daytona,modal,runloop,e2b}.py`;`SANDBOX_TYPE` 缺省从 `langsmith` 改 **`docker`**;`validate_sandbox_startup_config()` 加 docker 分支(daemon 可达 + 镜像可解析,失败即拒启动)。
- **镜像**:`DOCKER_SANDBOX_IMAGE`(缺省 `python:3.12-slim`,需自带 git)。
- **创建/复用**:容器名 `oswe-sbx-<thread_id 前 12 位>`;已存在且存活 → 复用;已删除 → `SandboxGoneError`(调用方重建);不存在 → `docker create`:`-v <宿主仓库绝对路径>:/workspace/<repo>`(bind mount 共享 git 对象库)、`-v <线程 .gitconfig-sandbox>:/workspace/.gitconfig-sandbox:ro`、`GIT_CONFIG_GLOBAL=/workspace/.gitconfig-sandbox`、`--network=bridge`(可 `none` 断网);**不挂** Docker socket、**不注入**任何 API key。
- **命令执行**:`docker exec -i -w /workspace/<repo> <容器> sh -lc <cmd>`,流式输出,超时杀 exec 进程不杀容器。
- **生命周期**:run 结束不销毁(线程复用);`DELETE /threads/{id}` 时 best-effort `docker rm -f`;`recreate_sandbox` = rm -f + 重建。
- **`local` provider 保留**为无 Docker 环境的裸开发回退,不删文件。

### 4.2 保留:`scheduler`(cron 扇出)
`StateGraph` 单节点 `launch` 保留,但 `task` 分派**裁剪**:
- **保留**:`reconcile`(`reconcile_stale_runs()`)、`background_tasks`(`monitor_background_tasks(thread_id)`)、默认 `launch_scheduled_agent_run(schedule_id)`。
- **删除**:`baby_sit`(`evaluate_watch`,CI 监视,强依赖 GitHub 检查)、`session_cost`(按 Slack 线程刷成本,依赖 Slack)。
- `agent/scheduler.py` 里对应的 import 与分支删除;`agent/baby_sit.py`、`agent/session_cost.py` 删除。

### 4.3 删除的图
`reviewer` / `analyzer` / `chat` 全删(理由见 §0)。随之删除:`agent/review/*`、`agent/chat.py`、`agent/analyzer.py`、`agent/reviewer.py`、`agent/review_style_*`、`agent/dashboard/review_api.py`、`review_styles.py`、`review_chat_api.py`、`pr_diff.py`、`pull_request_status.py`、`repo_cache.py`、`repo_snapshots.py`、`enabled_repos.py`、`agent_usage.py`(PR 用量)。

---

## 5. 输入契约与触发契约

**XML 输入信封(§5 基础书)与 `create_durable_run`(§6 基础书)逐字保留**:`multitask_strategy="interrupt"`、`durability="sync"`、`if_not_exists="create"`、`V2_RUN_STREAM_MODES`、`stream_subgraphs=True`、`stream_resumable=True`、`configurable.__event_streaming_v2=True`、`prepare_run_id`、`webhook=COMPLETION_WEBHOOK_URL?token=…` 全部不变。

差异:
- `dispatch_agent_run` 的 `source` 只可能是 `web`/`desktop`/`automation`(Slack/Linear/GitHub 不再作为触发源)。`_dispatch_input` 里抽 Slack user/channel/时区、GitHub login、Linear email 的逻辑**删除**,发送者身份退化为本地身份(§2)。
- `build_run_input` 的 people/channels/systems 注入保留框架,但 `PersonIdentity` 不再来自 GitHub/Slack 映射;dashboard 来源直接给一个 `local-user` 的 `<dynamic-context>`。
- **触发 webhook(slack/linear/github)全部删除**——它们本来就是外部平台 POST 进来的,本地没有外部平台。本地触发源只有 dashboard 表单(同步 `run.start`)与 `scheduler`(cron),两者都**不经过任何 webhook**。
- 完成 webhook 的投递方式**改为进程内 loopback**(见 §8):上游"相对/loopback URL 会 422、必须 absolute https"的约束,在本地版通过官方策略开关 `webhooks.url.disable_loopback=false` **合法解除**;`dispatch.py` 的 fail-closed 逻辑相应改写。

---

## 6. 工具集(裁剪)

**删除(依赖 GitHub/PR/review/Slack/Linear)**:
`open_pull_request`、`request_pr_review`、`manage_baby_sit`、`notify_automation_channel`(Slack 通知)、`fetch_review_diff`、`publish_review`、`add_finding`、`update_finding`、`list_findings`、`read_finding_outcomes`、`reply_to_finding_thread`、`resolve_finding_thread`、`save_review_style`、`list_review_findings`、全部 `linear_*`、全部 `slack_*`、`admin_gate` + `ADMIN_TOOLS`。

**保留(核心编程 + 本地交付)**:
- 文件系统(deepagents 内置):`read_file`、`write_file`、`edit_file`、`delete`、`glob`、`ls`、`grep`、`task`(子代理)、`execute`(shell,含 `git`)。
- 网络/检索:`http_request`、`fetch_url`、`web_search`。
- plan:`enter_plan_mode`、`save_plan`、`approve_plan`。
- 本地会话/技能:`save_user_instructions`、`save_user_skill`、`delete_user_skill`、`schedule_thread_wakeup`、`background_execute`、`background_task`、`recreate_sandbox`、`sandbox_reset`、`report_platform_issue`、`read_user_settings`、`read_repo_file`、`search_repo_code`、`output_iframe`。

> 说明:`execute` 工具里跑 `git add/commit/branch/push` 即可满足"提交本地 git 分支"。push 是否需要凭据由宿主机 `~/.gitconfig`/credential helper 决定,**服务器不注入 token**。`read_repo_file`/`search_repo_code` 若当前实现走 GitHub API,则改为本变体的**本地沙箱内读取**(或直接删除,交给 `read_file`/`grep`);不要保留任何 GitHub 访问路径。

---

## 7. 持久化与数据模型

**逐字沿用基础设计书 §7**:checkpoint 三表 + DeltaChannel messages 存储与读成本、`_load_blobs` 同步重活、**有界读不变式**(state 单快照 / history ≤5)、thread `metadata` jsonb 业务状态、store 长期记忆、`DATABASE_URI` 缺失降级。

唯一改动:thread metadata 里**删除** `github_login` 相关字段,owner 判据改为 §2.3 的本地身份;`kind` 只会出现 `agent`(不再有 `review_chat`/`reviewer`/`thread_wakeup` 之外的 PR 相关 kind)。

---

## 8. 完成信号

`agent/completion.py` + `/webhooks/run-complete` 的**幂等骨架保留**(`verify_run_complete_token` fail-closed、按 `run_id` 去重、按 `kind` 收尾)。

### 8.1 投递方式:进程内 loopback(本地版关键差异)
上游部署里 `COMPLETION_WEBHOOK_URL` 必须是**公网 absolute https URL**——因为 inmem 运行时(`langgraph_api/webhook.py`)**默认** `disable_loopback=true`,相对/localhost webhook 会在 `runs.create` 时 422(防 auth-bypass,GHSA-q3v5-r5ch-p57j:loopback 走进程内 ASGI transport,挂载在 `root_path="/noauth"`,绕会话认证)。

本地版**不需要公网 URL**,走官方策略开关:
```jsonc
// langgraph.json 追加
"webhooks": { "url": { "disable_loopback": false } }
```
(CLI 启动时序列化为 `LANGGRAPH_WEBHOOKS` env,`config/__init__.py` 的 `WEBHOOKS_CONFIG` 读取。)开启后,**相对 URL 经进程内 loopback client 直接 POST 到本进程的同一 FastAPI app**——不走网络、不暴露端口、无 https 要求。

配套改动:
- `COMPLETION_WEBHOOK_URL=/webhooks/run-complete`(改回**相对路径**;secret 仍必须设置,拼 `?token=`)。
- `dispatch.py` 的 `_is_loopback_webhook` 降级逻辑**反转**:loopback 策略已开启时,相对 URL **正常挂**;secret 未设时仍不挂(路由本身 fail-closed 503)。
- **token 校验不可省**:loopback 投递恰好绕过会话认证,`verify_run_complete_token` 是这条路由唯一的门。
- 验收:run 终结后,`/webhooks/run-complete` 收到进程内 POST 且 `?token=` 正确;用错误 token 调同路由得 401/403。

差异(业务层):
- `success` → 不再入队 Slack 成本刷新(§4.2 已删 `session_cost`),改为无操作或仅记日志。
- 终态失败 → `_post_failure_reply` 简化:不往 Slack/PR 发,改为**落一条 thread 级失败标记**到 thread metadata(store),dashboard 线程列表可见。`_settle_failed_reviewer_check` 删除(reviewer 没了)。

---

## 9. Dashboard 业务层(裁剪)

### 9.1 应用组装(保留机制)
`create_app()`:lifespan `pin_single_event_loop()`(二次)→ `validate_sandbox_startup_config()`(此时校验 `SANDBOX_TYPE=docker`:daemon 可达 + 镜像可解析)→ `validate_local_dev_llm_config()`;退出 `close_cached_models()`。`QueueDebugMiddleware` + `enable_from_env()`(`OPEN_SWE_QUEUE_DEBUG=1`)保留。**auth 钩子换成 §2 的静态 token / 开放本地**。CORS 保留。

### 9.2 路由(保留/删除)
| 保留 | 删除 |
|---|---|
| 线程 CRUD、消息、**有界 state/history 代理**(`thread_api.py`,clamp 数值全照基础书 §9.2) | `routes.py` 里所有 PR/review/OAuth/notion/slack/linear 端点 |
| plan(`plan_api.py` / `plan_store.py`) | `oauth.py` 登录流、`github_token_auth.py`、`oidc_auth.py`、`slack_oauth.py`、`notion_oauth.py` |
| workflow approval(`workflow_approval_api.py`) | `review_api.py`、`review_styles.py`、`review_style_jobs.py`、`review_chat_api.py`、`pr_diff.py`、`pull_request_status.py` |
| health(`api/health.py`、queue-probe) | `agent_usage.py`(PR 用量)、`repo_cache.py`、`repo_snapshots.py`、`enabled_repos.py`、`user_mappings.py` 的 GitHub 映射 |
| `health_router`、`run-complete` webhook | `slack_webhook_router`、`linear_webhook_router`、`github_webhook_router` |

`state/history` 代理的**有界 + 合并 + 退避三层防御**(90s/15s/30s 超时、`_DISCOVERY_HISTORY_LIMIT=5`、`_FALLBACK_ATTEMPTS=3`、30s 快照缓存、32 合并)**逐字保留**——这是单循环性能事故的防线,与 GitHub 无关。逐端点的请求/响应契约见 §15.2-B。

### 9.3 前端契约
前端 hydrate 仍用 `GET /state`(主 transcript)+ 有界 history 发现页,`VITE_DASHBOARD_API_BASE_URL` 指向 `:2024` 的 `/dashboard/api`;**删除**所有 PR/review/OAuth 页与组件,只留 Agents 线程列表 + 聊天 + plan 审批。

---

## 10. 系统提示(改写 PR 交付为本地提交)

`agent/prompt.py` 的装配框架(`construct_system_prompt` 分段拼接、`<system-instructions>` 包裹、幂等 splice)**保留**。逐段改动:

| 段 | 处理 |
|---|---|
| `WORKING_ENV_SECTION` | 保留(工作目录说明) |
| `REPO_SETUP_SECTION` | **改写**:删除 "Set the commit identity" 那步(§2.4)、删除 PR 相关措辞;保留 clone/同步/建分支/读 `AGENTS.md` |
| `TASK_EXECUTION_SECTION` | **改写**:把 "commit, push, and (when a PR is warranted) open/update the draft PR" 改为 **"commit 到线程稳定分支;push 仅当用户明确要求且宿主机已有该远端凭据;不创建 PR"** |
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

## 10.5 LLM 调用并发控制(队列模式 / 并发模式)

**背景**:本地 vLLM 常以 `--max-num-seqs 1` 启动(同时只处理 1 个序列)。此时多个线程/用户的 run **不能并发向 LLM 发请求**——直接并发会触发 vLLM 429/排队/报错。服务器层提供**两种可切换模式**(env 切换,默认队列):

| 模式 | `OPEN_SWE_LLM_MAX_CONCURRENCY` | 语义 |
|---|---|---|
| **队列(串行)** | `1`(默认) | 全进程同一时刻**最多 1 个**进行中的 LLM 调用;其余调用在**进程内队列**里按 FIFO 等待,轮到才发出。适配 `--max-num-seqs 1` 的 vLLM。 |
| **并发** | `N ≥ 2`(或读 vLLM `/v1/loads` 的 `num_seqs` 上限自动定) | 全进程**最多 N 个**并发 LLM 调用。适配更高并发的本地/云端后端。 |

**实现落点**(`agent/utils/llm_gate.py`,新增):
- `make_model()`(`agent/utils/model.py`)返回的 chat model 外面包一层 **gate**:模块级 `asyncio.Semaphore(OPEN_SWE_LLM_MAX_CONCURRENCY)`(缺省 1)。所有 `local:`/任意 provider 的 model call 过同一个 gate(单用户本地部署 = 一个 LLM 后端;若多 base_url 可按 base_url 分 gate)。
- **gate 挂在哪一层**:`agent/middleware/` 现有 `ModelCallTimeoutMiddleware.awrap_model_call` 的**外层**再包一层 `LLMGateMiddleware`(`awrap_model_call = async with gate: await handler(request)`)。位置在 timeout 之外 = **等待 gate 的时间不算模型调用超时**(排队 5 分钟不触发 `OPEN_SWE_MODEL_CALL_TIMEOUT_SECONDS` 的误杀);拿到 gate 后才开始计 timeout。
- **为什么进程内 semaphore 就够**:§1 单事件循环铁律保证所有 model call 都跑在同一个 loop 上,`asyncio.Semaphore` 天然是全进程唯一的 FIFO 闸;不需要 Redis/外部队列。多进程部署(dashboard 独立进程)下 dashboard 进程不直接调 LLM,无冲突。
- **队列语义(FIFO)**:semaphore 的等待者按获得顺序放行;每个等待者持有 run 级上下文(thread_id),放行时 log `llm-gate: thread=… 获得槽位(等待 X s)`。
- **与现有机制的关系(不冲突)**:
  - 同一线程的 follow-up 消息仍走 store 队列(`check_message_queue` 中间件,run 内注入)——那是**线程内**语义;本节是**跨线程**的 LLM 层排队。
  - `multitask_strategy="interrupt"` 不变:新 run 可以打断旧 run,但被挤掉的 LLM 调用释放 gate,新 run 的调用接着排队。
  - SSE/前端:run 状态仍是 `running`(LLM 在 gate 里排队时不引入新状态);前端可加"等待 LLM"提示(读 §观测端点)。
- **观测端点**:`GET /debug/llm-gate`(加进 `agent/api/health.py` 的 router,§9 保留组)→ `{"mode": "queue"|"concurrent", "max_concurrency": int, "in_flight": int, "waiting": [ {thread_id, queued_at_ms} ]}`。
- **验收**:
  - [ ] `OPEN_SWE_LLM_MAX_CONCURRENCY=1`:两个线程同时跑 run,`/debug/llm-gate` 显示 `in_flight ≤ 1`;vLLM 侧无 429;第二个线程的 LLM 调用在第一个结束后开始(FIFO,`queued_at` 顺序)。
  - [ ] `=2`:两个线程可真正并发(vLLM `num_seqs` 同时为 2),第三个等前两个之一释放。
  - [ ] 排队时间不计入 `OPEN_SWE_MODEL_CALL_TIMEOUT_SECONDS`(排队 120s 后调用正常完成,不误报超时)。

---

## 11. 环境变量清单(最终)

```
# 持久化
DATABASE_URI=postgres://…            # 缺失则 InMemory 降级 + 告警
# 模型(多 provider 任选其一,make_model 解析)
ANTHROPIC_API_KEY=…                  # 或 OPENAI_*/FIREWORKS_*/gateway
# 完成 webhook(相对路径 → 进程内 loopback 投递,§8.1;token 必填)
COMPLETION_WEBHOOK_URL=/webhooks/run-complete
RUN_COMPLETE_WEBHOOK_SECRET=…
# 认证(§2)
OPEN_SWE_LOCAL_AUTH_TOKEN=…          # 未设则要求 OPEN_SWE_ALLOW_ANON=1
# 沙箱(§2.4 / §4.1.1,唯一 provider = 本地 Docker)
SANDBOX_TYPE=docker
DOCKER_SANDBOX_IMAGE=python:3.12-slim   # 需自带 git
# 裸开发回退(无 Docker):SANDBOX_TYPE=local + LOCAL_SANDBOX_ROOT_DIR=/path/to/work
# LLM 并发控制(§10.5)
OPEN_SWE_LLM_MAX_CONCURRENCY=1   # 1=队列(串行,适配 vLLM --max-num-seqs 1);≥2=并发
# 可选调试
OPEN_SWE_QUEUE_DEBUG=1
```

**删除的 env**:`GITHUB_APP_ID`、`GITHUB_APP_PRIVATE_KEY`、`GITHUB_APP_INSTALLATION_ID`、`GITHUB_TOKEN`、`SLACK_*`、`LINEAR_*`、`DEFAULT_REPO_OWNER`(可选保留,用于解析 `repo:` 语法)、所有 OAuth client/secret、`E2B_API_KEY`/`E2B_TEMPLATE`、`DAYTONA_API_KEY`/`DAYTONA_SANDBOX_SNAPSHOT`、Modal SDK 凭据、`RUNLOOP_API_KEY`、`LANGSMITH_*` 沙箱 token(仅当仍用 LangSmith 追踪时保留,与凭据无关)。

---

## 12. 关键不变式(沿基础书 + 本变体追加)

沿用基础书 §11 全部 6 条(单循环 / 消息只增且写 checkpoint 前剥离 thinking / 所有 run 走 `create_durable_run` / 面向用户的 checkpoint 读有界 / 失败幂等 fail-closed / TTL 12h 删除)。追加:

7. **进程内与运行时不持有任何 GitHub/Slack/Linear/云沙箱(E2B/Daytona/Modal/Runloop)凭据**;`gh` CLI 在本变体**不代理、不要求登录**——沙箱 = 本地 Docker 容器,git 身份经容器内挂载的 `.gitconfig-sandbox` 注入,容器内无宿主 `~/.gitconfig`、无任何 token。
8. **交付物 = 本地分支 commit**;push/PR 永远不作为默认行为,仅用户显式要求 + 宿主凭据可用时才 push。
9.5 **LLM 调用有界**:全进程同一时刻进行中的 LLM 调用 ≤ `OPEN_SWE_LLM_MAX_CONCURRENCY`;等待在进程内 FIFO 队列,排队时间不计模型超时。
10. **认证面单一**:静态 token(`hmac.compare_digest`)或显式开放本地;不存在 GitHub/OIDC/Slack 任何登录路径。

**最小复刻验收(本变体)**:
- [ ] `langgraph.json` 仅 `agent` + `scheduler` + `http.app` + Postgres checkpointer/store + TTL 可启动
- [ ] `pin_single_event_loop` 在 import 期生效且可拒绝启动
- [ ] `dispatch_agent_run` 六参数契约 + 完成 webhook 幂等(**无** GitHub 触发源);`disable_loopback=false` 下 run 终结经**进程内** POST 到达 `/webhooks/run-complete`(`?token=` 错误 → 401/403),全程无公网回调
- [ ] 静态 token 认证:带 token 200 / 缺 token 401;开放本地模式仅在 `OPEN_SWE_ALLOW_ANON=1` 生效
- [ ] 沙箱 `SANDBOX_TYPE=docker`:每线程一个容器,run 结束不销毁;容器内 `git commit` 的**作者 = 宿主 gitconfig 的 `user.name`/`user.email`**(未被强制覆盖);`docker inspect` 的 env/挂载 grep 不到任何 token;删线程时容器 best-effort 清理;Docker daemon 不可达 → 启动即失败
- [ ] 一次 `agent` run 能:改文件 → `git commit` 到 `local/<slug>` 分支 → 汇报本地分支名,**不** push、**不**建 PR
- [ ] `/state`(单快照)与 `/history`(≤5 + 90s + `/state` 降级 + 合并)代理;超大线程(≥30MB checkpoint)下打开线程页 P95 < 10s,期间其他请求不冻结 > 1s(queue-probe `lag_ms` 验证)
- [ ] 进程 `env` 与运行时内存中 grep 不到 `GITHUB_APP` / `GITHUB_TOKEN` / 任何 OAuth token 字段

---

## 13. 主要文件索引(本变体,删/留/改)

| 状态 | 文件 |
|---|---|
| 改 | `langgraph.json`(两图)、`agent/api/app.py`(auth 钩子 + 路由裁剪)、`agent/prompt.py`(本地提交段)、`agent/server.py`(沙箱去 proxy + 工具列表)、`agent/dispatch.py`(source 收窄)、`agent/completion.py`(失败回执简化)、`agent/scheduler.py`(去 baby_sit/cost) |
| 留 | `agent/webapp.py`、`agent/utils/event_loop.py`、`agent/postgres_persistence.py`、`agent/input_messages.py`、`agent/utils/sandbox.py`、`agent/integrations/local.py`(裸开发回退)、`agent/utils/sandbox_paths.py`、`agent/utils/queue_debug.py`、`agent/api/health.py`、`agent/dashboard/{thread_api,plan_api,plan_store,workflow_approval_api}.py`、`agent/middleware/{sanitize,tool_error,exclude_tools,model_call_limit,model_call_timeout,strip_thoughts,prepare_run}.py`、`agent/tools/{web_search,fetch_url,http_request,save_plan,approve_plan,enter_plan_mode,schedule_thread_wakeup,background_execute,background_task,recreate_sandbox,sandbox_reset,report_platform_issue,read_user_settings,save_user_instructions,save_user_skill,delete_user_skill,output_iframe,read_repo_file*,search_repo_code*}.py` |
| 改语义 | `agent/local_auth.py`(从死代码→接上,作为认证入口)、`agent/utils/repo.py`(仅本地仓库解析) |
| 删 | `agent/{reviewer,analyzer,chat,baby_sit,session_cost}.py`、`agent/review/*`、`agent/graphs/{reviewer,analyzer,chat}.py`、`agent/utils/{github_app,github_checks,github_ci,github_comments,github_feedback,github_http,github_org_membership,github_proxy,github_token}.py`、`agent/middleware/{refresh_github_proxy,pr_creation_guard,workflow_push_guard,settle_review_check}.py`、`agent/webhooks/{slack,linear,github,common}.py`(留 run-complete)、`agent/dashboard/{oauth,github_token_auth,oidc_auth,slack_oauth,notion_oauth,user_mappings,review_api,review_styles,review_style_jobs,review_chat_api,pr_diff,pull_request_status,agent_usage,repo_cache,repo_snapshots,enabled_repos,team_credentials,team_settings,profiles*}.py`、`agent/tools/{open_pull_request,request_pr_review,manage_baby_sit,notify_automation_channel,fetch_review_diff,publish_review,add_finding,update_finding,list_findings,read_finding_outcomes,reply_to_finding_thread,resolve_finding_thread,save_review_style,list_review_findings,admin_gate,linear_*,slack_*}.py`、`agent/integrations/{langsmith,daytona,modal,runloop,e2b}.py`(5 种云 provider 全删)、`agent/integrations/{linear,notion_mcp,langsmith_tools 的 GitHub 相关}.py`、`test_github_oauth.py` |

> 注:本变体沙箱**只保留 `docker` 一种 provider**(+ `local` 裸开发回退);5 种云 provider 及其 SDK 依赖(`daytona`/`e2b`/`modal`/`runloop`/LangSmith 沙箱)从 `pyproject.toml`/`uv.lock` 一并移除。

---

## 14. 实现顺序建议(给 AI 编程代理)

1. **锁骨架**:改 `langgraph.json` 两图;`agent/api/app.py` 换 auth 钩子(§2)+ 裁剪路由;确保空实现也能 `langgraph dev` 起 + `/health` 200。
2. **打通 Docker 沙箱 + 本地提交**:新增 `agent/integrations/docker.py`(§4.1.1)+ `SANDBOX_FACTORIES` 加 `docker`;`SANDBOX_TYPE=docker`;`agent/server.py` 去 proxy 链;接 `LOCAL_COMMIT_SECTION` 提示;跑通一次"改文件→容器内 commit 到 `local/<slug>`"。
3. **接持久化与有界代理**:Postgres checkpointer/store(§7)+ `thread_api.py` 有界 state/history(§9.2)。
4. **接完成 webhook**(§8)+ `scheduler` 裁剪(§4.2)。
5. **提示词收尾**(§10)+ env 清单(§11)。
6. **逐条过 §12 验收清单**,尤其"超大线程不冻结"与"内存无 GitHub 凭据"两条。

---

## 15. 实现清单:路由 / Webhook / HTTP 字段盘点

> 本节是**可直接照做的实现契约**:§15.1 是"运行时免费提供、只需调用"的协议路由(含字段级契约);§15.2 是"必须自己实现"的 dashboard 路由(按活服务抓取的 92 端点逐条归类:留/改/删);§15.3 是 webhook 端点;§15.4 是贯穿所有端点的 HTTP 协议字段。
> 基线:活服务 `:2024` OpenAPI(148 paths = 92 dashboard + 56 协议/其他)。
>
> **图例(功能列首位彩色图标)**:🔐 鉴权 · ⚡ 触发 run · 📖 读 · ✏️ 写 · 🗑️ 删 · 🔍 搜索 · 📜 历史 · 📡 SSE 流 · 🔁 有界代理 · ✅ 批准/标记 · ❌ 拒绝 · 🛑 取消 · ⏰ 定时任务 · 🧩 技能/指令 · 🎛️ 配置选项 · 🩺 健康/调试 · 🔔 完成 webhook · 📝 plan · 🚦 推送审批 · 🌿 git diff · 📐 结构定义。功能列**加粗**=核心动作,*斜体*=必须注意的语义。

### 15.1 协议路由(`langgraph_api` 提供,不实现,只消费)

**不需要写的代码**:起 `langgraph dev` 即全部就位。列出实际被用到的子集与契约,实现 dashboard 侧时按这些字段调。

**threads(核心)**

| 端点 | 功能 | 请求 / 成功响应 | 失败定义 |
|---|---|---|---|
| `POST /threads` | ✏️ **建线程记录**(业务状态的容器;可指定 id 便于确定性命名) | body `{"thread_id"?: uuid, "metadata": {…}}` → **200** thread 对象 | 400 body 非法;id 已存在且 metadata 冲突 → 409 |
| `GET /threads/{id}` | 📖 **取线程记录**(metadata 读取/权限判断的数据源) | → **200** `{thread_id, status, metadata, created_at, updated_at}` | 不存在 → **404** |
| `PATCH /threads/{id}` | ✏️ **合并更新 metadata**(打 `latest_run_status`/`viewed`/`resolved` 等标记都走这;_merge 语义,非替换_) | body `{"metadata": {…}}`(merge,非替换) → **200** 更新后对象 | 不存在 → 404 |
| `DELETE /threads/{id}` | 🗑️ **删线程** + 其 checkpoint/流(*不可恢复*) | → **200/204** | 不存在 → 404 |
| `POST /threads/search` | 🔍 **搜线程**(按 metadata 过滤 + 排序 + 分页;dashboard 列表的底层) | body `{"metadata": {…}, "limit", "offset", "sort_by", "sort_order"}` → **200** `[thread…]`(≤limit 条) | 400 参数非法(如 limit 越界);无匹配 → **200 空数组**(*不是错误*) |
| `GET /threads/{id}/state` | 📖 **单快照读**:取最新 checkpoint 的完整 state——前端主 transcript 的唯一来源 | → **200** ThreadState:`{"values": {messages, …通道}, "next": […], "configurable": {thread_id, checkpoint_id, checkpoint_ns}, "metadata": {…}}` | 线程不存在 → 404;从未 run 过 → **200** 空快照(dashboard 侧再归一化为 `{"values":{},"next":[]}`) |
| `GET /threads/{id}/history` | 📜 **有界历史**:沿 checkpoint 链取最近 N 个快照(发现/跳转用,*禁止全量翻页*) | query `limit/before/after/filter` → **200** `[{checkpoint_id, parent_checkpoint_id, values, metadata, timestamp, next}, …]`(≤limit,最新在前) | 404;超大线程上遍历慢 → 调用方(有界代理)超时后自行降级,*本端点不主动 503* |
| `POST /threads/{id}/history` | 📜 同上 POST 形式(参数同 query) | 同上 | 同上 |
| `GET /threads/{id}/stream` | 📡 **SSE join 线程流**:连接后从当前/`last_event_id` 处开始推该线程全部 run 事件;`stream_resumable` 的 run 可断线重放 | query `last_event_id`、`stream_mode` → **200** `text/event-stream`(帧见 §15.4.6) | 404;连接中途上游 run 结束 → 流以终止帧正常关闭(*不是失败*) |
| `POST /threads/{id}/stream/events` | 📡 **v2 事件流**(前端 `StreamProvider` 主口;`__event_streaming_v2` 标记启用 v3 通道:tools/lifecycle/子代理命名空间) | body 同 stream 参数 → **200** SSE | 同上 |

**runs(触发契约,§5/§6 的落地字段)**

| 端点 | 功能 | 成功 | 失败 |
|---|---|---|---|
| `POST /threads/{id}/runs` | ⚡ **建 run(durable 触发)**——`create_durable_run` 就是打这里;run 进 inmem 队列异步执行,响应立即返回 | **200/202** `{"run_id", "thread_id", "status": "pending", …}`;`if_not_exists="create"` 时自动建线程 | 404 线程不存在且未给 `if_not_exists`;422 webhook 策略拒绝(§8.1);线程 busy 时按 `multitask_strategy` 处理(`interrupt` → 打断旧 run 并成功;`enqueue` → 排队;`reject` → **409** busy) |
| `POST /threads/{id}/runs/stream` | ⚡ **建 run + 直连 SSE**(建+流一步) | **200** SSE(首帧含 run 信息) | 同上;流错误 → 终止帧内带 error |
| `POST /threads/{id}/runs/wait` | ⚡ **建 run 并阻塞等终态** | **200** 终态结果 | 超时/失败在 body 报 |
| `GET /threads/{id}/runs` | 📖 **列线程的 runs**(新在前) | **200** `[run…]` | 404 |
| `GET /threads/{id}/runs/{run_id}` | 📖 **查单个 run 状态**(`status: pending/running/success/error/timeout/interrupted/cancelled`) | **200** run 对象 | 404 |
| `DELETE /threads/{id}/runs/{run_id}` | 🗑️ **删 run 记录** | **200/204** | 404 |
| `POST /threads/{id}/runs/{run_id}/cancel` | 🛑 **取消运行中的 run**(置 cancelled,不删 checkpoint) | **200** run 对象(status 置 cancelled);已在跑则异步终止 | 404 run 不存在/已是终态 → 409 或 400 |
| `GET /threads/{id}/runs/{run_id}/stream` | 📡 **单 run SSE join**(`last_event_id` 重放) | **200** SSE | 404;run 已结束且无重放 → 立即终止帧 |
| `POST /threads/{id}/commands` | ⚡ **SDK command 信封**(§15.4.4):`run.start`/`input.respond`/`input.inject`/`state.fork`;dashboard 发消息走这里 | **200** `{"type": "success", "run_id"?: str}` | `{"type": "error", "error": {…}}`(*业务错,200 体*);404 线程不存在且 method≠run.start;400 信封非法 |
| `POST /runs/crons`、`POST/GET /runs/crons/search|count`、`GET/PATCH/DELETE /runs/crons/{cron_id}`、`POST /threads/{id}/runs/crons` | ⏰ **cron 面**:`scheduler` 全部定时任务的注册/查询/改/删(表达式 cron + `assistant_id=scheduler` + `input.task`) | 建:**200** cron 对象(`{cron_id, cron, input, …}`);查:**200** 列表/对象;改/删:**200/204** | 400 非法 cron 表达式;404 不存在 |

**`POST /threads/{id}/runs` 的 body 契约(复刻必须逐字段一致)**:

```jsonc
{
  "input": {"messages": [RunMessage], "files": {…?}},   // §5 的 XML 信封
  "config": {"configurable": {…见 15.4.3…}, "recursion_limit"?},
  "metadata": {"kind": "agent", "source": "dashboard|web|automation|schedule",
               "local_user": "…", "repo": "…", "title": "…", "latest_run_id"? …},
  "multitask_strategy": "interrupt",          // 后台 follow-up 可 "enqueue"
  "durability": "sync",
  "if_not_exists": "create",
  "stream_mode": ["values","updates","messages","custom","tasks","checkpoints"],
  "stream_subgraphs": true,
  "stream_resumable": true,
  "webhook": "/webhooks/run-complete?token=…",  // 仅 secret 已设时
  "after_seconds": null                        // 可选延迟
}
```
resp(200/202):`{"run_id", "thread_id", "status", "created_at", …}`。

**store(长期记忆,`AsyncPostgresStore`)**

| 端点 | 功能 | 成功 | 失败 |
|---|---|---|---|
| `PUT /store/items` | ✏️ **写长期记忆**(plan 内容、用户指令、skills、schedules、失败去重标记都落这) | body `{"namespace": [str…], "key": str, "value": {…}}` → **200** 写入的 item | 400 参数缺失(namespace 必须非空数组) |
| `GET /store/items` | 📖 **按 (namespace, key) 精确读** | query `namespace`、`key` → **200** item;**404** = 不存在(*store 语义,不是 200 null*) | 404 无此项 |
| `DELETE /store/items` | 🗑️ **删 item** | **200/204** | 404 |
| `POST /store/items/search` | 🔍 **按 namespace 前缀 + 分页搜** | body `{"namespace"?: […], "prefix"?, "limit", "offset"}` → **200** `{items, …}` | 400 |
| `POST /store/namespaces` | 📖 **列命名空间**(调试用) | **200** 列表 | — |

**其余协议端点**(`assistants/*`、`/mcp/`、`/a2a/*`、`/store/namespaces`、`/runs/stream|wait|batch|cancel` 无线程形态):**不用**,不消费、不管。

**SSE 兼容性验收标准**(不逐帧抄 schema):前端 `@langchain/react` 的 `StreamProvider`/`StreamController` 与 Python 侧 `langgraph-sdk 0.4.2` 都能无感消费;`stream_resumable=True` 的 run 在晚连接/断线后用 `last_event_id` 重放;子代理事件带命名空间(`stream_subgraphs`)。

### 15.2 必须实现的 dashboard 路由(`/dashboard/api/*`)

基线 92 端点。**留**=原样移植(字段照抄现有实现);**改**=换认证/换判据;✂️=删除(依赖 GitHub/PR/Slack/Linear/review/团队多用户)。

**A. 认证(改)**

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `GET /auth/login`、`GET /auth/callback`、`POST /auth/logout`、`POST /auth/desktop/exchange`、`GET /slack/login`、`GET /slack/callback`、`GET /notion/login`、`GET /notion/callback` | 🔐 ~~全部删除~~(GitHub/Slack/Notion 登录链),替换为下一行 | — | 变体里访问这些路径 = 路由不存在,**404** |
| `GET /auth/token-login`(新增) | 🔐 **静态 token 换会话 cookie**(*唯一的登录入口*) | query `token` 与 `OPEN_SWE_LOCAL_AUTH_TOKEN` 经 `hmac.compare_digest` 相等 → **200** `{}` + `Set-Cookie: osw_session=<HMAC 签名, 含 identity="local-user"、exp>`;未设 token 且 `OPEN_SWE_ALLOW_ANON=1` → **200** 发 `anon` 会话(启动时有显著告警) | token 缺/错 → **401** `{"detail":"invalid token"}`(*不区分"缺"和"错",防探测*);未设 token 且未开 anon → **503** `{"detail":"auth not configured; set OPEN_SWE_LOCAL_AUTH_TOKEN"}`(*fail-closed*) |
| `POST /auth/logout` | 🔐 **作废当前会话 cookie** | → **200** `{}` + `Set-Cookie: osw_session=; Max-Age=0` | 无 cookie 也 **200**(*幂等,不是错误*) |
| `GET /me` | 📖 **读当前身份** + 用户级指令/设置 | → **200** `{"sub": "local-user", "email": null, "name": "local-user", "admin": true, "instructions"?: str, "profile": {…}}` | 会话无效/过期 → **401**(前端据此跳登录) |
| `GET/PUT/DELETE /me/instructions` | 📝 **用户级指令**(注入所有线程的系统提示;跨线程共享面) | GET **200** `{"instructions": str}`(无则空串);PUT body 任意文本 **200** 回显;DELETE **200** `{}` | 401 未认证 |
| `GET/PUT /profile` | 🎛️ **用户设置**(模型默认、draft 偏好等本地化字段) | GET **200** profile 对象;PUT **200** 更新后对象 | 400 字段非法;401 |
| `GET /my-mapping`、`GET/DELETE /admin/user-mappings*`、`GET /repos`、`GET /team-credentials*`、`GET/PUT /team-settings*`、`PUT /team-settings/transcription`、`GET/PUT/DELETE /my-credentials/*`(currents/langsmith/notion) | ✂️ 删(多用户映射/团队凭据/第三方集成) | — | 404(路由不存在) |

**B. 线程核心(留,变体主战场)**

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `GET /threads` | 📖 **线程列表**(Agents 首页/侧栏数据源):过滤+排序+分页封装运行时 `POST /threads/search`(加 `metadata.local_user` 过滤,单用户下恒真) | query `limit(≤100,默认25)/offset/resolved?/viewed?/source?/status?/query?(模糊)/scope=all\|interactive\|automation/automation_id?/sort_by=updated_at\|created_at` → **200** `{"items": [ThreadListItem], "limit", "offset", "hasMore": bool}`;无结果 = 200 空 items | 401 未认证;400 参数非法(如 `limit>100`、`scope` 非枚举) |
| `GET /threads/page`、`GET /threads/sidebar` | 📖 列表变体(分页页 / 侧栏精简字段,同一底层) | 同 `GET /threads`(字段子集) | 同上 |
| `GET /threads/{id}` | 📖 **线程详情元数据**(status/owner/links;*transcript 不在这*——前端另调 `/state` hydrate) | → **200** ThreadListItem 全字段 + `{"isOwner": true, "status", "latest_run_id"?, "links": {"web", "plan"?, "trace"?}}` | **404 归一化**:线程不存在**或**非本用户 → 一律 404(不泄漏存在性;变体单用户下 owner 判据恒真);另:从未 run 的线程,详情里 `status="idle"` |
| `ThreadListItem` 字段(定义) | 📐 **列表/详情元素结构** | `id, title, repo, repoFullName, branch, model, effort, planMode, planStatus, adminThread, source, origin, threadCategory, triggerKind, automationId?, automationName?, status(idle\|running\|error\|interrupted), viewed, viewedAt, createdAt, updatedAt, lastUserMessage?` | — |
| `DELETE /threads/{id}` | 🗑️ **删线程** + 其 checkpoint/流(*不可恢复*) | **200/204** `{}` | 404(归一化同上);线程 busy(有 running run)→ 先取消再删,或 **409** `{"detail":"thread busy"}` |
| `POST /threads/{id}/resolve` | ✅ **标记线程已解决/未解决**(列表分组) | body `{"resolved": bool}` → **200** 更新后的列表项 | 404 |
| `POST /threads/{id}/cancel` | 🛑 **取消线程当前 run** | → **200** `{"cancelled": bool}`;`cancelled=false` 且 200 = 无活动 run(*幂等成功,不是错误*) | 404 线程不存在 |
| `POST /threads/{id}/runs/{run_id}/cancel` | 🛑 取消指定 run | 同上 | 404 run 不存在;run 已终态 → 400/409 |
| `POST /threads/{id}/messages` | ⚡ **发消息 = 触发 run**(用户主动交互入口) | body(`ThreadMessageBody`):`{"content": str(≤20000), "images": [{data(base64), media_type, filename?}], "model_id"?, "effort"?, "plan_mode": bool}` → 构造 `build_run_input` 信封(source=`web`)→ `dispatch_agent_run` → **200** `{"run_id", "thread_id", "queued": bool}`;线程不存在时 `if_not_exists="create"` 自动建 | 400 `content` 超 20000 或空;401 未认证;运行时 409(busy 且策略 reject)透传;运行时不可达 → **502** `{"detail":"runtime unreachable"}` |
| `POST /threads/{id}/commands` | ⚡ **SDK command 信封透传 + 富化**(§15.4.4):前端 `StreamProvider` 的主发送口;`run.start` 时服务端注入 `configurable`(`local_user`/`repo`/`plan_mode`/`prepare_run_id`/模型覆盖)+ `metadata.source="dashboard"`,再 POST 到运行时同名端点 | raw JSON body → 运行时返回 `{"type":"success","run_id"?}` 时 → **200** 原样透传;`{"type":"error",…}` 时 → **200 + 同体透传**(*业务错在体里,HTTP 仍 200,前端按 type 判*) | **404 归一化**:线程不存在且 `method≠run.start` → 404;非 JSON body → **415**;运行时不可达 → 502;线程 busy 且方法为 `input.*` → 409 透传 |
| `POST /threads/{id}/history` | 🔁 **有界历史代理**(防超大线程冻结单循环的第一道闸) | raw body(`{"limit"?, "before"?, …}`)→ 无过滤请求 `limit` 强制 clamp ≤5 → 90s 预算 → **200** 透传运行时 history 数组(≤limit 条,最新在前) | 上游超时 → **503** `{"detail": "thread history request timed out; retry"}`(退避链:`{limit:1}` 重试 3 次×1.5s → 仍败则降级 `/state` 单快照包装成 1 条;30s 快照缓存去重);404/415 同上 |
| `GET /threads/{id}/state` | 🔁 **有界 state 代理**(主 transcript 读取入口) | 15s 预算单快照 → **200** 透传 ThreadState;**归一化**:线程存在但从未 run → `{"values": {}, "next": []}`(*200,不是 404*——前端据此显示空对话) | 线程不存在 → 404;15s 预算耗尽 → **503**(同上);上游 5xx → 502 |
| `GET /threads/{id}/stream` | 📡 **SSE 透传**:join 线程流(上游 `GET /threads/{id}/stream`,带 `last_event_id`),权限校验后字节流原样回传 | **200** `text/event-stream`;run 结束 → 流正常关闭 | 404/401 先于流校验;运行中上游断开 → 流带错误终止帧;**客户端断开必须释放上游订阅**(流订阅回收,否则泄漏事件循环) |
| `POST /threads/{id}/stream/events` | 📡 **SSE 透传 v2 事件流**(前端 `StreamProvider` 主口;晚连接可见历史事件——`stream_resumable` 的价值) | 同上 | 同上 |

**C. Plan(留,7 端点)** — 存储:store 命名空间 `plans`

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `GET /plan/{thread_id}` | 📝 **读当前 plan**(内容+状态+审批人) | **200** `{"content", "content_format", "status": "draft\|pending\|approved\|rejected", "author_login", "comments": […]}` | **404** 该线程无 plan(前端据此不显示 plan 面板) |
| `PUT /plan/{thread_id}` | 📝 **保存/更新 plan**(进入待审批) | body `{"content", "content_format": "html"\|"markdown"}` → **200** 更新后对象 | 400 content 空或 `content_format` 非法;线程不存在 → 404 |
| `POST /plan/{thread_id}/approve` | ✅ **审批通过**(状态翻转);*审批动作由前端随后发 `run.start`(`plan_mode=false`)——服务端不自动跑* | **200** 更新后对象;`status="approved"` | 404 无 plan;已终态再审批 → **409** `{"detail":"plan already approved/rejected"}` |
| `POST /plan/{thread_id}/reject` | ❌ **审批拒绝** | **200** `status="rejected"` | 404;409 同上 |
| `GET /plan/{thread_id}/comments` | 📖 **列 plan 评论** | **200** `[{"comment_id", "author_login", "content", "created_at"}]` | 404 无 plan |
| `POST /plan/{thread_id}/comments` | 📝 **加评论** | body `{"content"}` → **200** 新评论对象 | 404;400 空内容 |
| `DELETE /plan/{thread_id}/comments/{comment_id}` | 🗑️ **删评论**(仅作者或 admin) | **200/204** | 404 不存在;非作者 → **403** |

**D. Workflow 审批(留,3 端点)** — 存储:thread metadata `workflow_approvals`

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `GET /workflow-approval/{thread_id}` | 🚦 **列该线程待决/历史审批**(push 守卫命中时挂起) | **200** `{"approvals": [{"fingerprint", "message", "status": "pending\|approved\|rejected", "created_at"}]}`;无 → 200 空数组 | 404 线程不存在 |
| `POST /workflow-approval/{thread_id}/{fingerprint}/approve` | ✅ **批准该指纹的推送** | **200** 更新后审批对象(status=approved) | 404 线程/指纹不存在;已决 → **409** |
| `POST /workflow-approval/{thread_id}/{fingerprint}/reject` | ❌ **拒绝该指纹的推送** | **200** status=rejected | 同上 |

**E. Automations/schedules(留,5 端点)** — 落地:每个 schedule = 运行时一条 cron(`POST /runs/crons`,`assistant_id=scheduler`)

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `GET /schedules` | ⏰ **列定时任务** | query `limit/offset` → **200** `[{"id","name","cron","prompt","enabled","next_fire_at","last_result"?}]` | 401 |
| `POST /schedules` | ⏰ **新建定时任务**(到点由 scheduler 扇出 `launch_scheduled_agent_run`) | body `{"name","cron","prompt","enabled"}` → **200** 新建对象(含生成的 `id` 与 `next_fire_at`) | **400** 非法 cron 表达式(透传运行时错误);401 |
| `PATCH /schedules/{id}` | ⏰ **改名/改 cron/启停** | body 部分字段 → **200** 更新后对象 | 404;400 非法 cron |
| `DELETE /schedules/{id}` | 🗑️ **删定时任务**(连带删运行时 cron) | **200/204** | 404 |
| `POST /schedules/{id}/trigger` | ⏰ **立即触发一次**(不等下个 tick) | **200** `{"triggered": true, "run_id"?}`;触发即入队,异步执行 | 404;运行时不可达 → 502 |

**F. Skills / repo instructions(留,8 端点)** — 存储:store(`skills`/`org_skills` 命名空间;repo instructions 按 `owner/name` 键)

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `GET /skills` | 🧩 **列用户技能** | **200** `[{"name","description","updated_at"}]` | 401 |
| `POST /skills` | 🧩 **建技能**(body `{"name","description","instructions"?}`;name 小写字母/数字/单连字符) | **200** 新建对象 | **400** name 非法;已存在 → **409** |
| `PUT /skills/{name}` | 🧩 **改技能** | **200** 更新后 | 404 不存在 |
| `DELETE /skills/{name}` | 🗑️ **删技能** | **200/204** | 404 |
| `GET/POST /organization-skills`、`PUT/DELETE /organization-skills/{name}` | 🧩 **组织技能**(本地单用户可并入用户技能,保留双命名空间则同上行契约) | 同 skills 各行 | 同上 |
| `GET /agent-instructions` | 🧩 **列 per-repo 指令**(注入系统提示的 repo instructions) | **200** `[{"full_name","content","updated_at"}]` | 401 |
| `POST /agent-instructions` | 🧩 **建 per-repo 指令**(body `{"full_name":"owner/name","content"}`) | **200** 新建 | 400 full_name 非 `owner/name` 格式 |
| `GET/PUT/DELETE /agent-instructions/{full_name}` | 🧩 **读/改/删单条 per-repo 指令** | GET/PUT **200** 对象;DELETE **200/204** | 404 |

**G. 模型选项(留,1 端点)**

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `GET /options` | 🎛️ **前端下拉的模型/effort 列表** | **200** `{"models": [{"id","label","effort_options": [str]}]}`(来自 `SUPPORTED_MODEL_IDS`,按已配置 provider 过滤) | 401;无可用模型 → 200 空数组(前端显示"未配置模型") |

**H. Health/debug(留,4 端点)**

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `GET /health` | 🩺 **存活探测**(k8s/编排用) | **200** `{"status":"healthy"}`(仅进程活着即 200;**不**探测 Postgres/运行时) | 进程不可达(连接拒绝) |
| `GET /ok` | 🩺 同上的裸探活 | **200** `ok` | 同上 |
| `GET /debug/queue-probe` | 🩺 **事件循环 lag 探针快照**(§12 验收"不冻结 >1s"的观测口) | **200** `{"lag_ms": float, "loop_thread": str, "samples": […]}`;`OPEN_SWE_QUEUE_DEBUG` 未开 → 200 空样本 | 401 未认证 |
| `POST /debug/queue-probe` | 🩺 **触发一次即时探针** | **200** 同 GET | 401 |

**I. 可选留(本地 git 相关,默认不做,需要时加回)**

| 端点 | 功能 | 成功 | 失败定义 |
|---|---|---|---|
| `GET /threads/{id}/working-tree-diff` | 🌿 **沙箱内 `git diff` 物化**,供前端 diff 面板(*纯本地能力,不依赖 GitHub*) | **200** `{"diff": str, "branch": str}` | 404 线程/沙箱不存在;沙箱无 git 仓 → **200** 空 diff |
| `GET /threads/{id}/branch-diff` | 🌿 **分支 vs base 的 diff** | **200** 同上 | 同上 |
| `GET /threads/{id}/terminal/connect` | 🖥️ ✂️ 删(桌面终端,WebSocket) | — | 404 |

**J. 删除清单(✂️,共 60 端点)**

`/admin/*`(evals/threads cancel/user-mappings)、`/reviews/*` 全部(含 `…/chat*` 8 个 PR 聊天代理)、`/review-styles/*`、`/enabled-review-repos`、`/repo-snapshots*`、`/sandbox-settings`、`/environments*`、`/voice/*`、`/agent-usage-leaderboard`、`/threads/{id}/{pr-diff, pull-request-status, recovery.patch}`、A 节所列全部 OAuth/凭据/团队端点。

### 15.3 Webhook 端点

| 端点 | 方向 | 功能 | 成功(2xx) | 失败定义 |
|---|---|---|---|---|
| `POST /webhooks/run-complete?token=…` | 运行时 →(loopback 或真实网络)→ 本服务 | 🔔 **留,唯一保留的 webhook**:run 终结(成功/失败/超时)的统一收尾信号——*保证"任何 run 都以一个确定信号结束,即使 agent 崩溃"* | token 与 `RUN_COMPLETE_WEBHOOK_SECRET` 经 `hmac.compare_digest` 相等 + body 为合法 JSON → **200**:按 `run_id` 幂等处理(`handle_run_completion`):`status=success` → 记日志;`error/timeout/interrupted` → 取 `thread_id` metadata,落 `failure_ack_run_ids` + `latest_run_status` 标记;resp `{"status": "handled"}`(首次)或 `{"status": "deduped", "run_id"}`(重复回调,幂等) | **503** `RUN_COMPLETE_WEBHOOK_SECRET` 未设(fail-closed,路由整体拒收);**401** token 缺失/错(不区分);body 非 JSON → **200** `{"status":"error","message":"Invalid JSON"}`(不 5xx——webhook 失败不应触发运行时重试风暴);`thread_id` 已不存在 → **200** `{"status":"ignored","reason":"thread gone"}` |
| `POST /webhooks/github`、`GET/POST /webhooks/slack`、`POST /webhooks/slack/interactivity`、`GET/POST /webhooks/linear` | 外部平台 → 本服务 | 🚫 ✂️ **全删**(§0/§5;无 GitHub/Slack/Linear 触发源) | — | 变体里访问 = 路由不存在 → **404** |

### 15.4 HTTP 协议字段盘点

**15.4.1 认证与通用头**

- **协议面**(`:2024` 非 `/dashboard/api` 路由):`Authorization: Bearer <OPEN_SWE_LOCAL_AUTH_TOKEN>`(接 `agent/local_auth.py` 的 `Auth.authenticate`,身份 `local-user`)。
  - 成功:token 经 `hmac.compare_digest` 相等 → 放行,`langgraph_auth_user={"identity":"local-user"}` 注入。
  - 失败:scheme 非 bearer / token 缺 / 错 → **401** `{"detail":"Invalid bearer token"}`。
- **dashboard 面**:会话 cookie `osw_session`(§15.2-A 下发,`HttpOnly`、`SameSite=Lax`、`Path=/`)。
  - 成功:cookie 有效且未过期 → `_SESSION_DEP` 注入 `{sub, email, name, admin}`。
  - 失败:cookie 缺 / 签名坏 / 过期 → **401** `{"detail":"unauthenticated"}`(前端 `RequireLogin` 据此跳 `/auth/token-login`)。
- **CORS**(跨 origin 时):`Access-Control-Allow-Credentials: true` + 具体 origin 白名单(`DASHBOARD_ALLOWED_ORIGINS`;**禁止 `*`+credentials**,上游逻辑强校验)。
  - 成功:Origin 在白名单 → 回 `Access-Control-Allow-Origin: <该origin>` + `Allow-Credentials: true`;预检 `OPTIONS` → **204**。
  - 失败:Origin 不在白名单 → 无 `Access-Control-Allow-Origin` 头(浏览器拦,服务端仍 200 处理,但前端拿不到)。
- **全部 JSON 端点**:`Content-Type: application/json`(proxy 端点显式校验,非 JSON → **415** `Unsupported Media Type`)。
- **统一状态码语义(全端点适用)**:

  | 码 | 含义 | body 约定 |
  |---|---|---|
  | **200** | 成功(含"幂等无变化") | 资源对象 / 列表 / `{status…}` |
  | **201/202** | 建 run / 入队成功(异步) | `{"run_id",…}` |
  | **204** | 成功无返回体(DELETE/logout) | — |
  | **400** | 请求体/参数非法 | `{"detail": "<字段>: <原因>"}` |
  | **401** | 未认证 / 凭证错 | `{"detail":"…"}` |
  | **403** | 已认证但无权(非作者删评论等) | `{"detail":"forbidden"}` |
  | **404** | 不存在**或**非 owner(归一化,不泄漏存在性) | `{"detail":"not found"}` |
  | **409** | 冲突(busy、已终态再操作、技能重名) | `{"detail":"… conflict"}` |
  | **415** | Content-Type 非 JSON | `{"detail":"unsupported media type"}` |
  | **422** | 协议层校验失败(运行时 webhook 策略等) | `{"detail":"…"}` |
  | **502** | 上游(运行时)不可达 | `{"detail":"runtime unreachable"}` |
  | **503** | 有界读超时 / fail-closed(认证未配置、secret 未设) | `{"detail":"… timed out; retry"}` / `{"detail":"… not configured"}` |

**15.4.2 线程 metadata 契约(thread jsonb,业务状态的唯一载体)**

```jsonc
{
  "kind": "agent",                  // 变体恒为 agent(不再有 reviewer/review_chat/…)
  "local_user": "local-user",       // owner 判据(替代 github_login)
  "source": "dashboard|web|automation|schedule",
  "repo": {"owner"?, "name"},       // 解析自消息 repo: 语法或 /repos 配置
  "title": str,
  "status": "idle|running|error|interrupted",  // 展示态,由 latest_run_status 推导
  "latest_run_status": "pending|running|success|error|timeout|interrupted",
  "latest_run_id": str,
  "viewed": bool, "viewed_at_ms": int,
  "resolved": bool,
  "branch": str,                    // 线程稳定分支名(local/<slug>)
  "model": str, "effort"?: str, "plan_mode": bool, "plan_status"?: str,
  "sandbox_id"?: str,               // 沙箱注册表键(仅运行时进程内有效)
  "failure_ack_run_ids"?: [str],    // 完成 webhook 幂等去重
  "prepared_run_ids"?: [str]
}
```

**15.4.3 `configurable` 契约(runs/commands 的 `config.configurable`)**

| 键 | 值 | 说明 |
|---|---|---|
| `thread_id` | uuid | 恒有 |
| `source` | `dashboard\|web\|automation\|schedule` | 决定 Surface 与提示分段 |
| `local_user` | `"local-user"`(或 `anon`) | 替代 `github_login` |
| `repo` | `{"owner","name"}` | 可缺 |
| `agent_model_id` / `agent_effort` | str | 用户选模型(缺省走团队/全局默认) |
| `plan_mode` | bool | plan mode 开关 |
| `prepare_run_id` | 新 uuid(每次 run) | PrepareRun 中间件关联 |
| `__event_streaming_v2` | `true` | **必须置位**(v3 流协议标记,镜像 langgraph_api 的 key,不 import) |
| `sandbox_id` | str | 复用线程沙箱 |

**15.4.4 SDK command 信封(`POST /threads/{id}/commands` 的 body)**

```jsonc
// 前端(@langchain/react)发送:
{ "id": "cmd-<uuid>", "method": "run.start" | "input.respond" | "input.inject" | "state.fork",
  "params": {
    "input"?: {"messages": [RunMessage]},          // run.start 的消息
    "config"?: {"configurable": {…用户可选字段…}},
    "metadata"?: {…},
    "stream_mode"?: […], "stream_subgraphs"?: bool
  } }
// 服务端富化后透传;运行时响应:
{ "type": "success", "run_id"?: str } | { "type": "error", "error": {…} }
```

**15.4.5 run-complete webhook 的 body 契约**(运行时 `call_webhook` 组装;字段全集 = `allowed_fields` 白名单)

```jsonc
{
  "run_id": str, "thread_id": str, "assistant_id": "agent",
  "created_at": iso8601, "updated_at": iso8601,
  "metadata": {…thread/run metadata…},
  "kwargs": {"input": …, "config": …, …},
  "multitask_strategy": "interrupt",
  "status": "success" | "error" | "timeout",
  "run_started_at": iso8601, "run_ended_at": iso8601, "webhook_sent_at": iso8601,
  "values": {…最终 state(含 messages)…} | null,     // 最大字段;可在 webhooks.allowed_fields 裁掉
  "error": {"error": str, "message": str}           // 仅失败时
}
```

**15.4.6 SSE 事件流(GET/POST `…/stream*` 的响应)**
- `Content-Type: text/event-stream`;帧格式 `id: <event_id>\nevent: <type>\ndata: <json>\n\n`;
- 通道:`values`/`updates`/`messages`/`custom`/`tasks`/`checkpoints`(v2 标记下另含 tools/lifecycle 与子代理命名空间事件);
- 断线重连:客户端带 `Last-Event-ID` 头(或 `last_event_id` query)→ 服务端从该 event 重放(依赖 `stream_resumable=True`);
- **成功定义**:HTTP **200** + `text/event-stream`,首个响应头后立即开始推帧;run 正常结束 → 服务端发终止帧后**主动关闭连接**(客户端收到流结束,**不是错误**);重连带 `Last-Event-ID` → 从断点续帧(不重发已收帧、不漏帧)。
- **失败定义**:
  - 认证/权限失败 → 建流**前**返回 **401/404**(普通 JSON,不进流);
  - 流运行中上游 run 崩溃 → 流内发 `error`/终止帧后关闭(客户端据帧内 `error` 字段判,**HTTP 层仍 200**);
  - 代理侧(dashboard 透传)上游不可达 → **502**(建流前);
  - **客户端断开**:服务端必须释放上游订阅(流订阅回收),否则泄漏事件循环——这是"成功/失败"之外的**资源不变式**,§12 验收项。
- **验收口径**:用 `langgraph-sdk 0.4.2` 的 `client.runs.stream` 与前端 `StreamProvider` 双端消费通过,不逐帧比对 schema。
 - 代理侧(dashboard 透传)上游不可达 → **502**(建流前);
  - **客户端断开**:服务端必须释放上游订阅(流订阅回收),否则泄漏事件循环——这是"成功/失败"之外的**资源不变式**,§12 验收项。
- **验收口径**:用 `langgraph-sdk 0.4.2` 的 `client.runs.stream` 与前端 `StreamProvider` 双端消费通过,不逐帧比对 schema。
