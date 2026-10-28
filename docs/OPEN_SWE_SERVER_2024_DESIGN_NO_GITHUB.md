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

**新增(替代交付物)**:agent 在沙箱内 `git commit` 到线程稳定分支;远端 push **可选**且**纯本地凭据**(用户在 `~/.gitconfig` / credential helper 自备),服务器**不持有任何 GitHub token**。

**明确不在本变体范围**(从上游一起拿掉,因为它们强依赖 GitHub/PR):`reviewer` 图、`analyzer` 图、`chat`(review chat)图、PR CI 监视(`/baby-sit` + 其 scheduler 任务)、Linear/Slack 集成与对应 webhook、`fetch_review_diff` / `publish_review` 等 review 工具。保留范围见 §6 工具表。

---

## 1. 与基础设计书的逐条差异(总表)

| 域 | 基础版 | 本变体 | 落点(代码) |
|---|---|---|---|
| 身份/登录 | GitHub OAuth(`oauth.py`)、`github_token_auth.py`、`oidc_auth.py`、`slack_oauth.py`、`notion_oauth.py`;`agent/local_auth.py` 是未被引用的死代码 | **单一静态 token 认证**(`OPEN_SWE_LOCAL_AUTH_TOKEN`)或**开放本地模式** | `agent/dashboard/auth.py`(新增)、`agent/api/app.py` 依赖注入 |
| 注册图 | 5 个:agent/reviewer/analyzer/chat/scheduler | **2 个:agent + scheduler** | `langgraph.json`、`agent/graphs/*` |
| 沙箱鉴权 | 每沙箱注入 GitHub App installation token 作为 `gh` 代理(`_create_sandbox_with_proxy`、`_configure_github_proxy`) | **无任何 GitHub token**;`SANDBOX_TYPE=local`,git 走宿主机 `~/.gitconfig` | `agent/server.py`(`_resolve_proxy_token`/`_create_sandbox_with_proxy` 分支)、`agent/integrations/local.py`(已存在) |
| 提交交付 | `open_pull_request` 工具 + `COMMIT_PR_SECTION` 提示 + 两个 PR 守卫中间件 | **仅本地分支 commit**;push 可选;新写 `LOCAL_COMMIT_SECTION` 提示 | `agent/server.py` 工具列表、`agent/prompt.py` |
| 系统提示 | `COMMIT_PR_SECTION`/`DESKTOP_PR_SECTION`、协作署名(PR footer)、PR 规则 | 删除全部 PR 段落;`REPO_SETUP_SECTION`/`TASK_EXECUTION_SECTION` 改写为本地提交语义 | `agent/prompt.py` |
| Webhook 触发 | Slack/Linear/GitHub 三个 + run-complete | **只保留 run-complete** | `agent/webhooks/*`、`agent/api/app.py` |
| 中间件 | 含 `refresh_github_proxy`、`pr_creation_guard`、`workflow_push_guard`、`settle_review_check` | 删除 GitHub/PR 相关;保留公共件 | `agent/middleware/*` |
| 完成 webhook | 失败回复按 `kind` 走 Slack/PR 等渠道 | 简化:仅按 thread 落一个幂等失败回执(本地/dashboard 可查) | `agent/completion.py` |
| Dashboard 端点 | 117 个,含大量 PR/review/OAuth | 裁剪到 **线程/消息/state-history/plan/workflow/health** 一组 | `agent/dashboard/routes.py` 等 |
| env | 大量 `GITHUB_*`、`SLACK_*`、`LINEAR_*` | 只保留:`DATABASE_URI`、模型 key、`COMPLETION_WEBHOOK_URL`/`RUN_COMPLETE_WEBHOOK_SECRET`、`OPEN_SWE_LOCAL_AUTH_TOKEN`、`SANDBOX_TYPE=local`、`LOCAL_SANDBOX_ROOT_DIR` | `.env` |

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

### 2.4 沙箱内 git 身份(与 GitHub 解耦)
`configure_git_identity` 现在写死 bot 身份(`OPEN_SWE_BOT_NAME/EMAIL`)。本变体:改为**不覆盖用户身份**——本地沙箱(`SANDBOX_TYPE=local`)已经用 `_scoped_git_config_env` 把 `git config --global` 指向沙箱内 `.gitconfig-sandbox` 并 `[include]` 宿主机 `~/.gitconfig`,所以 **commit 作者 = 用户自己的 `~/.gitconfig`**。删除 `_configure_git_identity` 的强制 bot 写入;`git config user.name/email` 命令(基础书 §sender-context 里那一步)**整段删除**。

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
- 后端:**每线程一个本地沙箱**。`ensure_sandbox_for_thread(thread_id, repo, ...)` 保留签名,但**去掉** `github_proxy_token` / `github_proxy_repositories` / `base_proxy_config` 参数与 `_resolve_proxy_token`、`_create_sandbox_with_proxy`、`_refresh_github_proxy`、`record_proxy_token_expiry` 整条链——直接调 `create_sandbox()`(此时 `SANDBOX_TYPE=local`)。
- 沙箱缓存/复用/`get_cached_sandbox_backend` 不变(基础书 §3 单循环铁律依旧要求线程级注册表 + 循环亲和)。
- 子代理:`general-purpose`(完整沙箱工具)保留;`browser` 可选,保留与否随意(不依赖 GitHub)。
- plan mode / skills / repo instructions 保留;Linear 工单上下文**删除**。
- 工具列表见 §6。

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
`create_app()`:lifespan `pin_single_event_loop()`(二次)→ `validate_sandbox_startup_config()`(此时校验 `SANDBOX_TYPE=local` 的 env)→ `validate_local_dev_llm_config()`;退出 `close_cached_models()`。`QueueDebugMiddleware` + `enable_from_env()`(`OPEN_SWE_QUEUE_DEBUG=1`)保留。**auth 钩子换成 §2 的静态 token / 开放本地**。CORS 保留。

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
# 沙箱(§2.4 / §4.1)
SANDBOX_TYPE=local
LOCAL_SANDBOX_ROOT_DIR=/path/to/work # 缺省 = 进程 cwd
# 可选调试
OPEN_SWE_QUEUE_DEBUG=1
```

**删除的 env**:`GITHUB_APP_ID`、`GITHUB_APP_PRIVATE_KEY`、`GITHUB_APP_INSTALLATION_ID`、`GITHUB_TOKEN`、`SLACK_*`、`LINEAR_*`、`DEFAULT_REPO_OWNER`(可选保留,用于解析 `repo:` 语法)、所有 OAuth client/secret、`LANGSMITH_*` 沙箱 token(仅当仍用 LangSmith 追踪时保留,与凭据无关)。

---

## 12. 关键不变式(沿基础书 + 本变体追加)

沿用基础书 §11 全部 6 条(单循环 / 消息只增且写 checkpoint 前剥离 thinking / 所有 run 走 `create_durable_run` / 面向用户的 checkpoint 读有界 / 失败幂等 fail-closed / TTL 12h 删除)。追加:

7. **进程内与运行时不持有任何 GitHub/Slack/Linear 凭据**;`gh` CLI 在本变体**不代理、不要求登录**——沙箱内 git 用宿主机 `~/.gitconfig`(经 `.gitconfig-sandbox` 的 `[include]`)。
8. **交付物 = 本地分支 commit**;push/PR 永远不作为默认行为,仅用户显式要求 + 宿主凭据可用时才 push。
9. **认证面单一**:静态 token(`hmac.compare_digest`)或显式开放本地;不存在 GitHub/OIDC/Slack 任何登录路径。

**最小复刻验收(本变体)**:
- [ ] `langgraph.json` 仅 `agent` + `scheduler` + `http.app` + Postgres checkpointer/store + TTL 可启动
- [ ] `pin_single_event_loop` 在 import 期生效且可拒绝启动
- [ ] `dispatch_agent_run` 六参数契约 + 完成 webhook 幂等(**无** GitHub 触发源);`disable_loopback=false` 下 run 终结经**进程内** POST 到达 `/webhooks/run-complete`(`?token=` 错误 → 401/403),全程无公网回调
- [ ] 静态 token 认证:带 token 200 / 缺 token 401;开放本地模式仅在 `OPEN_SWE_ALLOW_ANON=1` 生效
- [ ] 沙箱 `SANDBOX_TYPE=local`,无 GitHub token 注入;`git config user.name/email` 未被强制覆盖(commit 作者 = 宿主身份)
- [ ] 一次 `agent` run 能:改文件 → `git commit` 到 `local/<slug>` 分支 → 汇报本地分支名,**不** push、**不**建 PR
- [ ] `/state`(单快照)与 `/history`(≤5 + 90s + `/state` 降级 + 合并)代理;超大线程(≥30MB checkpoint)下打开线程页 P95 < 10s,期间其他请求不冻结 > 1s(queue-probe `lag_ms` 验证)
- [ ] 进程 `env` 与运行时内存中 grep 不到 `GITHUB_APP` / `GITHUB_TOKEN` / 任何 OAuth token 字段

---

## 13. 主要文件索引(本变体,删/留/改)

| 状态 | 文件 |
|---|---|
| 改 | `langgraph.json`(两图)、`agent/api/app.py`(auth 钩子 + 路由裁剪)、`agent/prompt.py`(本地提交段)、`agent/server.py`(沙箱去 proxy + 工具列表)、`agent/dispatch.py`(source 收窄)、`agent/completion.py`(失败回执简化)、`agent/scheduler.py`(去 baby_sit/cost) |
| 留 | `agent/webapp.py`、`agent/utils/event_loop.py`、`agent/postgres_persistence.py`、`agent/input_messages.py`、`agent/utils/sandbox.py`、`agent/integrations/local.py`、`agent/utils/sandbox_paths.py`、`agent/utils/queue_debug.py`、`agent/api/health.py`、`agent/dashboard/{thread_api,plan_api,plan_store,workflow_approval_api}.py`、`agent/middleware/{sanitize,tool_error,exclude_tools,model_call_limit,model_call_timeout,strip_thoughts,prepare_run}.py`、`agent/tools/{web_search,fetch_url,http_request,save_plan,approve_plan,enter_plan_mode,schedule_thread_wakeup,background_execute,background_task,recreate_sandbox,sandbox_reset,report_platform_issue,read_user_settings,save_user_instructions,save_user_skill,delete_user_skill,output_iframe,read_repo_file*,search_repo_code*}.py` |
| 改语义 | `agent/local_auth.py`(从死代码→接上,作为认证入口)、`agent/utils/repo.py`(仅本地仓库解析) |
| 删 | `agent/{reviewer,analyzer,chat,baby_sit,session_cost}.py`、`agent/review/*`、`agent/graphs/{reviewer,analyzer,chat}.py`、`agent/utils/{github_app,github_checks,github_ci,github_comments,github_feedback,github_http,github_org_membership,github_proxy,github_token}.py`、`agent/middleware/{refresh_github_proxy,pr_creation_guard,workflow_push_guard,settle_review_check}.py`、`agent/webhooks/{slack,linear,github,common}.py`(留 run-complete)、`agent/dashboard/{oauth,github_token_auth,oidc_auth,slack_oauth,notion_oauth,user_mappings,review_api,review_styles,review_style_jobs,review_chat_api,pr_diff,pull_request_status,agent_usage,repo_cache,repo_snapshots,enabled_repos,team_credentials,team_settings,profiles*}.py`、`agent/tools/{open_pull_request,request_pr_review,manage_baby_sit,notify_automation_channel,fetch_review_diff,publish_review,add_finding,update_finding,list_findings,read_finding_outcomes,reply_to_finding_thread,resolve_finding_thread,save_review_style,list_review_findings,admin_gate,linear_*,slack_*}.py`、`agent/integrations/{langsmith_sandbox 的 GitHub 代理相关,daytona,modal,runloop,e2b}.py`(只留 `local.py`)、`agent/integrations/{linear,notion_mcp,langsmith_tools 的 GitHub 相关}.py`、`test_github_oauth.py` |

> 注:上表"删"里的集成件(daytona/modal/e2b 等)若你希望保留多 provider 沙箱,可只删其 GitHub 代理接线、保留 provider 本体;本变体最简形态是 `local` 单一 provider。

---

## 14. 实现顺序建议(给 AI 编程代理)

1. **锁骨架**:改 `langgraph.json` 两图;`agent/api/app.py` 换 auth 钩子(§2)+ 裁剪路由;确保空实现也能 `langgraph dev` 起 + `/health` 200。
2. **打通本地沙箱 + 本地提交**:`SANDBOX_TYPE=local`;`agent/server.py` 去 proxy 链;接 `LOCAL_COMMIT_SECTION` 提示;跑通一次"改文件→commit 到 `local/<slug>`"。
3. **接持久化与有界代理**:Postgres checkpointer/store(§7)+ `thread_api.py` 有界 state/history(§9.2)。
4. **接完成 webhook**(§8)+ `scheduler` 裁剪(§4.2)。
5. **提示词收尾**(§10)+ env 清单(§11)。
6. **逐条过 §12 验收清单**,尤其"超大线程不冻结"与"内存无 GitHub 凭据"两条。

---

## 15. 实现清单:路由 / Webhook / HTTP 字段盘点

> 本节是**可直接照做的实现契约**:§15.1 是"运行时免费提供、只需调用"的协议路由(含字段级契约);§15.2 是"必须自己实现"的 dashboard 路由(按活服务抓取的 92 端点逐条归类:留/改/删);§15.3 是 webhook 端点;§15.4 是贯穿所有端点的 HTTP 协议字段。
> 基线:活服务 `:2024` OpenAPI(148 paths = 92 dashboard + 56 协议/其他)。

### 15.1 协议路由(`langgraph_api` 提供,不实现,只消费)

**不需要写的代码**:起 `langgraph dev` 即全部就位。列出实际被用到的子集与契约,实现 dashboard 侧时按这些字段调。

**threads(核心)**

| 端点 | 用途 | 请求/响应契约 |
|---|---|---|
| `POST /threads` | 建线程 | body:`{"thread_id"?: uuid, "metadata": {…业务字段…}}`;resp:`{"thread_id", "created_at", …}` |
| `GET /threads/{id}` | 取线程 | resp 含 `thread_id/status/metadata/created_at/updated_at` |
| `PATCH /threads/{id}` | 改 metadata | body:`{"metadata": {…}}`(merge 语义) |
| `DELETE /threads/{id}` | 删线程(+其 checkpoint) | 200/204 |
| `POST /threads/search` | 按 metadata 过滤搜线程 | body:`{"metadata": {"kind": …, "github_login": …}, "limit", "offset", "sort_by": "updated_at", "sort_order": "desc"}`;resp:`[thread…]` |
| `GET /threads/{id}/state` | **单快照读**(主 transcript 来源) | query 可选 `subgraphs`;resp:ThreadState = `{"values": {…state 通道,含 messages…}, "next": […], "configurable": {"thread_id", "checkpoint_id", "checkpoint_ns"}, "metadata": {…}}` |
| `GET /threads/{id}/history` | 有界历史 | query:`limit` / `before` / `after` / `filter`;resp:`[{checkpoint_id, parent_checkpoint_id, values, metadata, timestamp, next}, …]` |
| `POST /threads/{id}/history` | 同上的 POST 形式 | body 同上字段 |
| `GET /threads/{id}/stream` | **SSE join 线程流** | query:`last_event_id`(重连续播)、`stream_mode`;SSE 帧,可断线重放(`stream_resumable`) |
| `POST /threads/{id}/stream/events` | **v2 事件流**(前端 `StreamProvider` 主消费口) | body 同 stream 参数;SSE,v3 协议(由 `__event_streaming_v2` 标记启用,含 tools/lifecycle 通道) |

**runs(触发契约,§5/§6 的落地字段)**

| 端点 | 用途 |
|---|---|
| `POST /threads/{id}/runs` | **建 run(durable 触发)**——`create_durable_run` 就是打这里 |
| `POST /threads/{id}/runs/stream` | 建 run 并直接 SSE 流 |
| `POST /threads/{id}/runs/wait` | 建 run 并同步等到终态 |
| `GET /threads/{id}/runs` | 列 runs(query `limit`) |
| `GET /threads/{id}/runs/{run_id}` / `DELETE` | 查/删 run |
| `POST /threads/{id}/runs/{run_id}/cancel` | 取消 run |
| `GET /threads/{id}/runs/{run_id}/stream` | 单 run SSE join(query `last_event_id`、`stream_mode`) |
| `POST /threads/{id}/commands` | **SDK command 信封**(见 §15.4.4)——dashboard 消息注入走这里 |
| `POST /threads/{id}/runs/crons` 及 `POST/GET/PATCH/DELETE /runs/crons…` | **cron 面**——`scheduler` 图所有定时任务(`reconcile`/`background_tasks`/用户 Automations)都经这套创建/查询 |

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

**store(长期记忆,`AsyncPostgresStore`)**:`PUT/GET/DELETE /store/items`(body `{"namespace": […], "key": str, "value": {…}}`)、`POST /store/items/search`、`POST /store/namespaces`。

**其余协议端点**(`assistants/*`、`/mcp/`、`/a2a/*`、`/store/namespaces`、`/runs/stream|wait|batch|cancel` 无线程形态):**不用**,不消费、不管。

**SSE 兼容性验收标准**(不逐帧抄 schema):前端 `@langchain/react` 的 `StreamProvider`/`StreamController` 与 Python 侧 `langgraph-sdk 0.4.2` 都能无感消费;`stream_resumable=True` 的 run 在晚连接/断线后用 `last_event_id` 重放;子代理事件带命名空间(`stream_subgraphs`)。

### 15.2 必须实现的 dashboard 路由(`/dashboard/api/*`)

基线 92 端点。**留**=原样移植(字段照抄现有实现);**改**=换认证/换判据;✂️=删除(依赖 GitHub/PR/Slack/Linear/review/团队多用户)。

**A. 认证(改)**

| 端点 | 处理 | 契约 |
|---|---|---|
| `GET /auth/login`、`GET /auth/callback`、`POST /auth/logout`、`POST /auth/desktop/exchange`、`GET /slack/login`、`GET /slack/callback`、`GET /notion/login`、`GET /notion/callback` | **全部删除**,替换为: | — |
| `GET /auth/token-login`(新增) | 静态 token 换会话 cookie | query `token`;校验 `OPEN_SWE_LOCAL_AUTH_TOKEN`(`hmac.compare_digest`)→ 下发 `osw_session`(HMAC 签名,含 `identity="local-user"`、过期时间);错/缺 → 401。未设 token 且 `OPEN_SWE_ALLOW_ANON=1` → 直接发 `anon` 会话 |
| `GET /me`、`GET/PUT/DELETE /me/instructions`、`GET/PUT /profile` | **留**(判据改本地身份) | `/me` resp:`{"sub": "local-user", "email"?: null, "name": "local-user", "admin": true}`;`/me/instructions` body 任意文本(用户级指令,§跨线程共享面) |
| `GET /my-mapping`、`GET/DELETE /admin/user-mappings*`、`GET /repos`、`GET /team-credentials*`、`GET/PUT /team-settings*`、`PUT /team-settings/transcription`、`GET/PUT/DELETE /my-credentials/*`(currents/langsmith/notion) | ✂️ 删 | — |

**B. 线程核心(留,变体主战场)**

| 端点 | 契约(请求 → 响应) |
|---|---|
| `GET /threads` | query:`limit(≤100, 默认25)`、`offset`、`resolved?`、`viewed?`、`source?`、`status?`、`query?`(模糊)、`scope=all\|interactive\|automation`、`automation_id?`、`sort_by=updated_at\|created_at`。实现:对 `POST /threads/search` 加 `metadata.local_user` 过滤(单用户下恒真)分页封装。resp:`{"items": [ThreadListItem], "limit", "offset", "hasMore": bool}` |
| `GET /threads/page`、`GET /threads/sidebar` | 同上的变体(分页/侧栏精简字段);留 |
| `GET /threads/{id}` | **元数据详情**(transcript 不在这——前端另调 `/state` hydrate)。resp:`ThreadListItem` 全字段 + `{"isOwner": true, "status", "latest_run_id"?, "links": {"web","plan"?,"trace"?}}`;404 归一化:非本用户线程 → 404(变体下恒真放行) |
| `ThreadListItem` 字段 | `id, title, repo, repoFullName, branch, model, effort, planMode, planStatus, adminThread, source, origin, threadCategory, triggerKind, automationId?, automationName?, status(idle\|running\|error\|interrupted), viewed, viewedAt, createdAt, updatedAt, lastUserMessage?` |
| `DELETE /threads/{id}` | 删线程 + 其 checkpoint/流;resp 200/204 |
| `POST /threads/{id}/resolve` | body `{"resolved": bool}` → metadata 打标;resp 更新后的列表项 |
| `POST /threads/{id}/cancel` | 取消当前 run(经 `runs/{id}/cancel`);resp `{"cancelled": bool}` |
| `POST /threads/{id}/runs/{run_id}/cancel` | 同上,指定 run |
| `POST /threads/{id}/messages` | **发消息=触发 run**。body(`ThreadMessageBody`):`{"content": str(≤20000), "images": [{data, media_type, filename?}], "model_id"?, "effort"?, "plan_mode": bool}`。实现:构造 `build_run_input` 信封(source=`web`,sender=本地身份)→ `dispatch_agent_run` → `create_durable_run`(§15.1 runs 契约)→ resp:`{"run_id", "thread_id", "queued": bool}` |
| `POST /threads/{id}/commands` | **SDK command 信封透传 + 富化**(见 §15.4.4):raw JSON body;服务端在 `run.start` 时注入 `configurable`(`local_user`、`repo`、`plan_mode`、`prepare_run_id`、模型覆盖)与 `metadata`(`source="dashboard"`)→ POST 到运行时 `/threads/{id}/commands` → 回传响应(`{"type":"success","run_id"?}` / `{"type":"error","error":…}`);404 归一化:线程不存在且 method≠run.start → 404 |
| `POST /threads/{id}/history` | **有界代理**:raw body(`{"limit"?, "before"?, …}`)→ 无过滤时 `limit` 强制 ≤5(`_DISCOVERY_HISTORY_LIMIT`)→ 90s 预算 → 失败退避(`{limit:1}` 重试 3 次 ×1.5s → 降级单快照)→ resp 透传运行时 history 数组;超时 → **503** `{"detail": "thread history request timed out; retry"}` |
| `GET /threads/{id}/state` | **有界代理**:15s 预算单快照 → resp 透传 ThreadState;线程不存在/从未 run 的归一化:`{"values": {}, "next": []}`;超时 → 503 |
| `GET /threads/{id}/stream` | **SSE 透传** join 线程流:上游 `GET /threads/{id}/stream`(带 `last_event_id`),权限校验后字节流回传;客户端断开必须释放上游订阅(流订阅回收) |
| `POST /threads/{id}/stream/events` | **SSE 透传** v2 事件流(前端 `StreamProvider` 主口);同上回收语义 |

**C. Plan(留,4+3 端点)**

`GET/PUT /plan/{thread_id}`(body `{"content", "content_format": "html"|"markdown"}`)、`POST …/approve`、`POST …/reject`、`GET/POST /plan/{thread_id}/comments`(body `{"content"}`)、`DELETE …/comments/{comment_id}`。存储:store 命名空间 `plans`;approve/reject 只是状态翻转,审批动作由**前端**在收到 approve 后发 `run.start`(带 `plan_mode=false`)——服务端不自动跑。

**D. Workflow 审批(留)**

`GET /workflow-approval/{thread_id}`(resp `{"approvals": [{fingerprint, message, status, created_at}]}`)、`POST …/{fingerprint}/approve`、`POST …/{fingerprint}/reject`。存储:thread metadata(`workflow_approvals`);`POST /threads/{id}/commands` 的 `run.start` 若命中 pending 审批由前端先走这里。

**E. Automations/schedules(留,4 端点)**

`GET /schedules`(query `limit/offset`;resp 列表 `[{id, name, cron, prompt, enabled, next_fire_at, last_result?}]`)、`POST /schedules`(body `{"name", "cron", "prompt", "enabled"}`)→ 建 cron(`POST /runs/crons` 打 `scheduler` 图,`task` 缺省 = 定时 agent run)、`PATCH/DELETE /schedules/{id}`、`POST /schedules/{id}/trigger`(立即触发一次)。

**F. Skills / repo instructions(留)**

`GET/POST /skills`、`PUT/DELETE /skills/{name}`(用户技能,store);`GET/POST /organization-skills`、`PUT/DELETE …/{name}`(组织技能,本地单用户可并入用户技能或保留双命名空间);`GET/POST /agent-instructions`、`GET/PUT/DELETE /agent-instructions/{full_name}`(per-repo 指令,注入系统提示)。

**G. 模型选项(留)**:`GET /options` → `{"models": [{id, label, effort_options[]}]}`(来自 `SUPPORTED_MODEL_IDS`)。

**H. Health/debug(留)**:`GET /health` → `{"status":"healthy"}`、`GET /ok` → 200、`GET/POST /debug/queue-probe`(事件循环 lag 探针,`OPEN_SWE_QUEUE_DEBUG=1` 时有数据;resp `{"lag_ms", "loop_thread", …}`)——§12 验收"不冻结 >1s"就靠它。

**I. 可选留(本地 git 相关,默认不做,需要时加回)**

`GET /threads/{id}/working-tree-diff`(沙箱内 `git diff` 物化,供前端 diff 面板;纯本地能力,不依赖 GitHub)、`GET /threads/{id}/branch-diff`。`GET /threads/{id}/terminal/connect` ✂️ 删(桌面终端)。

**J. 删除清单(✂️,共 60 端点)**

`/admin/*`(evals/threads cancel/user-mappings)、`/reviews/*` 全部(含 `…/chat*` 8 个 PR 聊天代理)、`/review-styles/*`、`/enabled-review-repos`、`/repo-snapshots*`、`/sandbox-settings`、`/environments*`、`/voice/*`、`/agent-usage-leaderboard`、`/threads/{id}/{pr-diff, pull-request-status, recovery.patch}`、A 节所列全部 OAuth/凭据/团队端点。

### 15.3 Webhook 端点

| 端点 | 方向 | 处理 |
|---|---|---|
| `POST /webhooks/run-complete?token=…` | 运行时 →(loopback 或真实网络)→ 本服务 | **留,唯一保留的 webhook**。鉴权:`token` query 与 `RUN_COMPLETE_WEBHOOK_SECRET` 比对(`hmac.compare_digest`);secret 未设 → **503**(fail-closed);token 错 → 401。body 见 §15.4.5。处理:`handle_run_completion`——按 `run_id` 幂等;`status=success` → 记日志;失败态 → 按 `thread_id` 取 metadata,落 `failure_ack` 标记 + `latest_run_status`,resp `{"status": "handled"|"deduped"}` |
| `POST /webhooks/github`、`GET/POST /webhooks/slack`、`POST /webhooks/slack/interactivity`、`GET/POST /webhooks/linear` | 外部平台 → 本服务 | ✂️ **全删**(§0/§5;无 GitHub/Slack/Linear 触发源) |

### 15.4 HTTP 协议字段盘点

**15.4.1 认证与通用头**
- 协议面(`:2024` 非 `/dashboard/api` 路由):`Authorization: Bearer <OPEN_SWE_LOCAL_AUTH_TOKEN>`(接 `agent/local_auth.py` 的 `Auth.authenticate`,身份 `local-user`);
- dashboard 面:会话 cookie `osw_session`(§15.2-A 下发,`HttpOnly`、`SameSite=Lax`、`Path=/`);跨 origin 时 CORS:`Access-Control-Allow-Credentials: true` + 具体 origin 白名单(`DASHBOARD_ALLOWED_ORIGINS`;**禁止 `*`+credentials**);
- 全部 JSON 端点:`Content-Type: application/json`(proxy 端点显式校验,非 JSON → 415);
- 404 归一化:非 owner/不存在的线程一律 404(不泄漏存在性);503 仅用于"有界读超时"(body `{"detail": "… timed out; retry"}`)。

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
- `Content-Type: text/event-stream`;帧 `id: <event_id>\nevent: <type>\ndata: <json>\n\n`;
- 通道:`values`/`updates`/`messages`/`custom`/`tasks`/`checkpoints`(v2 标记下另含 tools/lifecycle 与子代理命名空间事件);
- 断线重连:客户端带 `Last-Event-ID` 头(或 `last_event_id` query)→ 服务端从该 event 重放(依赖 `stream_resumable=True`);
- **验收口径**:用 `langgraph-sdk 0.4.2` 的 `client.runs.stream` 与前端 `StreamProvider` 双端消费通过,不逐帧比对 schema。
