# chem-mcp · AI问化学 WorkBuddy 平台版数据层

> **学生召唤 WorkBuddy 专家 → MCP 网关（Token 硬身份）→ 数据层 → MySQL**
> 让"专家记住学生"跨会话成立：长期记忆、作答流水、错题本、学情报告全部落自有数据库。

[![tests](https://img.shields.io/badge/tests-84%2F84-brightgreen)]() [![protocol](https://img.shields.io/badge/MCP-compatible--JSON--RPC%20%2B%20SSE-blue)]() [![version](https://img.shields.io/badge/version-1.8-orange)]()

## 这是什么

《AI问化学 · WorkBuddy 平台版开发方案》的 **MCP 数据层**生产实现。
平台原生没有作答数据层、不向专家暴露召唤者身份——本项目用一个薄 MCP 网关把两块短板都补上：

- **Token ↔ 学生硬绑定**：一人一 Token（只存 SHA-256），网关反查即知身份；**student_id 由网关注入，不信任 LLM 传参**（防跨学生数据泄露）
- **9 个白名单工具**：get_profile / update_profile / save_memory / record_attempt（幂等） / list_mistakes / get_mastery / class_summary（仅教师） / get_report / save_report（幂等）
- **长期记忆三层设计**：`student_memory` 一人一行摘要（增量合并：卡点并集、计划覆盖、taught_topics 防重复讲解）+ dialogue_log / attempts / reports 明细可回溯
- **并发正确性**（v1.3/v1.5/v1.7）：`record_attempt` 幂等由 `UNIQUE(student_id,qid,idem_key)` 原子保证；记忆更新（含**并发首建档**）统一走"原子建行 + version 条件更新"（内容与版本同源读取）+ 自动重试，杜绝 Lost Update；限流经 `FOR UPDATE` 行锁串行化，并发下不可穿越配额
- **输出治理**（v1.3/v1.5）：get_profile 结构化裁剪至 3K 字符硬上限（含终极兜底），**任何输入都返回合法 JSON**；报告全文按需经 get_report 拉取
- **发布一致性**（v1.6–v1.8）：`sync_from_chemask.py` fail-closed 门禁（源缺失即失败）+ **章节级扫描豁免**（H2 标题精确匹配，伪造章节编号无效）+ **动态对账**（徽章恰好一个且值精确、版本行唯一、测试数由 AST 统计、工具清单集合与顺序相等、Gateway 实际注册工具比对）+ AST import 扫描（含 `importlib`/`__import__` 别名）+ pytest 门禁 + manifest（13 文件含专家包与依赖清单）+ `--verify`（release_commit 祖先关系/两段式 diff 白名单/HEAD 与 blob 双重对账）；门禁自身由 `tests/test_release_gate.py` 回归保护
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
│   ├── mcp_echo_server.py    # 平台连通性验证用 echo MCP（零业务依赖）
│   └── verify_deployment.py  # 生产部署逐文件 SHA-256 对账（服务器本地执行，不联网）
├── tests/
│   ├── test_mcp_gateway.py   # 38 例（本文件）：协议/鉴权/限流(仅业务计费)/并发幂等/JSON 合法性/乐观锁/并发首建档/RBAC/合并状态机
│   └── test_release_gate.py  # 46 例（本文件）：发布门禁自身回归（章节豁免攻击/动态对账/AST 动态导入/manifest 临时仓库/.env 注入）
├── DESIGN.md                 # ★ 技术方案与设计背景（审查者从这里开始）
└── README.md                 # 本文件
```

## MCP 协议实现要点（三连修复的教训）

WorkBuddy 及标准宿主走 **Streamable HTTP**，自研服务端必须同时满足：

1. **POST /mcp**：JSON-RPC（initialize / tools/list / tools/call）
2. **GET /mcp**：`Accept: text/event-stream` 时返回 200 + SSE 长流（每 10s `: ka` 心跳，`X-Accel-Buffering: no` 防 nginx 缓冲）——只注册 POST 会让客户端收到 405 判定传输失败
3. **通知**：无 `id` 字段的 JSON-RPC 通知（如 `notifications/initialized`）必须返回 **202 空体**，返回 400 会让客户端中断握手
4. **限流语义**：只对 `tools/call` 计费。保活/轮询流量计入配额 = 系统自我拒绝服务（实测 WorkBuddy 连接器以 ~3s 间隔轮询）

> **定位声明**：本项目是 *MCP-compatible data gateway for a specific production client*（WorkBuddy 连接器），协议面按其真实行为适配并经 84 个测试锁定（38 个协议/数据层 + 46 个发布门禁回归）；非 MCP 参考实现。协议层与工具层完全解耦，需兼容更多宿主时可平滑迁移至官方 SDK（工具实现零改动）。详见 DESIGN.md §3.8。

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
uvicorn gateway.mcp_gateway:app --host 127.0.0.1 --port 8721

# 5. WorkBuddy → 连接器 → 自定义 → 添加 MCP
#    URL: https://your-domain/chem-mcp/mcp
#    Header: Authorization: Bearer <token>

# 6. 测试（连 MySQL，走 .env）
python -m pytest tests/ -q                     # 全部 84 例（含发布门禁回归）
python -m pytest tests/test_release_gate.py -q  # 仅门禁回归（无需数据库之外的外部依赖）
```

生产部署：systemd 服务（`127.0.0.1:8721`，仅本机监听）+ nginx HTTPS 反代（`/chem-mcp/`）。

## 发布流程（六阶段，无歧义闭环）

代码的真实来源是 `../chemask` 工作区；本仓库是面向审查与部署的发布视图。**每次发布必须按顺序执行**：

| 阶段 | 命令 | 做什么 | 失败行为 |
|---|---|---|---|
| 1 同步 + 门禁 | `python3 sync_from_chemask.py` | 源文件预检 → 复制 → import/启动命令改写 → 章节级旧内容扫描 + 动态对账 + AST 扫描 → pytest（38+46 例）→ 必需文件检查 | 任一不符即非零退出，零发布 |
| 2 业务发布提交 | `git add -A && git commit -m "release vX.Y"` | 提交全部业务发布文件 | — |
| 3 固化 manifest | `python3 sync_from_chemask.py --finalize-manifest` | 校验工作区干净 → 为 13 个固定文件算 SHA-256 → 记录 `release_commit`（=当前 HEAD） | 工作区 dirty 即拒绝固化 |
| 4 提交 manifest | `git add build_manifest.json && git commit -m "chore: finalize release manifest"` | manifest 单独一次提交（避免自引用） | — |
| 5 验证 | `python3 sync_from_chemask.py --verify` | manifest 结构/文件集合/哈希格式/`release_commit` 存在性与祖先关系/两段式 diff 严格只含 `build_manifest.json`/HEAD 与 release blob 双重哈希对账/工作区干净 | 任一不符即非零退出 |
| 6 部署对账 | 上传后服务器执行 `python ops/verify_deployment.py build_manifest.json` | 生产机本地逐文件 SHA-256 对账，打印 version + release_commit | 任一文件不符即非零退出 |

**`release_commit` 的语义**：它是**发布文件内容对应的 commit**，不等于"任何时候的 `main` HEAD"。两段式发布下，HEAD 会比它多一个只改 `build_manifest.json` 的提交——`--verify` 用 `git diff --name-only release_commit..HEAD` 严格校验差异集合恰为 `{"build_manifest.json"}`，任何业务文件（网关/DB/测试/README/DESIGN/专家包/ops）在 release_commit 之后被改动都会验证失败。

**为什么需要单独的 manifest 固化提交**：manifest 记录的文件哈希必须对应一个已提交的 Git 树；若把 manifest 与业务文件放在同一 commit，则 manifest 自身内容无法参与该 commit 的树校验（自引用）。因此拆为"业务提交 → 固化 → manifest 提交 → 验证"。

**门禁的三层边界（诚实声明）**：
1. **本地脚本门禁**：`sync_from_chemask.py`，手动执行，绕过方式存在（直接 `cp` + `git commit` 不跑脚本）。
2. **CI 门禁**：`.github/workflows/release-gate.yml` 在 push/PR 上执行静态检查、动态对账、AST 扫描、`--verify` 与 MySQL 集成测试；**但 branch protection / required status checks 需仓库管理员在 GitHub 设置中手工开启**，仓库代码无法自我强制。
3. **生产对账**：`ops/verify_deployment.py` 在服务器本地执行，可证明"运行文件 = 发布文件"。脚本**不主动 SSH 访问生产机**，因此"部署后自动对账"仍未自动化，需人工触发。

门禁脚本自身由 `tests/test_release_gate.py`（46 例，含真实临时 Git 仓库的 12 个 `--verify` 场景）回归保护。

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

- **[DESIGN.md](./DESIGN.md)**：技术方案与设计背景（背景/决策/取舍/局限/六轮评审修复记录 §5–§5quin）
- 方案总纲：《AI问化学 WorkBuddy 平台版开发方案（完整版）》（WorkBuddy 资料库）
- 自研版对照：[chemask](https://github.com/lanmao1123/chemask)（私有）
