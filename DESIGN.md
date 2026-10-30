# chem-mcp 技术方案与设计背景

> 版本：v1.3 ｜ 更新：2026-10-30 ｜ 状态：生产运行中（单实例试点规模）
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
2. **错误语义**：8 个工具需要细粒度的数据库级错误映射（唯一键冲突=幂等重复、行版本不符=乐观锁冲突），跨 HTTP 层传递这些语义要设计一套错误协议；
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

单写者场景（当前试点：一个学生一个连接器）用无锁路径（`expected_version=None`），行为与 v1.2 兼容。

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
2. **协议面极小**。本网关只需 `initialize` / `tools/list` / `tools/call` / notifications 四个方法，无 resources/prompts/sampling 等能力。手写面 ≈ 60 行路由代码，且有 30 个测试用例锁定行为。
3. **迁移路径已预留**。`_TOOL_IMPL` 字典是唯一的工具注册点，协议层与工具层完全解耦——若后续需要官方 SDK（例如要兼容更多 MCP 宿主），只需替换路由层，8 个工具实现零改动。

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

## 5. 外部评审修复记录（2026-10-30）

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

## 6. 快速开始

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

## 7. 目录说明

```
chem-mcp/
├── gateway/
│   ├── mcp_gateway.py    # MCP 网关：路由/鉴权/RBAC/限流/8 工具/裁剪/乐观锁重试
│   └── db.py             # 数据层：连接池/5 张 MCP 表/原子幂等/乐观锁 upsert
├── expert-package/       # 「CIE化学考情教练」专家包（plugin.json + 提示词 v3 + 安装指南）
├── ops/
│   ├── mcp_migrate.py    # DDL 迁移（幂等，含 v1.3 三项升级与存量数据搬迁）
│   ├── mcp_token.py      # Token 签发/吊销/列表 CLI（--role teacher）
│   └── mcp_echo_server.py# 平台连通性验证用最小 MCP 服务器（零业务依赖）
├── tests/
│   └── test_mcp_gateway.py  # 30 用例：协议/鉴权/吊销/限流语义/并发幂等/JSON 合法性/乐观锁/RBAC
├── DESIGN.md             # 本文档
└── README.md             # 快速概览
```
