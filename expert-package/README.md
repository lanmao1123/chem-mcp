# 专家包：CIE化学考情教练（cie-chem-coach）

WorkBuddy 专家包（Agent 型）——与 `gateway/` 配合，构成"平台版 AI问化学"的学生侧入口。

## 文件清单

```
expert-package/
├── .codebuddy-plugin/
│   └── plugin.json        # 包清单：displayName/profession/tags/quickPrompts/分类
├── agents/
│   └── cie-chem-coach.md  # 提示词 v3 全文（frontmatter + 正文）
└── README.md              # 本文件
```

## 功能

- 考纲内概念答疑（苏格拉底模式 + 题型分流，引用可溯源）
- 真题讲评（按官方 mark scheme 拆得分点）
- 长期记忆：经 chem-mcp 网关读写学生档案，跨会话记住卡点/计划/偏好
- 六段式学情报告生成（含 14 天行动计划）

## 使用示例（quickPrompts）

- "我卡在卤代烃机理题，讲一下"
- "帮我拆 2024 年 3 月卷 Q3(b) 的得分点"
- "生成我的学情报告"

## 提示词设计要点（为什么这么写）

| 设计 | 出处 | 作用 |
|---|---|---|
| 身份识别段（get_profile 返回即真相） | 方案 §8.4 Token 硬绑定 | 会话开头一次调用即"想起"学生 |
| 首访五问（仅无记录学生） | §7.1 | 建档进 student_memory，此后免问 |
| 数据规则（8 工具白名单） | §8.4/8.5 | 读写走 MCP，档案目录降级备份 |
| 检索硬规则（锁定空间） | §2.3 ⚗实测 | 防全局检索污染（定哥专家翻车实录） |
| 苏格拉底分强度（概念题引导/讲评直给） | §7.2 | 全题引导会劝退，按题型平衡 |
| 引用格式定死（【真题 文件名 ▶ 题号】） | §5.3 | 文件级引用补偿，答案可验证 |
| 六段式报告模板 | 定哥案例改造 | 输出直接可执行 |
| MCP 故障降级话术 | §8.4 | 数据层故障不拒绝答疑 |

## 安装方式

**方式一：脚本安装（推荐）**

```bash
cp -r expert-package ~/.workbuddy/plugins/marketplaces/my-experts/plugins/cie-chem-coach
# 在 WorkBuddy 专家中心刷新，或用 expert-manager skill 注册：
# register_expert.py ~/.workbuddy/plugins/marketplaces/my-experts/plugins/cie-chem-coach
```

**方式二：打包导入**——用 expert-manager skill 的 `package_expert.py` 打 zip，
WorkBuddy 专家中心 → 导入专家。

## 前置条件

- WorkBuddy 已连接 `chem-mcp` 自定义连接器（见主 README 快速开始）
- 学生持有老师签发的个人 Token（`ops/mcp_token.py issue`），填入连接器 Authorization
- （可选）召唤者账号可见「化学资料库」资料库（检索答疑用；数据功能不依赖）

## 修改与分发

- 提示词迭代：改 `agents/cie-chem-coach.md` → 重装 → 固定评测集回归（方案 §9）
- 分发给学生：本目录打包发给学生导入；学生各自粘贴自己的 Token
- 注意：`plugin.json` 的 `name` 与 `agents/` 下的 md 文件名是唯一标识，不可改名
