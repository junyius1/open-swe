# Open SWE「2024 服务器」程序设计书

> 目标读者:AI 编码代理 / 熟悉 LangGraph 的工程师。
> 范围:复刻 `localhost:2024` 上运行的 Open SWE agent 服务器(in-memory LangGraph runtime 版,下称 **2024 服务器**)的架构、数据契约与关键行为。
> 依据:`langchain-ai/open-swe` 仓库 `local` 分支。文中模块路径、函数名、SQL、env 均取自真实代码,可逐条验证。

---

## 1. 总览:2024 服务器是什么

**2024 服务器 = `langgraph dev`(in-memory 运行时,包名 `langgraph-api` / `langgraph-runtime-inmem`)起在 `localhost:2024` 的单个 Python 进程。** 它同时承担三件事:

1. **LangGraph runtime**:提供 LangGraph SDK 协议的全部 HTTP 面(`POST /threads/{id}/runs`、`GET /threads/{id}/state`、`POST /threads/{id}/history`、`/runs/{id}/stream/events` SSE、threads CRUD/search……),并**执行注册的 graph**(即 agent 本体)。
2. **Open SWE 业务 API**:通过 `langgraph.json` 的 `"http": {"app": "agent.webapp:app"}` 把 open-swe 自己的 FastAPI app **挂进同一个进程**(`/dashboard/api/*`、`/webhooks/*`、`/health/*`、`/debug/*`)。
3. **持久化宿主**:checkpointer 与 store 由 `agent/postgres_persistence.py` 提供,指向 Postgres(`DATABASE_URI`)。

进程模型(单进程、单事件循环):

```
langgraph dev  (localhost:2024, 单 uvicorn worker)
├── inmem runtime: REST/SSE 协议面 + run 队列 + stream 管理
│    └── 执行 5 个注册 graph:agent / reviewer / analyzer / chat / scheduler
├── FastAPI app (agent.webapp:app → agent.api.app:app)
│    ├── /dashboard/api/…  线程 CRUD、消息、state/history 代理、plan、workflow
│    ├── /webhooks/{slack,linear,github,run-complete}  外部触发与完成回调
│    ├── /health, /debug/queue-probe
├── checkpointer → AsyncPostgresSaver (checkpoint_blobs/checkpoints/checkpoint_writes)
└── store          → AsyncPostgresStore (长期记忆:设置、findings、队列等)
```

前端 UI(TanStack Start,`:3000`)通过 `VITE_DASHBOARD_API_BASE_URL` 跨源访问 `:2024` 的 `/dashboard/api`(dev 下 CORS 由 `DASHBOARD_ALLOWED_ORIGINS` 放行)。

外部依赖:Postgres 16(checkpoint + store + 业务表)、Redis(`docker-compose.infra.yml`,队列/缓存)、GitHub(App token + `gh` 沙箱代理)、可选 LangSmith(追踪)、沙箱 VM(每线程一个 Linux 容器/VM,独立于本进程)。

**与 LangGraph Platform(云版)的区别**:协议完全一致(SDK 客户端无感),但运行时是自托管的 inmem 版;因此 open-swe 代码里"平台"行为(如 webhook 拒绝 loopback URL、`runs.create` 参数)都按 inmem 的实际约束适配。

---

## 2. 服务器配置:`langgraph.json`

```jsonc
{
  "python_version": "3.12",
  "api_version": "0.12.6",                       // 锁定 langgraph-api 大版本
  "graphs": {
    "agent":     "agent.graphs.agent:traced_agent",
    "reviewer":  "agent.graphs.reviewer:traced_reviewer_agent",
    "analyzer":  "agent.graphs.analyzer:traced_analyzer",
    "chat":      "agent.graphs.chat:traced_chat_agent",
    "scheduler": "agent.graphs.scheduler:get_scheduler"
  },
  "http": { "app": "agent.webapp:app" },          // 业务 FastAPI 挂进同一进程
  "checkpointer": {
    "path": "agent/postgres_persistence.py:create_checkpointer",
    "ttl": { "strategy": "delete", "sweep_interval_minutes": 60, "default_ttl": 43200 }
  },
  "store": { "path": "agent/postgres_persistence.py:create_store" },
  "env": ".env"
}
```

要点:
- `agent/graphs/*.py` 全是 **3 行 re-export shim**(如 `from agent.server import get_agent, traced_agent`),真正工厂在 `agent/server.py`、`agent/reviewer.py` 等。`traced_*` 是包了 LangSmith tracing 的工厂。
- **checkpointer TTL = 12h,删除策略,每 60min 清扫**——不活跃线程的 checkpoint 会被整体删除(不是截断)。
- 无 `DATABASE_URI` 时 `create_checkpointer/create_store` 降级为 `InMemorySaver/InMemoryStore` 并告警(进程重启即丢状态)。

---

## 3. 铁律:单事件循环(`pin_single_event_loop`)

`agent/utils/event_loop.py` —— **全进程只允许一个事件循环**,`agent/api/app.py` 在模块 import 时就调用 `pin_single_event_loop()`,且再次在 lifespan 里调用。

原因(代码注释原文语义):
- 沙箱后端按 thread 缓存在**进程级全局注册表**里,循环亲和(loop-affine)。
- inmem 的后台队列若开 `BG_JOB_ISOLATED_LOOPS`(每个 worker 独立循环),一个 run 留在某循环的注册表状态,下一个 run 换了循环就取不到 → **线程永久卡死**。
- 所以:强制 `BG_JOB_ISOLATED_LOOPS=false`;若 `langgraph_api.config` 已没有这个开关(运行时模型变了),**直接 RuntimeError 拒启动**——"跑在隔离循环上比不启动更糟"。

**推论(性能事故根)**:所有 HTTP 处理、graph 执行、checkpoint 反序列化都共享这一个循环;任何同步 CPU 重活(如大 blob 反序列化)会**冻结整个进程**,包括其他请求、SSE 流、健康检查。这决定了 §9 的所有"有界读"设计。

---

## 4. 图工厂:五个 graph 的规格

### 4.1 共同底座
四个 agent 图(`agent/reviewer/analyzer/chat`)都用 **`deepagents.create_deep_agent`** 构建(deepagents = LangChain 的"带文件系统+子代理+摘要"的 agent 框架)。共同特征:
- `system_prompt=""`:提示词**运行时**由 `Prepare*RunMiddleware` 渲染注入 `rendered_system_prompt` 通道(每 run 动态,支持 source/sender/仓库/环境/用户指令/Linear 工单等变量)。
- 中间件栈(顺序即执行顺序),公共件:`SanitizeToolInputsMiddleware`、`ToolErrorMiddleware`、`ExcludeToolsMiddleware`、`ModelCallLimitMiddleware(run_limit=…, exit_behavior="end")`、`ModelCallTimeoutMiddleware`、`StripThoughtsMiddleware`(模型返回后、写 checkpoint 前剥离 thinking 块,保留带 `signature` 的 Anthropic thinking)。
- 模型解析:`make_model(model_id, effort)` 支持多 provider(anthropic/openai/fireworks/gateway),`ttl_cache` 缓存;桌面(local)run 与 Web run 走不同设置解析。

### 4.2 `agent`(主编程 agent,`agent/server.py` 工厂)
- **后端:每线程一个沙箱**(`ensure_sandbox_for_thread(thread_id, repo, environment_slug)` → `SandboxBackendProtocol`)。沙箱 = 独立 Linux 环境,预置 `gh` 代理鉴权;`backend.start()` 在工厂里调用,run 结束不销毁(线程复用)。
- 工具:deepagents 文件系统工具 `read_file/write_file/edit_file/delete/glob/ls/grep` + `task`(子代理)+ 动态集成工具(GitHub/Linear/Slack/Corridor 等按 thread 设置装载)。
- 子代理:`general-purpose`(完整沙箱工具)+ 可选 `browser`(Chromium 自动化,有工具时才挂)。
- 特性:plan mode(状态通道 `plan_mode`/`plan_approval_blocked`)、skills(`skills_metadata` 通道)、repo instructions、Linear 工单上下文、desktop run(桌面端,backend 换 `create_desktop_backend`)。

### 4.3 `reviewer`(PR 审查 agent,`agent/reviewer.py`)
- 与主 agent 同构(也有沙箱:checkout 仓库 + `fetch_review_diff` 物化 diff),但**职责固定**:review 一个 PR 并发布单次 review。
- 首轮/复审/finding 回复三套上下文构建(`_build_first_review_context/_build_re_review_context/_build_finding_reply_context`),findings 存 store 并结构化(`severity/confidence/status`)。
- 线程 id 确定性:`reviewer_thread_id(owner, repo, pr_number)`。
- 失败收尾:completion webhook 里 `_settle_failed_reviewer_check` 把卡住的审查标记失败。

### 4.4 `chat`(review 页"chat with this PR",`agent/chat.py`)
- **无沙箱**。PR 上下文(diff/findings/overview)由 dashboard 代理 seed 为**虚拟文件** `/pr/{overview.md,diff.patch,findings.md}`,走 `FilesystemMiddleware` 只读白名单(`read_file/ls/glob/grep`)。
- 工具:`read_repo_file(path, ref)`、`search_repo_code(query)`、`list_review_findings(status_filter)`、`web_search`、`fetch_url`。
- `_EXCLUDED_TOOLS = {execute, write_file, edit_file, delete}` 由 `ExcludeToolsMiddleware` 剥掉;general-purpose 子代理也显式覆盖为只读。
- `CHAT_MODEL_CALL_LIMIT = 100`;仓库坐标 + reviewer thread id 在 `configurable`;GitHub 访问用 **repo 级 GitHub App token**,绝不收用户凭据。
- assistant id = `chat`,线程 metadata `kind = "review_chat"`,dashboard 侧有独立线程列表/删除/404 归一化(未运行过的线程 history 返回 `[]`、state 返回 `{"values":{},"next":[]}`)。

### 4.5 `scheduler`(cron 扇出,`agent/scheduler.py`,唯一不用 deepagents 的图)
- `StateGraph(SchedulerState)`,单节点 `launch`,按 `task` 分派:
  - `reconcile` → `reconcile_stale_runs()`(清扫僵尸 run)
  - `baby_sit` → `evaluate_watch(watch_key)`(CI 监视/重跑 flaky)
  - `background_tasks` → `monitor_background_tasks(thread_id)`
  - `session_cost` → `run_session_cost_refresh(state)`
  - 默认 → `launch_scheduled_agent_run(schedule_id)`(定时/唤醒:建/复用 agent 线程并 dispatch)
- 由 LangGraph 的 cron 机制周期性触发(每个 tick 一次 run)。

---

## 5. 输入契约:XML 信封(`agent/input_messages.py`)

**所有入站消息统一序列化为用户消息里的 XML 信封**,让模型以结构化方式感知发送者/渠道/系统:

```xml
<input-message sender="github:login" surface="web" kind="human">
  <content>…正文(或 content blocks)…</content>
</input-message>
```

- `RunInput = {"messages": [RunMessage], "files"?: {...}}`;`RunMessage = {"role": "user"|"system", "content": str | blocks, "id"?}`。
- `build_run_input(content, context, people, channels, systems, files)`:先注入**身份介绍消息**(person/channel/system 的 `<dynamic-context>` 块,按内容 hash 去重,`injected_dynamic_context_hashes` 防止重复注入),再注入真正的 `human_input`/`system_input`。
- `Surface = slack|linear|github|web|desktop|automation|eval`;`kind = human|system`;无人类发送者时合成 `system:<source>` 身份。
- 系统提示经 `wrap_system_prompt` 包进 `<system-instructions format="open-swe-v1">`,追加项以增量 `<input-message>` 形式 splice 进去(幂等)。
- `append_message_data` 支持对已序列化信封**追加 data 字段**(如 Slack permalink)。

---

## 6. 触发契约:`agent/dispatch.py`

**一切触发(Slack/Linear/GitHub/dashboard/scheduler)都走同一个 durable 契约**:`dispatch_agent_run(thread_id, content, configurable, source=…)` → `create_durable_run(...)`。

`create_durable_run` 的固定参数(复刻时必须一致):

| 参数 | 值 | 原因 |
|---|---|---|
| `multitask_strategy` | `"interrupt"`(后台 follow-up 可 `enqueue`) | 新消息**打断**活跃 run:同步 checkpoint 已存住进度,新 run 带全量历史 + 新消息重启 agent |
| `durability` | `"sync"` | **每步之前**写 checkpoint,崩溃/回收从最后 checkpoint 恢复 |
| `if_not_exists` | `"create"` | 线程不存在时自动建 |
| `stream_mode` | `["values","updates","messages","custom","tasks","checkpoints"]` | Protocol v2 全通道(REST 不接受 `tools/lifecycle`,它们由 v2 标记隐式启用) |
| `stream_subgraphs` | `True` | 子代理命名空间事件 |
| `stream_resumable` | `True` | 事件流可重放——**晚连接的 dashboard 才能看到别人发起的 run**(否则外部触发的 run 在 Web 里看起来"空闲") |
| `configurable.__event_streaming_v2` | `True` | v3 流协议标记(镜像 `langgraph_api.event_streaming` 的 key,不 import 它) |
| `configurable.prepare_run_id` | 新 UUID | 关联 PrepareRun 中间件与 run |
| `webhook` | `COMPLETION_WEBHOOK_URL?token=…` | 完成/失败回调(见 §8) |

其他:
- `_dispatch_input` 把 `source` 映射为 `Surface`(未知 → `automation`),从 `configurable` 里抽发送者身份(Slack thread 的 user/channel/时区;GitHub login;Linear email)。
- **webhook 地址解析是 fail-closed**:secret 未设 → 不挂 webhook;URL 相对/loopback → inmem 会 422 掉**每个** run 的创建,所以降级为不挂并告警(完成回执是 best-effort,绝不能拖垮 run 创建)。

---

## 7. 持久化与数据模型

### 7.1 Checkpoint 三表(AsyncPostgresSaver 建)

```sql
checkpoints(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
            type, checkpoint jsonb, metadata jsonb)          -- 每步一行;channel_values 只存非 blob 通道
checkpoint_blobs(thread_id, checkpoint_ns, channel, version,  -- 主键即此 4 列
                 type, blob bytea)
checkpoint_writes(thread_id, checkpoint_ns, checkpoint_id,
                 task_id, task_path, idx, channel, type, blob)
```

`checkpoints.checkpoint` jsonb 内含 `channel_versions: {channel → version}`,blob 按 `(channel, version)` **内容寻址去重**。

**messages 通道 = DeltaChannel(增量通道)**,存储策略:
- **每 ~150 superstep 落一个 `_DeltaSnapshot` 全量 blob**(msgpack ext 编码;`_DeltaSnapshot.value` = 截至当时的完整消息列表);
- 其余 checkpoint 的 `channel_values.messages` 为 `MISSING`,只靠 `checkpoint_writes` 里每步追加的 1 条消息**回放重建**;
- 读取:`aget_delta_channel_history` 两阶段 SQL(stage-1 沿 parent 链找最近 snapshot;stage-2 一次 UNION ALL 拉全部 writes + seed blob),Python 回放;
- 实测量级(3583 步、1186 条消息的线程):blobs 23 行/29.3MB(占 98%),writes 1190 行/3MB,checkpoints 3583 行/54.9MB(行内含 `rendered_system_prompt` ~24KB)。

**性能不变式(必须遵守)**:
- `_load_blobs` 是**同步**的,跑在唯一事件循环上(只有 `_load_writes` 走 `to_thread`)。
- 无快照 checkpoint 的**每个**读取都要重拉 + 重解一份完整 seed 快照(实测尾部 ~0.65s/个 → 全量 history 遍历 ≈ 19 分钟)。
- ⇒ **任何面向用户的读都必须有界**:单快照读(`/state`)或 ≤5 的 history 窗口;客户端超时**不会**取消服务端遍历,会堆积阻塞循环。

### 7.2 线程/Run 元数据(store + thread metadata jsonb)
- thread `metadata`(LangGraph threads 表)承载业务状态:`kind`(agent/review_chat/reviewer/…)、`github_login`、`repo`、`sandbox_id`、`latest_run_id`、`source`、`viewed`、`resolved`、失败回执去重 flag、`prepare_run_id` 历史等。
- store(AsyncPostgresStore)承载:review findings、团队/用户设置、后台任务队列、schedules 等(命名空间按用途分)。

### 7.3 降级
`DATABASE_URI` 缺失 → `InMemorySaver/InMemoryStore` + 启动告警(只用于裸开发,状态不持久)。

---

## 8. 完成信号:`agent/completion.py` + `/webhooks/run-complete`

- 每个 dispatched run 挂 `webhook`;平台在 run 终结(成功/失败/超时)POST 回本服务。
- `verify_run_complete_token` 校验 `?token=`(fail-closed:secret 未设时路由直接 503)。
- `handle_run_completion`:
  - `success` → 入队 Slack 成本刷新(按 run_id 幂等)。
  - 终态失败 → 按 thread metadata 的 `kind` 走对应失败回复;**按 run_id 幂等去重**,`kind=thread_wakeup` 的自动化唤醒失败静默。
  - 设计意图:**任何 run 都保证以"一个信号"收尾**,即使 agent 死掉。

---

## 9. Dashboard 业务层(`agent/api/app.py` + `agent/dashboard/*`)

### 9.1 应用组装
- `create_app()`:lifespan 里 `pin_single_event_loop()`(二次)→ `validate_sandbox_startup_config()` → `validate_local_dev_llm_config()`;退出时 `close_cached_models()`。
- 路由:`dashboard_router`(117 个端点,`agent/dashboard/routes.py` ~2400 行)+ `plan_router` + `workflow_approval_router` + `linear/slack/github` webhook 路由 + `health_router`。
- `QueueDebugMiddleware` + `enable_from_env()`(`OPEN_SWE_QUEUE_DEBUG=1` 启用每秒事件循环 lag 探针,独立 daemon 线程打点,循环卡死时仍可观测)。
- CORS:`DASHBOARD_ALLOWED_ORIGINS` 配置;上游逻辑禁止 `*` + credentials 组合。

### 9.2 state/history 代理(超大线程防护,`agent/dashboard/thread_api.py`)
前端(`VITE_DASHBOARD_API_BASE_URL` + `/dashboard/api`)→ dashboard 代理 → runtime `:2024`。

| 机制 | 值 | 作用 |
|---|---|---|
| `_HISTORY_PROXY_TIMEOUT` | 90s | history 遍历预算 |
| `_STATE_PROXY_TIMEOUT` / `_GET_STATE_TIMEOUT` | 30s / 15s | 单快照读预算 |
| `_DISCOVERY_HISTORY_LIMIT` | **5** | 无 `before/checkpoint/metadata` 过滤的请求 limit 强制 ≤5 |
| `_FALLBACK_ATTEMPTS` / `_FALLBACK_RETRY_BASE_DELAY` | 3 / 1.5s | 超时后退避重试:`/history {limit:1}` → 仍败 → 包装 `/state` 单条 |
| `_FALLBACK_SNAPSHOT_CACHE_TTL_SECONDS` | 30s | 降级快照按线程缓存 |
| `_HISTORY_COALESCING`(prune at 32) | — | 相同 payload 的并发请求合并为一次上游调用 |

- 前端 hydrate(`@langchain/react` 的 `StreamController`):主 transcript 用 **`GET /state`**;history 只用于有界发现页(limit 20 / limit 1 / 至多一次 `before` 回退)——**不做全量翻页**。
- review 聊天代理(`review_chat_api.py`)与主代理**同一套 clamp**。
- **已知边界**:客户端超时/放弃不取消服务端已入队的遍历;被放弃的遍历仍占用唯一事件循环,后续请求(含 fallback)排在其后——所以"有界 + 合并 + 退避"是三层防御。

### 9.3 认证/授权
- 会话 cookie(`_SESSION_DEP`);thread 级权限:`_assert_thread_owner/_assert_thread_readable`(owner=metadata 的 `github_login` 或 email 匹配)。
- 公开仓库的 webhook 触发有 org 门 + 自动审查门(`_is_repo_auto_review_enabled` 等)。

---

## 10. Webhook 触发面(`agent/webhooks/*`)

- `slack_routes / linear_routes / github_routes`:各自验签 → 权限门 → 解析发送者身份 → `dispatch_agent_run(source="slack"|"linear"|"github")`。
- 线程 id 确定性生成:Linear issue → `generate_thread_id_from_issue`;GitHub issue → `generate_thread_id_from_github_issue`;PR 审查 → `reviewer_thread_id(owner, repo, pr)`。**同一工单/PR 的多条消息落到同一线程**(线程 = 工单的生命周期容器)。
- 触发前:补仓库元数据到 thread metadata、Slack permalink 注入、docs-only 通道门等。

---

## 11. 关键不变式与复刻验收清单

**不变式(违反即出生产事故):**
1. 单进程单事件循环;任何同步重活不得上主循环(重活要么有界,要么 `to_thread`)。
2. 消息只增不减(`add_messages`),思考块在**写 checkpoint 前**剥离(保 `signature`),LangSmith 追踪不受影响。
3. 所有 run 走 `create_durable_run` 契约(sync 持久化 + v2 流 + resumable + 完成 webhook)。
4. 面向用户的 checkpoint 读必须有界(state 单快照 或 history ≤5);全量遍历只允许后台显式任务。
5. 失败必须幂等、fail-closed(webhook token、loopback 降级、404 归一化)。
6. checkpointer TTL 12h 删除;业务状态放 thread metadata / store,不依赖 checkpoint 长存。

**最小复刻验收:**
- [ ] `langgraph.json` 五图 + http.app + Postgres checkpointer/store + TTL 可启动
- [ ] `pin_single_event_loop` 在 app import 期生效且可拒绝启动
- [ ] `dispatch_agent_run` 六参数契约 + 完成 webhook 幂等
- [ ] XML 信封输入 + 身份注入去重(hash)
- [ ] `/state`(单快照,~秒级)与 `/history`(≤5 + 90s 超时 + `/state` 降级 + 合并)代理
- [ ] 超大线程(≥30MB checkpoint)下:打开线程页 P95 < 10s,期间其他线程请求不被冻结 > 1s(用 queue-probe 的 `lag_ms` 验证)

---

## 12. 主要文件索引

| 职责 | 文件 |
|---|---|
| 服务器配置 | `langgraph.json` |
| FastAPI 组装 | `agent/api/app.py`、`agent/webapp.py` |
| 单循环铁律 | `agent/utils/event_loop.py` |
| 持久化 | `agent/postgres_persistence.py` |
| 输入信封 | `agent/input_messages.py` |
| 触发契约 | `agent/dispatch.py` |
| 完成信号 | `agent/completion.py` |
| 主 agent / 审查 / 分析 / 聊天 / 调度 | `agent/server.py` / `reviewer.py` / `analyzer.py` / `chat.py` / `scheduler.py` |
| 系统提示 | `agent/prompt.py` |
| dashboard 业务 | `agent/dashboard/routes.py`、`thread_api.py`、`review_chat_api.py`、`plan_api.py`、`workflow_approval_api.py` |
| webhook | `agent/webhooks/{slack,linear,github}_routes.py`、`common.py` |
| 中间件 | `agent/middleware/*`(`strip_thoughts.py` 等) |
| 沙箱 | `agent/utils/sandbox*.py`、`agent/runtime/sandbox.py` |
| 运维/调试 | `agent/utils/queue_debug.py`、`agent/api/health.py` |
| 部署 | `docker-compose.infra.yml`(Postgres 16 + Redis 7)、`Makefile`(`make dev` = `uv run langgraph dev`) |
