# 共享诊断 SQL 服务（FastAPI + PostgreSQL）

值班数据库工程师共用的诊断执行服务：提交至多 10 条可信 SQL，在**固定复用的两条
PostgreSQL 连接**之一上按序自动提交执行；支持逐句结果查询、取消未结束的单；
每单结束后清理会话状态（SET、临时表、会话级咨询锁等），清理失败则淘汰连接并补建。

## 架构

```
                ┌────────────── FastAPI (uvicorn, 单进程) ──────────────┐
  POST /jobs ──▶│  校验(sqlguard) → JobStore(内存注册表) → 共享队列       │
  GET  /jobs/*  │                                                        │
  POST ../cancel│  cancel: 置标志 + 202 即时受理 ──▶ CancelSender 线程    │
  GET  /pool    │       (可配置发送停顿) ──▶ conn.cancel()               │
                │                                                        │
                │  Worker 线程 #1 ── 连接 #1 (autocommit, 长连接)         │
                │  Worker 线程 #2 ── 连接 #2 (autocommit, 长连接)         │
                └────────────────────────┬───────────────────────────────┘
                                         ▼
                                   PostgreSQL
```

- **固定两条连接**：两个 worker 线程各自持有一条长连接（`autocommit=True`），
  从同一个队列取单。连接在启动时建立并反复复用，**绝不每单重连**；唯一的重建
  路径是"清理失败/连接损坏 → 淘汰旧连接 → 原地补建"（`generation` 递增，
  池子仍是两条）。
- **排队**：两条连接都忙时，新单在内存队列等待（默认上限 1000，超出返回 429）。
- **互不阻塞**：每条连接一个独立 worker；慢 SQL、慢清理只占用本连接的 worker，
  另一连接的执行、提交以及所有状态查询（读内存注册表）都不受影响。

## 快速开始

```bash
docker compose up --build
# API: http://localhost:8000  文档: http://localhost:8000/docs
```

本地开发（需自有 PostgreSQL）：

```bash
pip install -r requirements.txt
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/diag \
  uvicorn app.main:app --port 8000
```

演示脚本（覆盖：顺序执行、会话清理验证、取消、慢取消发送/慢清理钩子、迟到取消）：

```bash
./scripts/demo.sh            # BASE=http://localhost:8000 可覆盖
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/jobs` | 提交 1–10 条 SQL（202，返回 `job_id`） |
| GET | `/jobs` | 最近的单（摘要列表） |
| GET | `/jobs/{id}` | 单详情 + 逐句结果（状态/列/行/rowcount/耗时/错误） |
| POST | `/jobs/{id}/cancel` | 取消未结束的单（202 受理；已结束返回 409） |
| GET | `/pool` | 两条连接的状态（idle/busy/cleaning/reconnecting、代数、清理失败数） |
| GET | `/logical-replication/check/{schema}/{table}` | 核实该普通表的 UPDATE/DELETE 能否从发布端发出（逐发布评估） |
| GET | `/health` | 存活检查 |
| GET | `/docs` | Swagger UI |

### 提交

```bash
curl -XPOST localhost:8000/jobs -H 'content-type: application/json' -d '{
  "statements": ["SET work_mem = '"'"'64MB'"'"'", "SELECT count(*) FROM pg_stat_activity"],
  "options": {"cleanup_delay_ms": 0, "break_connection_after": false}
}'
```

- 每条元素必须是**一条**语句（`;` 分隔的多句会被 422 拒绝；字符串/美元引号/
  注释里的 `;` 不算）。
- **拒绝显式事务控制**：`BEGIN/START/COMMIT/ROLLBACK/ABORT/END/SAVEPOINT/
  RELEASE`、`SET TRANSACTION`、`SET SESSION CHARACTERISTICS`、
  `PREPARE TRANSACTION`（含注释伪装的写法），返回 422。每条语句各自自动提交。
- 单语句错误 → 该句记 `error`，后续句记 `skipped`，单状态 `failed`。

### 取消语义

- **及时受理**：`POST /jobs/{id}/cancel` 立即置标志并返回 202；真正的取消发送
  由独立线程完成（可用 `{"delay_ms": N}` 模拟慢取消管道），不阻塞受理。
- **受理后**：正在执行的语句被 `conn.cancel()` 中断（PostgreSQL 标准取消），
  后续语句全部 `skipped`；**此前已提交的语句效果保留**（自动提交，无回滚）。
  极端时序下已发往服务器的语句可能自然完成，属正常现象。
- **排队中的单**：直接标记 `cancelled`，不会执行。
- **迟到取消**：单已结束时取消返回 409（无操作）；发送线程在发送前按连接校验
  "该单仍是此连接当前单且未结束"，因此**迟到的取消绝不会误停同一连接上的下一单**。
- **连接归还条件**：SQL 停止 + 取消发送完成 + 会话清理完成，三者齐备后连接才
  回到空闲（可用延迟钩子直接观测）。

### 会话清理（每单结束后，成功/失败/取消都执行）

`RESET ALL`、`RESET ROLE`、`RESET SESSION AUTHORIZATION`、`DEALLOCATE ALL`、
`DISCARD SEQUENCES`、删除本会话全部临时表/视图/序列、
`pg_advisory_unlock_all()`。清理失败 → 关闭并淘汰该连接，原地补建新连接
（`/pool` 可见 `generation` 与 `cleanups_failed` 递增）。

## 逻辑复制发布端核查（UPDATE/DELETE 能否从发布端发出）

`GET /logical-replication/check/{schema}/{table}` 面向已配置逻辑复制的
PostgreSQL 16 **普通表**：找出所有覆盖该表的发布——单表发布（`FOR TABLE`）、
整个 schema 发布（`FOR TABLES IN SCHEMA`）、全库发布（`FOR ALL TABLES`）——
并对**每个发布分别**核对 UPDATE 与 DELETE 两种操作：

- **操作开关**：`pubupdate` / `pubdelete` 未开启 → 该发布不会发出此操作
  （`disabled`，DML 本身不受影响）；
- **表的 replica identity**：`FULL`、或 `USING INDEX`（索引存在且有效）、
  或 `DEFAULT` 且有主键才可用；否则发布端会拒绝 UPDATE/DELETE
  （`cannot update/delete ... does not have a replica identity`）；
- **列清单**（单表发布可带）：必须覆盖全部 replica identity 列，且与
  `REPLICA IDENTITY FULL` 不能共存，否则发布端拒绝
  （`Column list used by the publication does not cover the replica identity`）；
- **行过滤**（单表发布可带）：过滤表达式只能引用 replica identity 列
  （`FULL` 时不受限），否则发布端拒绝
  （`Column used in the publication WHERE expression is not part of the
  replica identity`）；行过滤有效时在 `notes` 中说明**只覆盖匹配行**。

判定规则与 PostgreSQL 16 发布端自身检查（`CheckCmdReplicaIdentity`）一致，
并已对照真实 PostgreSQL 16 验证。

### 结论词汇（每个发布 × 每种操作）

| verdict | 含义 |
|---|---|
| `ok` | 该发布可以从发布端发出此操作 |
| `blocked` | 发布已开启此操作，但发布端会拒绝（`reasons` 给出具体阻碍：replica identity 不可用 / 列清单未覆盖身份列 / 行过滤引用非身份列） |
| `disabled` | 发布未开启此操作（`pubupdate`/`pubdelete` 为 off），不会经此发布发出 |
| `unknown` | 无法判定（`reasons` 给出原因，如 replica identity 索引无效） |

### 语义与保证

- **逐发布独立评估**：一份发布满足条件，不会掩盖另一份发布的缺项
  （与 PostgreSQL 实际行为一致：任一覆盖该表的发布有问题，对应操作就会失败）。
- **同一快照**：所有目录信息在**一条** `REPEATABLE READ READ ONLY` 事务内
  读取，保证相互一致。
- **查询失败不返回局部结论**：任何目录查询失败 → 502，不返回任何评估结果。
- **不占用两条诊断长连接**：本端点每次请求使用独立的短连接
  （带 `statement_timeout`，默认 15s，`LOGREPL_STATEMENT_TIMEOUT_MS` 可调），
  与 `/jobs` 的两条长连接池完全无关。
- **只代表发布端判断**：`assessment_scope` 字段明确说明——结果不代表任何
  订阅端已收到或应用了变更。
- 表不存在 → 404；不是普通表（视图、分区表、分区等）→ 422 并说明原因
  （分区表的祖先发布与 `publish_via_partition_root` 不在本核查范围内）。

```bash
curl -s localhost:8000/logical-replication/check/public/orders | python3 -m json.tool
```


## 测试钩子（可控停顿/故障注入）

| 钩子 | 位置 | 作用 |
|---|---|---|
| `options.cleanup_delay_ms` | 提交单时 | 清理阶段停顿 N 毫秒（模拟慢清理） |
| `{"delay_ms": N}` | 取消请求体 | 受理后延迟 N 毫秒再向 PG 发送取消（模拟慢取消发送） |
| `options.break_connection_after` | 提交单时 | 单结束后断开本连接（模拟连接故障），触发淘汰+补建 |
| `CANCEL_SEND_DELAY_MS` / `CLEANUP_DELAY_MS` | 环境变量 | 上述两个延迟的全局默认值 |

## 自测

```bash
pip install -r requirements-dev.txt
pytest tests/test_sqlguard.py        # 纯单元测试（SQL 守卫），无需数据库
pytest tests/test_logrepl.py         # 纯单元测试（发布端核查判定逻辑），无需数据库
# 集成测试需要运行中的服务与数据库：
API_BASE=http://localhost:8000 TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/diag \
  pytest tests/test_integration.py tests/test_logical_replication.py
```

集成测试覆盖：按序自动提交与逐句结果、行数截断、失败即停、参数校验、
会话状态不泄漏（SET/临时表/咨询锁/预备语句，含失败单）、取消运行中/排队中的单、
迟到取消（409 与延迟发送不误伤下一单）、慢 SQL/慢清理不阻塞另一连接、
清理失败淘汰并补建连接（钩子注入 + `pg_terminate_backend` 真实杀连接两种）。

逻辑复制核查的集成测试（`tests/test_logical_replication.py`）覆盖：主键表/
无主键表/`REPLICA IDENTITY FULL`/`NOTHING`/`USING INDEX`、列清单覆盖与缺失
身份列、行过滤引用身份列与非身份列、单表/schema/全库三种覆盖方式、
多发布独立评估（好发布不掩盖坏发布）、表不存在（404）与非普通表（422）、
核查不占用连接池；并将每个 `blocked` 结论与真实 UPDATE/DELETE 在发布端的
报错逐一对照（ground truth）。

> 注：PostgreSQL 的语句取消依赖向后端进程投递 SIGINT。在禁止进程间信号的
> 沙箱里，"中断正在执行的语句"无法生效（`pg_terminate_backend` 同样无效），
> 相关测试用例设计为在两种环境下都通过：沙箱内验证协作式停止（语句间停止），
> Docker/正常 Linux 下同一代码路径会真正中断执行中的语句。

## 设计说明与限制

- 面向**可信 SQL**（值班 DBA）：守卫只强制执行契约（单句、禁显式事务），
  不是安全边界；`CALL`/`DO` 体内的事务控制无法静态识别，属约定范围。
- 任务注册表在内存中（重启即失），保留最近 1000 单。
- 结果行数上限 `ROW_LIMIT`（默认 200，超出截断并标记 `truncated`）。
- 单进程单实例：连接池在进程内，勿以多 worker 方式启动 uvicorn。
- 连接使用 `prepare_threshold=None`，避免 psycopg 的客户端预备语句缓存与
  清理中的 `DEALLOCATE ALL` 冲突。
