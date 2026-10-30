# chem-mcp 技术方案与设计背景

> 版本：v1.7 ｜ 更新：2026-10-30（五轮评审修复：首建档并发原子化/章节级扫描豁免/动态对账/manifest 固定清单与 --verify blob 对账） ｜ 状态：生产运行中（单实例试点规模）
> 本文档面向审查者：完整交代项目背景、要解决的问题、每个关键设计决策的动机、已知取舍与未解决问题。配套代码全部在本仓库内。

---

## 1. 项目背景：这个网关为什么存在

### 1.1 上游产品

本项目是 [chemask](https://fangjunling.top/chem/)（AI问化学，CIE 9701 A-Level 化学学习系统，FastAPI + MySQL + Qdrant RAG，私有仓）的**平台版数据层**。

背景：chemask 是全栈自研版；2026-10 我们在做它的"平台版"——把教学能力搬到 WorkBuddy 智能体平台上，用「专家 + 资料库 RAG + MCP 工具」替代部分自研代码。双轨对照本身是产品实验：**同一产品，租流水线 vs 自建流水线，边界在哪**。

### 1.2 要解决的核心问题

WorkBuddy 平台有三个硬限制（实测确认）：

| # | 平台限制 | 后果 |
|---|---|---|
| 1 | 不向专家暴露召唤者身份（无"我是谁"API） | 专家不知道当前学生是谁，无法按人服务 |
| 2 | 会话隔离，无跨会话记忆 | 学生每次对话都要重新自我介绍 |
| 3 | 无作答数据持久层 | 错题本/掌握度/学情报告没有数据源 |

**chem-mcp 就是这三个限制的统一解法**：一个 MCP Server，把 chemask 的学生数据层安全地暴露给平台上的 AI 专家。本质是：

> 让一个无状态的 AI Agent 具备跨会话的、按学生隔离的长期记忆和学习数据能力。

### 1.3 一次真实运行

```
学生召唤「CIE化学考情教练」专家
  → 专家提示词第一条指令：调 get_profile
  → WorkBuddy 经 MCP 连接器 POST https://…/chem-mcp/mcp（Bearer Token 在请求头）
  → 网关：SHA-256(Token) → api_keys 表 → 反查 student_id
  → 返回画像：卷型 9701/23、目标 a、卡点"卤代烃消去机理:open"、最近对话摘要
  → 专家带着记忆继续教学；作答经 record_attempt 实时落库
```

## 2. 总体架构

```
学生（WorkBuddy 账号）
   ↓ 召唤专家
WorkBuddy Expert（提示词 v3：身份识别/首访五问/答疑规则/报告模板）
   ↓ MCP（HTTP POST JSON-RPC + GET SSE 长流心跳，Bearer Token）
chem-mcp Gateway（本仓库 gateway/）
   ├─ Token 鉴权：SHA-256 哈希比对 api_keys，明文永不落库
   ├─ 身份注入：student_id 由 Token 反查，不信任 LLM 传参
   ├─ RBAC：api_keys.role（student/teacher），class_summary 仅 teacher
   ├─ 业务限流：仅 tools/call 计费（保活流量免计费）
   ├─ 数据约束：幂等唯一键 / 乐观锁 / 字段白名单 / 输出体积上限
   ↓ 函数调用（同进程，非 HTTP）
数据层 gateway/db.py（连接池）→ MySQL
```

**关键取舍：网关直连 MySQL，而不是经 chemask HTTP API。** 三点理由：

1. **部署拓扑**：网关与 chemask 同机（同一台服务器、同一个 MySQL 实例），HTTP 中间层只增加一跳延迟与故障点，无隔离收益；
2. **错误语义**：9 个工具需要细粒度的数据库级错误映射（唯一键冲突=幂等重复、行版本不符=乐观锁冲突），跨 HTTP 层传递这些语义要设计一套错误协议；
3. **事务边界**：乐观锁的"确保行存在 + 条件 UPDATE"必须在同一连接事务内完成，跨服务拆开会引入分布式一致性问题，收益为零。

代价是网关与 chemask 共享 db.py 口径（我们用同一份代码文件保证）。**评审曾指出 README"经 chemask API"的表述与代码不符——实际是文档错误，架构意图就是本节的直连方案，现已更正。**

## 3. 核心设计决策（按审查者关注点组织）

### 3.1 身份与业务参数分离（本项目最重要的安全设计）

**LLM 永远不传 student_id。** 身份链路：

```
Authorization: Bearer ck_xxx
  → SHA-256 → api_keys.token_hash 查找（revoked_at IS NULL）
  → student_id 注入工具函数签名 impl(key_row["student_id"], args, key_row)
```

工具入参 schema 中没有任何身份字段（`additionalProperties: False` 白名单校验）。这杜绝了"prompt injection 让模型读别的学生的数据"这类攻击面——模型根本拿不到传身份参数的入口。

配套：Token 只存 SHA-256（签发时明文展示一次）；Token 格式预校验（长度上限、拒绝控制字符）；吊销即失效（`revoked_at`）；`last_used_at` 审计。

### 3.2 记忆分层（对"把聊天记录塞向量库"方案的否定）

| 层 | 存储 | 内容 | 生命周期 |
|---|---|---|---|
| 会话内 | 平台上下文 | 本次对话 | 会话关闭即失 |
| **摘要记忆** | `student_memory` | 一人一行"当前画像"：卷型/目标/卡点(含状态)/已讲知识点/计划阶段/偏好/自由摘要 | 跨会话，增量合并 |
| 明细记忆 | `dialogue_log` | 每轮对话摘要句（≤500字，**非原文**） | 只写不覆盖，可重建摘要 |
| 行为数据 | `attempts` | 每次作答：题目/得分/错因/耗时 | 只写 |
| 总结数据 | `reports` | 六段式学情报告全文 | 只写 |

设计动机：

- **一人一行摘要**：记忆读取成本恒定（`get_profile` 一次查询），不随历史增长变慢；明细表是摘要的来源与回溯依据；
- **对话原文永不入库**：只存摘要句。隐私与体积双赢，也符合"看成果不看过程"的产品立场；
- **防膨胀**：摘要限 2000 字符（超限保留尾部，近因优先）；`taught_topics` 记录讲过的知识点防止重复讲解。

### 3.3 幂等：唯一约束 + 原子写入（v1.3 重写）

Agent 工具调用天然会重试（超时→retry→同一操作执行两次），`record_attempt` 必须幂等。

**v1.2 的错误实现**（评审正确指出）：`find → 不存在 → insert` 两步走，并发下有竞态窗口；且 `idem_key` 编码进 `weak_points` JSON 列——业务数据与系统控制字段混杂。

**v1.3 修复**：

```sql
ALTER TABLE attempts ADD COLUMN idem_key VARCHAR(128) NULL;
ALTER TABLE attempts ADD UNIQUE KEY uq_attempts_idem (student_id, qid, idem_key);
```

```python
def add_attempt_idempotent(student_id, idem_key, qid, **fields):
    try:
        cur.execute("INSERT INTO attempts(... idem_key ...) VALUES (...)")
        return cur.lastrowid, False
    except IntegrityError as e:
        if e.args[0] != 1062: raise      # 非唯一键冲突照常抛
    # 冲突 → 回读已存在行
    return existing_id, True
```

并发验证：6 线程 barrier 同步插入同一 idem_key → 恰好 1 行、1 新 + 5 duplicate（`tests/test_mcp_gateway.py::test_atomic_idempotency_concurrent`）。存量数据迁移：weak_points 里的 `idem:xxx` 自动搬到新列。

### 3.4 记忆并发安全：乐观锁（v1.3 新增）

`save_memory` 是 read-merge-write 模式（读 weak_topics → 合并 → 写回），并发下有经典 Lost Update：

```
A 读 weak=[A]     B 读 weak=[A]
A 写 [A,B]        B 写 [A,C]   ← B 覆盖掉 A 的更新
```

**v1.3 修复**：`student_memory.version` 列，条件更新：

```sql
UPDATE student_memory SET …, version=version+1
WHERE student_id=%s AND version=%s    -- rowcount==0 → 冲突
```

网关侧 `_retry_on_conflict` 装饰器：冲突自动重读重试（≤3 次，50ms 退避），仍失败返回 409。测试通过 monkeypatch 在工具执行中途插队推进版本，验证重试收敛且两个写者的 weak_topics 都保留（无丢失）。

单写者场景（当前试点：一个学生一个连接器）的边界问题已随 v1.7 首建档原子化关闭（§5t.3）：现在无论单写还是并发，所有 Memory 写入都经版本条件更新。

### 3.5 get_profile 输出治理（v1.3 重写）

`get_profile` 返回体有硬上限（3000 字符），防 MEDIUMTEXT 报告全文等随历史膨胀注入上下文。

**v1.2 的 bug**（评审正确指出）：超限时对 `json.dumps` 结果做字符串切片——会产生非法 JSON（截断引号/括号），客户端 `json.loads` 直接失败。

**v1.3 修复**：`_trim_to_limit`——按字段优先级逐轮收缩，**裁剪发生在构建 payload 阶段**，序列化永远最后一步：

```
优先保留：student_id > stale > weak_topics > plan_stage
逐级牺牲：taught_topics 收缩 → summary 收缩(800→400→150) → 报告块整体丢弃 → 对话块整体丢弃
```

任何输入都返回可解析 JSON（测试 `test_trim_to_limit_never_breaks_json`）。报告全文走独立的 `get_report` 工具按需拉取。

### 3.6 RBAC：正式 role 列（v1.3 重写）

**v1.2 的错误**（评审正确指出）：教师权限判断是 `if "teacher" in label.lower()`——字符串约定不是权限模型，label 是给人看的备注。

**v1.3 修复**：`api_keys.role` 列（`student`/`teacher`，CHECK 级 assert 在应用层），`class_summary` 按 `role` 判 403。存量数据迁移：`teacher-*` label 自动升级为 `role='teacher'`。签发 CLI：`ops/mcp_token.py issue --role teacher`。

测试覆盖三个方向：student role 403 / teacher role 200（label 不含 teacher 字样）/ label 含 teacher 但 role=student 仍 403（证明 label 不再生效）。

### 3.7 限流：只对业务调用计费（真实故障驱动）

初版限流挂在鉴权层（每个请求都计数）。**真实故障**：WorkBuddy 连接器以 ~3 秒/次的频率做保活轮询（SSE 重连 + 探活 POST），10 分钟打掉 100+ 次配额，**真正的业务调用 get_profile 反而 429**——系统自我拒绝服务。

修复后的语义：

- **免计费**：`initialize` / `tools/list` / notifications / GET SSE 保活
- **计费**：仅 `tools/call`（业务动作），默认 120 次/小时

教训（已写进 README）：**限流的计数对象应该是"业务动作"而不是"网络请求"——把保活流量计入配额，等于自我 DoS。**

已知局限（诚实声明）：计数用 MySQL `api_key_calls` 表（key_id + 时间戳索引），单实例正确；多实例部署需换 Redis 滑动窗口——当前单机试点不做此复杂度，已列入路线图。

### 3.8 MCP 协议实现：手写适配层（有意识的取舍，不是无知）

**评审意见**：应基于官方 MCP SDK 而非手写 JSON-RPC 解析；`2024-11-05` 版本与自定义 SSE 偏旧。

**我们的回应——这是一个有意识的工程决策，理由如下**：

1. **兼容性优先于标准性**。本网关的第一个兼容目标是一个具体的生产客户端（WorkBuddy 连接器），而它在新协议细节上的行为无法从公开文档完全推断。我们是**通过三次实测故障逆向出它的真实期望的**：
   - `GET /mcp` 必须返回 200 + SSE 流（返回 405 会被判传输失败——我们最初只有 POST 路由）；
   - `notifications/initialized`（无 id 通知）必须返回 202 空体（返回 400 会让客户端中断握手陷入重连循环）；
   - 短流 SSE 会触发 3 秒重连风暴，需长流 + 心跳。
   官方 SDK 对这些边缘行为（尤其非标准客户端的容错）是黑盒；手写层让我们在 10 分钟内定位并修复每一次故障，修复版本当天上线。
2. **协议面极小**。本网关只需 `initialize` / `tools/list` / `tools/call` / notifications 四个方法，无 resources/prompts/sampling 等能力。手写面 ≈ 60 行路由代码，且有 37 个测试用例锁定行为。
3. **迁移路径已预留**。`_TOOL_IMPL` 字典是唯一的工具注册点，协议层与工具层完全解耦——若后续需要官方 SDK（例如要兼容更多 MCP 宿主），只需替换路由层，9 个工具实现零改动。

**诚实的定位声明**：本项目是 *MCP-compatible data gateway for a specific production client*，不是 *reference MCP server implementation*。若你的目标是后者，请以官方 SDK 为准——这也是我们 README 不宣称"标准 MCP 实现"的原因。

### 3.9 分层原则：MCP 负责能力，不负责智能

```
LLM（专家提示词）  = 决策层：何时查记忆/何时存记忆/怎么辅导/怎么出报告
MCP 网关           = 能力层：身份/数据访问/权限/约束/幂等/限流
MySQL              = 状态层：唯一真相
```

安全规则全部在网关强制执行，不依赖提示词遵守（提示词会被注入，数据库约束不会）。例如：即使 prompt injection 成功诱导模型调用 `class_summary`，非 teacher role 的 Token 依然被网关 403 拒绝。

## 4. 已知局限与路线图（诚实清单）

| # | 局限 | 现状 | 计划 |
|---|---|---|---|
| 1 | 限流为单实例内存语义（MySQL 计数表） | 单机试点够用 | 多实例时换 Redis 滑动窗口 |
| 2 | 手写 MCP 协议层，版本 2024-11-05 | 兼容当前生产客户端（WorkBuddy） | 需兼容更多宿主时迁官方 SDK（工具层零改动） |
| 3 | `weak_topics` 状态机只有 open/solved 两态 | 满足当前教学流 | 扩展 discovered→confirmed→learning→mastered→regressed |
| 4 | 记忆合并粒度：整行版本锁，非字段级 | 冲突概率极低（单学生单连接器） | 字段级 JSON_ARRAY_APPEND 原子更新（按需） |
| 5 | class_summary 全量加载学生列表计算 | 试点 <50 人 | 分页/物化视图（按需） |
| 6 | 教师Token与学生Token同表，scope 粒度=role 二值 | 满足当前 | 引入 scopes 列（student:read / class:read） |
| 7 | 无请求链路 tracing id | 日志有 key_id+工具名 | 加 X-Request-Id 透传 |
| 8 | 重名学生经 create_student 可能共享 student_id（INSERT IGNORE 按名幂等） | 试点期人工对名单兜底 | **P1**：签发流程改 user_id 映射，姓名仅展示 |
| 9 | 测试依赖真实 MySQL，无 CI | 37 用例本地跑 | **P1**：单测/集成分离 + GitHub Actions |
| 10 | dialogue_log 写入无独立幂等键 | 低危（摘要重复无资损） | **P1**：统一 operation_id 幂等框架 |
| 11 | class_summary 无班级边界（teacher 见全表） | 试点期教师=管理员 | **P1**：teacher_classes 关联 + 查询强制带边界 |
| 12 | ~~Memory 首建档（ver=None）并发首写无版本保护~~ | **v1.7 已修复**：无行按 version=1 统一走原子建行+条件更新，见 §5t.3 | 关闭（并发测试锁定） |

## 5. 二轮评审修复记录（v1.4，2026-10-30）

第二轮外部审查（ChatGPT 对 GitHub main 分支静态审查）发现了一个**比代码 bug 更严重的问题**：

> **v1.3 的文档声称修复的四项内容，GitHub 仓库里的 `gateway/mcp_gateway.py` 和 `tests/` 仍是旧版**——db.py 是新的，网关没接入。

**根因（我们的流程失误）**：仓库创建于 v1.3 编码之前（cp 快照），修复在 chemask 工作区完成并部署生产，但推送仓库时只重新 cp 了部分文件。教训：**发布仓库必须以"内容 diff 为零"验证，不能以"曾经复制过"记忆为准**。已修复并在本仓库根加入同步脚本前的对账检查。

### v1.4 修复清单

| # | 指控 | 核实 | 处置 |
|---|---|---|---|
| 1 | v1.3 四项修复未接入仓库网关/测试 | ✅ **属实（同步事故）** | 仓库全量同步至生产版本（`gateway/mcp_gateway.py`、`tests/` 逐字节一致），v1.4 当时的 33/33 测试在仓库 import 路径下通过（历史记录：v1.5 起 37 个，v1.7 起 38 个） |
| 2 | 缺 `save_report`：报告生成后无处持久化，数据闭环断裂 | ✅ 属实 | **新增第 9 个工具**：`save_report(report_type, content_md, report_key)`，`UNIQUE(student_id, report_key)` 原子幂等（Agent 重试只落一份）；专家提示词同步规则"**必须等 save_report 返回成功才能宣称已存档**" |
| 3 | 限流计数 SELECT→INSERT 非原子，并发可穿越配额 | ✅ 属实 | **部分修**：已改 insert-before-count 并修正边界为"计数 > 限额"；但三轮评审指出 InnoDB REPEATABLE READ 下未提交 INSERT 对并发事务不可见，仍可穿透配额 → **v1.5 已改行锁串行化**（见 §5bis），本项至此真正修复 |
| 4 | Expert Prompt 与工具 schema 不一致（save_memory 无 target_grade 参数；报告存档话术提前） | ✅ 属实 | **已修**：提示词明确"目标等级/分数段/卷型变化走 update_profile"；报告存档流程改为"save_report 成功后才宣称" |
| 5 | 重名学生 create_student INSERT IGNORE 返回同一 student_id → 数据串号 | ✅ 属实（schema 已有 user_id 列但签发流程未用） | **列入 P1**（需要业务层注册流程配合，见路线图 #8）；当前试点用"报名字+教师对名单"人工兜底 |
| 6 | class_summary 无班级权限边界，teacher 可看全表 | ✅ 属实（设计如此） | **列入 P1**（路线图 #6：teacher_classes 关联表）；试点期教师=管理员，文档已声明 |
| 7 | 测试依赖真实 MySQL / 缺 CI | ✅ 属实 | **列入 P1**（路线图 #9）；当前 37 用例跑真实库（本地隧道），单测/集成分离与 GitHub Actions 待加 |
| 8 | 对话摘要无独立幂等键 | ✅ 属实（低危） | **列入 P1**（统一 operation_id 方案，见路线图 #10） |

### v1.4 后的工具全景（9 个）

```
读：get_profile / list_mistakes / get_mastery / get_report / class_summary(teacher)
写：update_profile / save_memory / record_attempt(幂等) / save_report(幂等·新)
```

学情报告闭环就此完整：**生成（LLM）→ save_report 落库（幂等）→ get_profile 带出摘要 → get_report 按需全文**。

---

## 5bis. 三轮评审修复记录（v1.5，2026-10-30）

第三轮审查判定：同步完整性/save_report/Prompt 三组 PASS；**限流原子性 FAIL**（find 到了真正的并发漏洞）、**文档一致性 FAIL**（旧数字残留）。全部处置如下：

### 5b.1 限流并发原子化（本轮最重要修复）

**评审指出的漏洞（完全属实）**：v1.4 的 insert-before-count 在 InnoDB `REPEATABLE READ` 隔离级别下，并发事务**互相看不到对方未提交的 INSERT**——两个请求可同时读到相同计数、同时通过配额判定。单纯调整语句顺序不构成原子性，v1.4 的"原子占位"表述是错的（已在 §5 修正）。

**v1.5 修复：行锁串行化**

```sql
START TRANSACTION;                          -- get_conn 上下文
SELECT id FROM api_keys WHERE id=%s FOR UPDATE;   -- 行锁：同一 Token 的并发请求在此排队
INSERT INTO api_key_calls(key_id) VALUES (%s);
SELECT COUNT(*) ... WHERE called_at > NOW() - INTERVAL 1 HOUR;
COMMIT;                                     -- 释放锁，后续请求可见已提交计数
```

同一 Token 的并发请求被 `FOR UPDATE` 强制串行：后到者必须等前者提交后才能统计（此时能看到前者的计数）。不同 Token 互不阻塞（锁粒度=单行）。

**并发测试**（评审明确要求的形态）：`test_rate_limit_concurrent_serialized` —— 6 线程 Barrier 同步、限额 1，断言恰好 1 个 200 + 5 个 429。真实 MySQL 上通过。

### 5b.2 Memory 乐观锁读取时序（评审新发现，属实）

**漏洞**：`_save_memory_locked` 先 `get_student_memory()` 再 `get_memory_version()`——两次独立事务。竞态：A 读到旧内容(v1) → B 写入并推版本 → A 再读到新版本号(v2) → A 用**旧内容+新版本号**做条件更新 → 成功，B 的更新被覆盖。

**修复**：内容与版本改为**同一次 SELECT**（`get_student_memory` 返回的行自带 `version` 列，`mem.get("version")` 即条件更新依据）。内容旧则版本必旧，条件更新必失败，触发重试重读。`_save_memory_locked`/`_update_profile_locked`/`_auto_solve_on_repeat_success` 三处全部改为同源读取。

### 5b.3 新学生首次建档丢 weak_topics（评审新发现，属实）

**漏洞**：`_update_profile_locked` 中 `ver is None` 分支直接 upsert fields，不含 weak_topics 合并——新学生首次 `update_profile` 传入的卡点列表被静默丢弃。

**修复**：weak_topics 归一化（字符串→`{"topic","status":"open"}`）移到版本判断之外，首建档同样写入。测试 `test_first_profile_with_weak_topics_not_lost` 锁定。

### 5b.4 get_profile 长度硬上限（评审：合法 ≠ 受控）

裁剪计划扩展至全部可膨胀字段（preferences 整块丢弃、weak_topics/taught_topics 条数与单项长度、exam_board/plan_stage/current_score/target_grade/summary 逐级收缩）；新增**终极兜底**：计划走完仍超限时输出仅含 `student_id + stale + truncated:true` 的保护性 JSON——**任何输入下输出必 ≤3000 字符且为合法 JSON**。测试 `test_trim_never_exceeds_hard_limit` 用 9000 字符巨型字段验证。

### 5b.5 文档对账（三轮时修正了 7 处指定位置，四轮复查仍有残留）

三轮时修正了评审指定的 7 处位置（README"8 个白名单工具"→9、"30 个测试"→37、DESIGN §2/§3.8/§8 三处 30→37、网关模块 docstring"7 个工具/app.db"→"9 个工具/db.py"），§5 限流行表述已更正为诚实版本（不再声称 v1.4 已完成原子限流）。**但四轮复查证明"指定位置修正"不等于"全仓库一致"**——README 徽章、README 目录注释、DESIGN 路线图等未列入清单的位置仍残留 33/v1.4/旧启动命令。教训已固化为机制：文档对账不靠人工清单，靠 `sync_from_chemask.py` 的全量模式扫描（见 §5ter），扫描规则与版本号同步演进。

**关于"生产-仓库逐字节一致"的验证方式**：评审正确指出其无法独立验证。我们的对账方式是 `diff` 本地两份文件 + 在仓库布局下跑完整测试套件（本版 37/37 于 chem-mcp/ 目录通过）。CI 落地前，这是人工流程；路线图 #9 的 GitHub Actions 即为此而设。

---

## 5ter. 四轮评审修复记录（v1.6，2026-10-30）

第四轮审查判定：限流并发原子化、Memory 同源读取、首建档 weak_topics、长度硬上限四组全部 PASS（限流修复正式关闭）；**文档与发布对账 FAIL**。全部处置如下：

### 5t.1 文档全量对账（四轮列出的 A–E 问题）

| # | 指控 | 位置 | 处置 |
|---|---|---|---|
| A | README 徽章仍 tests-33/33、version-1.4 | README L6 | ✅ 已改 `tests-37/37`、`version-1.6` |
| B | README 目录注释仍"33 用例" | README 目录结构段 | ✅ 已改 37 用例 |
| C | DESIGN 路线图 #9 仍"33 用例本地跑" | DESIGN §4 | ✅ 已改 37（v1.4 表格内的历史现状描述同步更新） |
| D | README 工具清单 save_report 重复两次 | README L14 | ✅ 已去重，9 个工具各出现一次（record_attempt 补标幂等） |
| E | Gateway docstring 旧启动命令 `-m app.mcp_gateway` | gateway/mcp_gateway.py | ✅ 已改为 `uvicorn gateway.mcp_gateway:app --port 8721`；chemask 源文件同步修正（保留两种布局各自的启动方式说明，防止下次同步回退） |

### 5t.2 sync_from_chemask.py 加固为发布门禁（fail-closed）

四轮指出脚本三缺陷，全部修复：

| 缺陷 | 修复 |
|---|---|
| 源文件缺失静默跳过，可能假报"同步成功" | **fail-closed**：同步前逐一校验源文件存在，缺失即 `RuntimeError` 非零退出，一个文件都不复制 |
| 旧内容扫描规则不完整（33/33、33 用例、version-1.4、`-m app.mcp_gateway`、工具重复均不拦截） | **全量模式扫描**：新增 `STALE_PATTERNS` 集中清单（含正则），覆盖徽章、用例数、版本号、旧启动命令、旧 import；**清单与版本号同文件维护**，版本升级时强制同步演进；工具清单去重校验（README 中 9 个工具名各恰好出现一次） |
| 重复的 ops import 替换代码块 | 已删除冗余段 |
| （无哈希/无测试门禁） | 同步后自动执行 pytest（仓库布局），失败即中止发布；成功后生成 `build_manifest.json`：每个发布文件的 SHA-256 + git commit SHA，**生产部署后可对账运行中的文件是否与仓库逐字节一致** |

能力对照（四轮评审表格逐项回应）：源缺失强制失败 ✅ ／ 全量源文件完整性验证 ✅（哈希清单）／ 检查生产部署版本 ✅（manifest 对账，人工在服务器执行一条命令）／ 自动 pytest 阻止发布 ✅（脚本内置，不再靠人工）。

**仍需诚实声明的边界**：manifest 生成与生产对账之间是"推送-拉取"两步，脚本本身不能主动读生产机；对账命令已写入脚本输出提示，是否执行仍是人工流程。完全自动化需 CI/CD（路线图 #9）。

### 5t.3 Memory 首建档并发边界（四轮发现 P2 → **五轮已修复**）

四轮评审正确指出：三个 Memory 函数在 `ver is None`（尚无 Memory 行）时的写入路径没有版本保护——两个并发请求同时对无 Memory 记录的学生首次建档，可能互相覆盖。

五轮评审进一步质疑了"一个学生一个连接器 = 调用串行"的前提（**一个连接器不必然等于单请求，平台 single-flight 不可验证**），我们接受该质疑并直接实现修复（不再挂 P2）：

**v1.7 修复：无行时版本按 1，统一走条件更新路径**

```python
ver = mem.get("version", 1)          # 无行视为 version=1
db.upsert_student_memory(student_id, expected_version=ver, **fields)
```

db 层 `expected_version` 路径本身已是原子的：`INSERT IGNORE INTO student_memory(student_id, version) VALUES (%s, 1)` 原子建行 + `UPDATE ... WHERE student_id=%s AND version=%s` 条件写入，rowcount==0 抛 `MemoryConflict` → 网关 `_retry_on_conflict` 重读合并重试。并发首写的效果：A 建行并成功（version→2）；B 条件更新失败 → 重读 → 合并 A 的内容后再写。**两个写者的 weak_topics 都保留，ver=None 覆盖窗口消除**。三处（`_update_profile_locked`/`_save_memory_locked`/`_auto_solve_on_repeat_success`）全部统一。并发测试 `test_first_creation_concurrent_no_overwrite`（双线程 Barrier 同时 save_memory）锁定。

---

## 5quater. 五轮评审修复记录（v1.7，2026-10-30）

第五轮审查结论"有条件放行"：P1 四项中三项 PASS；**扫描豁免 FAIL**（行级豁免过宽）、manifest 覆盖 PARTIAL、首建档"条件接受"（但质疑单连接器≠单请求）。全部处置如下：

### 5q.1 DESIGN 扫描豁免改章节级（本轮主要 FAIL 修复）

**漏洞（评审攻击样例成立）**：v1.6 的豁免按行首字符（`|`/`#`/`>`/README/教训…）判定，未检查所在章节——在 DESIGN 任意位置（如快速开始）插入 `> 生产启动命令：python -m app.mcp_gateway` 或表格行即可逃过扫描。另发现 `DESIGN_EXEMPT_LINE` 为未使用的死代码。

**修复**：豁免改为**真正的章节级**——解析 Markdown 标题流，仅当行处于修复记录章节（`## 5.` / `## 5bis.` / `## 5ter.` / `## 5quater.` / `## 6.` 标题之后、下一个 H2 之前）才豁免；任何其他章节的 blockquote/表格/普通行命中旧串一律 FAIL。死代码已删除。自测：快速开始章节插入 `> 旧命令` 样例 → 扫描 FAIL（拦截）；修复记录章节内历史引用 → PASS（放行）。

### 5q.2 动态对账取代"枚举旧版本正则"（评审：版本一致性应校验而非枚举）

评审指出 v1.6 规则集漏报 `version-1.5`、`tests-36/36` 等"新的旧值"。修复为双保险：

1. **动态对账（主）**：README 测试徽章必须精确等于 `tests-{CURRENT_TESTS}%2F{CURRENT_TESTS}`；README 版本徽章必须含 `version-{CURRENT_VERSION}`；DESIGN 头部版本行必须精确匹配 `> 版本：v{CURRENT_VERSION} `；测试函数数量必须等于基线；README 工具清单**解析后集合精确相等**（第 10 个未知工具会 FAIL）。任何回退/漂移都拦截，无需枚举。
2. **兜底正则（辅）**：`tests-3[0-9]%2F3[0-9]`、`version-1.[0-7]` 等宽匹配规则兜底非徽章位置的残留。

同时按评审建议把 import 残留检查升级为 **AST 扫描**（`ast.walk` 覆盖 `import app` / `from app.x import` / 函数内延迟导入等所有形态），不再依赖文本正则。

### 5q.3 manifest 固定清单 + 专家包纳入 + --verify blob 对账

| 评审问题 | 修复 |
|---|---|
| `MANIFEST_FILES` 导入时 `exists()` 静默过滤：目标文件被删→重新复制→仍不进 manifest | **固定必需清单**（10 个文件），生成前逐一校验存在性，缺失即 fail |
| 专家包不在对账范围 | `expert-package/.codebuddy-plugin/plugin.json` 与 `expert-package/agents/cie-chem-coach.md` 纳入 manifest（修正了 v1.6 写错的仓库根 `plugin.json` 路径） |
| `git_commit` 含义不明（02fc71b vs 1ba6734） | 字段改名 `release_commit`（生成时 HEAD），新增 `workspace_clean` 标志；dirty 时输出提示"commit 后必须 --verify" |
| 脚本不校验工作区与 git tree 对应 | **新增 `--verify` 模式**：从 release commit 逐文件 `git show` 取 blob 重新计算 SHA-256 与 manifest 比对，不一致即非零退出。发布固化为三步：sync → commit → `--verify` |

诚实声明（评审已指出，维持）：SHA-256 不是数字签名，manifest 与代码同仓提交，防的是"无意漂移"而非"恶意替换"；绕过脚本直接 cp+commit 目前仍无法被仓库侧强制（需 GitHub Actions required check，见路线图 #9——本轮已把门禁收敛为单脚本单命令，CI 化时原样搬进 workflow 即可）。

### 5q.4 首建档并发：接受质疑，直接修复（不再 P2）

评审正确指出"一个连接器不必然等于单请求"，平台 single-flight 不可验证，P2 依据不充分。v1.7 直接实现评审给出的最小方案（详见 §5t.3）：三处 Memory 写入统一 `ver = mem.get("version", 1)` 走 `INSERT IGNORE` 原子建行 + 条件 UPDATE，复用既有 `_retry_on_conflict`。新增并发测试 `test_first_creation_concurrent_no_overwrite`（38 用例）。

### 5q.5 其他收尾

- `.env` 注入优先级反转：先剥离 shell 中残留的 `MYSQL_*` 再注入 chemask `.env`（防止测试连到开发者 shell 里碰巧存在的另一套库）；运行前打印脱敏库目标（host:port/db，无密码）。
- v1.4 表格中 `33/33` 标注为历史记录（"v1.4 当时的 33/33"），不篡改历史数字。
- 评审提到的"绕过脚本直接提交"与"CI 强制"维持路线图 #9 既有结论，本轮未新增虚假声明。

---

## 6. 一轮评审修复记录（v1.3，2026-10-30）

收到一份高质量外部评审（判 6/10 生产级），逐条核实后修复情况：

| 评审指控 | 核实结果 | 处置 |
|---|---|---|
| 幂等 check→insert 有并发竞态 | ✅ 属实 | **已修**：UNIQUE 约束 + 原子插入 + 6 线程并发测试 |
| idem_key 塞 weak_points 混杂 | ✅ 属实 | **已修**：独立列 + 存量数据迁移 |
| Memory 并发 Lost Update | ✅ 属实 | **已修**：version 乐观锁 + 重试装饰器 + 冲突注入测试 |
| get_profile 字符串截断产生非法 JSON | ✅ 属实（真 bug） | **已修**：结构化裁剪 `_trim_to_limit` + JSON 合法性测试 |
| 仓库缺 db.py 不可运行 / 目录与 import 不一致 | ✅ 属实 | **已修**：db.py 入仓（gateway/ 自包含）+ 全部 import 修正 + 迁移脚本补齐 v1.3 |
| class_summary 靠 label 字符串判权限 | ✅ 属实 | **已修**：role 列 RBAC + 迁移升级存量 + 三向测试 |
| get_report 绕过业务层直连 DB | ⚠️ 文档歧义 | **已澄清**：架构本就是"网关=数据访问层直连 DB"（见 §2），README 错误表述已更正；get_report 的 SQL 已收编进 db 层保持一致性 |
| 应基于官方 MCP SDK / 协议偏旧 | ⚠️ 有意识取舍 | **已声明**（§3.8）：兼容具体生产客户端优先，迁移路径已预留，文档不再宣称"标准实现" |
| 限流应 Redis 分布式 | ⚠️ 属实但超前 | **列入路线图** #1（当前单实例） |

## 7. 快速开始

```bash
# 1. 配置 .env（MySQL 连接；本地开发走 SSH 隧道 3307）
cat > .env << 'EOF'
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3307
MYSQL_USER=chem_ro
MYSQL_PASSWORD=***
MYSQL_DB=chemask
EOF

# 2. 迁移（幂等，安全重跑）
python ops/mcp_migrate.py

# 3. 起网关
uvicorn gateway.mcp_gateway:app --host 127.0.0.1 --port 8721

# 4. 签发学生 Token（明文只显示一次）
python ops/mcp_token.py issue --new-student 小明

# 5. 跑测试
pytest tests/test_mcp_gateway.py -q
```

生产部署：systemd 服务（`127.0.0.1:8721`，仅本机监听）+ nginx HTTPS 反代（`/chem-mcp/`）。MySQL 永不暴露公网。

## 8. 目录说明

```
chem-mcp/
├── gateway/
│   ├── mcp_gateway.py    # MCP 网关：路由/鉴权/RBAC/限流/9 工具/裁剪/乐观锁重试
│   └── db.py             # 数据层：连接池/5 张 MCP 表/原子幂等/乐观锁 upsert
├── expert-package/       # 「CIE化学考情教练」专家包（plugin.json + 提示词 v3 + 安装指南）
├── ops/
│   ├── mcp_migrate.py    # DDL 迁移（幂等，含 v1.3 三项升级与存量数据搬迁）
│   ├── mcp_token.py      # Token 签发/吊销/列表 CLI（--role teacher）
│   └── mcp_echo_server.py# 平台连通性验证用最小 MCP 服务器（零业务依赖）
├── tests/
│   └── test_mcp_gateway.py  # 37 用例：协议/鉴权/吊销/限流语义(含并发)/并发幂等/JSON 合法性与硬上限/乐观锁/RBAC
├── DESIGN.md             # 本文档
└── README.md             # 快速概览
```
