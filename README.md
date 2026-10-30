# chem-mcp · AI问化学 WorkBuddy 平台版数据层

> **学生召唤 WorkBuddy 专家 → MCP 网关（Token 硬身份）→ chemask API → MySQL**
> 让"专家记住学生"跨会话成立：长期记忆、作答流水、错题本、学情报告全部落自有数据库。

[![tests](https://img.shields.io/badge/tests-23%2F23-brightgreen)]() [![protocol](https://img.shields.io/badge/MCP-Streamable%20HTTP%20%2B%20SSE-blue)]()

## 这是什么

《AI问化学 · WorkBuddy 平台版开发方案》（v1.2）的 **§8.4 MCP 数据层** 生产实现。
平台原生没有作答数据层、不向专家暴露召唤者身份——本项目用一个薄 MCP 网关把两块短板都补上：

- **Token ↔ 学生硬绑定**：一人一 Token（只存 SHA-256），网关反查即知身份，专家调用天然带身份
- **8 个白名单工具**：get_profile / update_profile / save_memory / record_attempt / list_mistakes / get_mastery / class_summary（仅教师 Token）/ get_report
- **长期记忆三层设计**：`student_memory` 一人一行摘要（增量合并：卡点并集、计划覆盖、taught_topics 防重复讲解）+ dialogue_log / attempts / reports 明细可回溯
- **防膨胀**：摘要 ≤2000 字符超限压缩；get_profile 返回上限 3K 字符，报告全文按需经 get_report 拉取
- **安全**：3306 永不出公网、凭据只在服务器 .env、限流只计业务调用（保活流量免计费）、Token 支持吊销/轮换

## 目录结构

```
chem-mcp/
├── gateway/
│   └── mcp_gateway.py        # FastAPI MCP 网关（POST JSON-RPC + GET SSE 长流心跳）
├── expert-package/           # WorkBuddy 专家包「CIE化学考情教练」
│   ├── .codebuddy-plugin/plugin.json
│   └── agents/cie-chem-coach.md   # 提示词 v3（身份识别/首访五问/检索硬规则/六段式报告）
├── ops/
│   ├── mcp_migrate.py        # DDL 迁移：api_keys/student_memory/dialogue_log/reports + students.user_id（幂等）
│   ├── mcp_token.py          # Token 签发/吊销/列表 CLI（明文仅签发时展示一次）
│   └── mcp_echo_server.py    # D1-0a 平台验证用 echo MCP（零依赖）
├── tests/
│   └── test_mcp_gateway.py   # 23 用例：协议/鉴权/限流(仅业务计费)/幂等/合并状态机/权限/吊销
└── docs/
```

## MCP 协议实现要点（三连修复的教训）

WorkBuddy 及标准宿主走 **Streamable HTTP**，自研服务端必须同时满足：

1. **POST /mcp**：JSON-RPC（initialize / tools/list / tools/call）
2. **GET /mcp**：`Accept: text/event-stream` 时返回 200 + SSE 长流（每 10s `: ka` 心跳，`X-Accel-Buffering: no` 防 nginx 缓冲）——只注册 POST 会让客户端收到 405 判定传输失败
3. **通知**：无 `id` 字段的 JSON-RPC 通知（如 `notifications/initialized`）必须返回 **202 空体**，返回 400 会让客户端中断握手
4. **限流语义**：只对 `tools/call` 计费。保活/轮询流量计入配额 = 系统自我拒绝服务（实测 WorkBuddy 连接器以 ~3s 间隔轮询）

## 快速开始

```bash
# 1. 迁移（幂等，可重复跑）
python ops/mcp_migrate.py

# 2. 签发学生 Token（明文只显示一次）
python ops/mcp_token.py issue --new-student 小明
# 教师聚合 Token（class_summary 可用）
python ops/mcp_token.py issue --student-id 3 --label 班主任 --teacher

# 3. 起网关（本地 8721）
python -m gateway.mcp_gateway --port 8721

# 4. WorkBuddy → 连接器 → 自定义 → 添加 MCP
#    URL: https://your-domain/chem-mcp/mcp
#    Header: Authorization: Bearer <token>

# 5. 测试（连 MySQL，走 .env）
python -m pytest tests/ -q
```

生产部署（lobster）：`chem-mcp.service`（127.0.0.1:8721）+ nginx `/chem-mcp/` 反代。

## 架构位置

```
WorkBuddy 专家（CIE化学考情教练）
   │  MCP (HTTPS + Bearer Token)
   ▼
chem-mcp 网关（本项目，薄封装 ~500 行）
   │  复用 chemask 既有业务规则（错题本满分移出/掌握度口径）
   ▼
chemask API ──► MySQL（attempts/student_memory/... 同轴 student_id）
```

不直连 MySQL：绕过业务规则会产生口径不一致的脏数据，且 SQL 注入面更大。

## 相关文档

- 方案总纲：《AI问化学 WorkBuddy 平台版开发方案（完整版）》（WorkBuddy 资料库）
- 自研版对照：[chemask](https://github.com/lanmao1123/chemask)（私有）
