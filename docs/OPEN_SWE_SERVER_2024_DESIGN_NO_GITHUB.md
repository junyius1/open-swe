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
- 完成 webhook 地址解析的 **fail-closed 逻辑保留**(loopback/相对 URL 降级为不挂 + 告警)——这是 inmem 的硬约束,与 GitHub 无关。

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

差异:
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

`state/history` 代理的**有界 + 合并 + 退避三层防御**(90s/15s/30s 超时、`_DISCOVERY_HISTORY_LIMIT=5`、`_FALLBACK_ATTEMPTS=3`、30s 快照缓存、32 合并)**逐字保留**——这是单循环性能事故的防线,与 GitHub 无关。

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
# 完成 webhook(fail-closed;loopback 会自动降级为不挂)
COMPLETION_WEBHOOK_URL=http://localhost:2024/webhooks/run-complete
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
- [ ] `dispatch_agent_run` 六参数契约 + 完成 webhook 幂等(**无** GitHub 触发源)
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
