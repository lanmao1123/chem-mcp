# chem-mcp · AI问化学 WorkBuddy 平台版数据层

> **学生召唤 WorkBuddy 专家 → MCP 网关（Token 硬身份）→ 数据层 → MySQL**
> 让"专家记住学生"跨会话成立：长期记忆、作答流水、错题本、学情报告全部落自有数据库。

[![tests](https://img.shields.io/badge/tests-30%2F30-brightgreen)]() [![protocol](https://img.shields.io/badge/MCP-compatible--JSON--RPC%20%2B%20SSE-blue)]() [![version](https://img.shields.io/badge/version-1.3-orange)]()

## 这是什么

《AI问化学 · WorkBuddy 平台版开发方案》的 **MCP 数据层**生产实现。
平台原生没有作答数据层、不向专家暴露召唤者身份——本项目用一个薄 MCP 网关把两块短板都补上：

- **Token ↔ 学生硬绑定**：一人一 Token（只存 SHA-256），网关反查即知身份；**student_id 由网关注入，不信任 LLM 传参**（防跨学生数据泄露）
- **8 个白名单工具**：get_profile / update_profile / save_memory / record_attempt / list_mistakes / get_mastery / class_summary（仅教师 role）/ get_report
- **长期记忆三层设计**：`student_memory` 一人一行摘要（增量合并：卡点并集、计划覆盖、taught_topics 防重复讲解）+ dialogue_log / attempts / reports 明细可回溯
- **并发正确性**（v1.3）：`record_attempt` 幂等由 `UNIQUE(student_id,qid,idem_key)` 原子保证；记忆更新走 version 乐观锁 + 自动重试，杜绝并发 Lost Update
- **输出治理**（v1.3）：get_profile 结构化裁剪至 3K 字符，**任何输入都返回合法 JSON**；报告全文按需经 get_report 拉取
- **RBAC**（v1.3）：`api_keys.role` 正式权限列（student/teacher），替代字符串约定
- **安全**：3306 永不出公网、凭据只在服务器 .env、限流只计业务调用（保活流量免计费）、Token 支持吊销/轮换

## 📐 完整技术方案与设计背景

**请先读 [DESIGN.md](./DESIGN.md)** —— 包含项目背景、要解决的三个平台限制、每个设计决策的动机与取舍（为什么直连 MySQL、为什么手写协议层、记忆为什么分层）、诚实的已知局限清单与路线图、以及外部评审修复的完整记录。

## 目录结构

```
chem-mcp/
├── gateway/
│   ├── mcp_gateway.py        # FastAPI MCP 网关（POST JSON-RPC + GET SSE 长流心跳）
│   └── db.py                 # 数据层：连接池/MCP 表/原子幂等/乐观锁 upsert
├── expert-package/           # WorkBuddy 专家包「CIE化学考情教练」
│   ├── .codebuddy-plugin/plugin.json
│   ├── agents/cie-chem-coach.md   # 提示词 v3（身份识别/首访五问/检索硬规则/六段式报告）
│   └── README.md             # 专家包安装与分发指南
├── ops/
│   ├── mcp_migrate.py        # DDL 迁移（幂等；含 v1.3：idem_key 唯一键/role 列/version 列 + 存量数据搬迁）
│   ├── mcp_token.py          # Token 签发/吊销/列表 CLI（--role teacher；明文仅签发时展示一次）
│   └── mcp_echo_server.py    # 平台连通性验证用 echo MCP（零业务依赖）
├── tests/
│   └── test_mcp_gateway.py   # 30 用例：协议/鉴权/限流(仅业务计费)/并发幂等/JSON 合法性/乐观锁/RBAC/合并状态机
├── DESIGN.md                 # ★ 技术方案与设计背景（审查者从这里开始）
└── README.md                 # 本文件
```

## MCP 协议实现要点（三连修复的教训）

WorkBuddy 及标准宿主走 **Streamable HTTP**，自研服务端必须同时满足：

1. **POST /mcp**：JSON-RPC（initialize / tools/list / tools/call）
2. **GET /mcp**：`Accept: text/event-stream` 时返回 200 + SSE 长流（每 10s `: ka` 心跳，`X-Accel-Buffering: no` 防 nginx 缓冲）——只注册 POST 会让客户端收到 405 判定传输失败
3. **通知**：无 `id` 字段的 JSON-RPC 通知（如 `notifications/initialized`）必须返回 **202 空体**，返回 400 会让客户端中断握手
4. **限流语义**：只对 `tools/call` 计费。保活/轮询流量计入配额 = 系统自我拒绝服务（实测 WorkBuddy 连接器以 ~3s 间隔轮询）

> **定位声明**：本项目是 *MCP-compatible data gateway for a specific production client*（WorkBuddy 连接器），协议面按其真实行为适配并经 30 个测试锁定；非 MCP 参考实现。协议层与工具层完全解耦，需兼容更多宿主时可平滑迁移至官方 SDK（工具实现零改动）。详见 DESIGN.md §3.8。

## 快速开始

```bash
# 1. 配置 .env（MySQL 连接）
#    MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DB

# 2. 迁移（幂等，可重复跑）
python ops/mcp_migrate.py

# 3. 签发学生 Token（明文只显示一次）
python ops/mcp_token.py issue --new-student 小明
# 教师聚合 Token（class_summary 可用；v1.3 走正式 role）
python ops/mcp_token.py issue --student-id 3 --label 班主任 --role teacher

# 4. 起网关（本地 8721）
python -m gateway.mcp_gateway --port 8721

# 5. WorkBuddy → 连接器 → 自定义 → 添加 MCP
#    URL: https://your-domain/chem-mcp/mcp
#    Header: Authorization: Bearer <token>

# 6. 测试（连 MySQL，走 .env）
python -m pytest tests/ -q
```

生产部署：systemd 服务（`127.0.0.1:8721`，仅本机监听）+ nginx HTTPS 反代（`/chem-mcp/`）。

## 架构位置

```
WorkBuddy 专家（CIE化学考情教练）
   │  MCP (HTTPS + Bearer Token)
   ▼
chem-mcp 网关（本项目，~550 行）
   │  身份反查 / RBAC / 业务限流 / 幂等 / 乐观锁 / 输出裁剪
   ▼
gateway/db.py（连接池）──► MySQL（attempts/student_memory/... 同轴 student_id）
```

**为什么直连 MySQL 而非经 chemask HTTP API**：网关与主应用同机部署，HTTP 中间层只增加延迟与故障点；数据库级错误语义（唯一键冲突=幂等重复、版本不符=乐观锁冲突）需要细粒度映射；乐观锁两步操作须在同一事务内。与 chemask 共享同一份 db.py 保证业务口径一致。详见 DESIGN.md §2。

## 相关文档

- **[DESIGN.md](./DESIGN.md)**：技术方案与设计背景（背景/决策/取舍/局限/评审修复记录）
- 方案总纲：《AI问化学 WorkBuddy 平台版开发方案（完整版）》（WorkBuddy 资料库）
- 自研版对照：[chemask](https://github.com/lanmao1123/chemask)（私有）
