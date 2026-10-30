"""chemask 工作区 → chem-mcp 仓库的发布同步与门禁脚本（v1.8，六轮评审加固）。

用法：
    python3 sync_from_chemask.py                 完整发布门禁（阶段一：同步+扫描+测试）
    python3 sync_from_chemask.py --check-only    CI 模式：不依赖 ../chemask，仅仓库静态门禁
    python3 sync_from_chemask.py --finalize-manifest  阶段三：发布内容已 commit，固化 manifest
    python3 sync_from_chemask.py --verify        严格发布验证（阶段五）

发布六阶段（详见 DESIGN §5quin"发布流程"与 README）：
    1. python3 sync_from_chemask.py                        # 同步 + 全门禁
    2. git add -A && git commit -m "release vX.Y"          # 业务发布提交
    3. python3 sync_from_chemask.py --finalize-manifest    # 固化 manifest（指向当前 HEAD）
    4. git add build_manifest.json && git commit -m "chore: finalize release manifest"
    5. python3 sync_from_chemask.py --verify               # blob 对账，全过才算发布完成
    6. 上传服务器 + ops/verify_deployment.py 逐文件对账

fail-closed 设计（四/五/六轮评审逐项落实）：
  1. 源文件缺失即非零退出，零复制；
  2. DESIGN 历史豁免=章节级精确标题匹配（H2 标题精确等于白名单，伪造"## 5. 快速开始"不豁免）；
  3. 动态对账=精确计数（徽章恰好一个、版本行唯一、测试数 AST 统计、工具清单集合+顺序相等、
     Gateway 实际注册工具与基线比对）；
  4. AST import 扫描覆盖 importlib/__import__/别名等动态导入形态；
  5. pytest 门禁（数量严格等于基线，真实 MySQL；缺 .env 且缺必要变量 fail-closed）；
  6. manifest 固定必需清单（不静默过滤）+ release_commit 祖先关系 + 两段式 diff 严格
     仅 build_manifest.json + workspace_clean 强制 + HEAD 与 release blob 双重对账；
  7. --verify 是严格验证器：任何一项不满足即非零退出（回归测试见 tests/test_release_gate.py）。
"""
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "chemask")

PAIRS = [
    ("app/mcp_gateway.py", "gateway/mcp_gateway.py"),
    ("app/db.py", "gateway/db.py"),
    ("tests/test_mcp_gateway.py", "tests/test_mcp_gateway.py"),
    ("prep/mcp_migrate.py", "ops/mcp_migrate.py"),
    ("prep/mcp_token.py", "ops/mcp_token.py"),
    ("prep/mcp_echo_server.py", "ops/mcp_echo_server.py"),
]

# ── 发布清单：固定必需文件（任何一轮新增正式发布文件都必须显式加入此处）─────────
MANIFEST_FILES = [
    "gateway/mcp_gateway.py", "gateway/db.py", "tests/test_mcp_gateway.py",
    "tests/test_release_gate.py",          # 六轮新增：发布门禁回归测试
    "ops/mcp_migrate.py", "ops/mcp_token.py", "ops/mcp_echo_server.py",
    "ops/verify_deployment.py",            # 六轮新增：生产对账脚本
    "README.md", "DESIGN.md",
    "requirements-release.txt",           # 六轮新增：CI/部署依赖清单（版本固定）
    "expert-package/.codebuddy-plugin/plugin.json",
    "expert-package/agents/cie-chem-coach.md",
]
# 明确不纳入 manifest 的文件及原因（写入文档，避免"漏项"争议）：
#   build_manifest.json —— 自引用
#   .github/workflows/release-gate.yml —— CI 配置，非部署产物；其正确性由 CI 自身运行验证

# ── 版本基线（升级版本时必须同步更新此处 + README 徽章 + DESIGN 头部 + 全部文档）──
CURRENT_VERSION = "1.8"
CURRENT_TESTS = 84          # 38 (test_mcp_gateway.py) + 46 (test_release_gate.py)
CURRENT_TOOLS = ["get_profile", "update_profile", "save_memory", "record_attempt",
                 "list_mistakes", "get_mastery", "class_summary", "get_report",
                 "save_report"]

# ── 兜底正则（非徽章位置的历史串；徽章/版本/测试数/工具清单由动态对账精确校验）────
# scope: "all" = 全部文件；"docs" = 仅文档（.py 的导入检查交给 AST 扫描，
# 因为 AST 能识别"except ImportError 守卫内的布局兼容回退"，文本正则不能）
STALE_PATTERNS = [
    (r"-m app\.mcp_gateway", "仓库侧旧启动命令 app.mcp_gateway", "all"),
    (r"^\s*(from|import) (app|prep)\b", "旧 import 路径", "docs"),
    (r"30 个测试用例|30 用例：|7 个白名单 MCP|8 个白名单工具\*\*", "v1.2/v1.4 历史旧串", "all"),
]

# DESIGN 历史修复章节：H2 标题【精确匹配】白名单（伪造"## 5. 快速开始"不豁免）
DESIGN_HISTORY_SECTIONS = frozenset({
    "## 5. 二轮评审修复记录（v1.4，2026-10-30）",
    "## 5bis. 三轮评审修复记录（v1.5，2026-10-30）",
    "## 5ter. 四轮评审修复记录（v1.6，2026-10-30）",
    "## 5quater. 五轮评审修复记录（v1.7，2026-10-30）",
    "## 5quin. 六轮评审修复记录（v1.8，2026-10-30）",
    "## 6. 一轮评审修复记录（v1.3，2026-10-30）",
})

# AST import 扫描目标（六轮新增 test_release_gate.py：其内容含攻击样例字符串，
# 属于测试数据而非残留，由 SCAN_EXEMPT_FILES 豁免文本扫描；AST 扫描仍照常执行）
AST_SCAN_FILES = ["gateway/mcp_gateway.py", "gateway/db.py",
                  "ops/mcp_migrate.py", "ops/mcp_token.py",
                  "ops/mcp_echo_server.py", "tests/test_mcp_gateway.py",
                  "tests/test_release_gate.py"]
# 文本扫描豁免：本脚本 + 回归测试（含旧串的攻击样例是测试数据）
SCAN_EXEMPT_FILES = frozenset({"sync_from_chemask.py", "tests/test_release_gate.py"})

# 活跃文档中的测试数描述（动态匹配，数字 ≠ CURRENT_TESTS 即旧描述）：
#   经/有/按/共/当前/用 N 个测试(用例|锁定|验证)
#   N 用例(本地跑|跑真实库)
#   N 用例：/: (目录列表形态)
# 历史章节（§5/§5bis/§5ter/§5quater/§5quin/§6）内引用由章节豁免兜底。
STALE_TEST_COUNT_PATTERN = re.compile(
    r"(?:经|有|按|共|当前|用)\s*(\d+)\s*个(?:测试(?:用例|锁定|锁定行为|验证))"
    r"|(\d+)\s*用例(?:本地跑|跑真实库)"
    r"|(\d+)\s*用例[：:]")


def fail(msg):
    print(f"[FAIL] {msg}")
    sys.exit(1)


def step4_scan():
    """兜底正则全量扫描（DESIGN 章节级豁免）+ 活跃文档旧测试数精确扫描。"""
    bad = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", ".venv", ".pytest_cache")]
        for f in files:
            if not f.endswith((".py", ".md", ".json")):
                continue
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, HERE)
            if rel in SCAN_EXEMPT_FILES:
                continue  # 本脚本/回归测试含旧串的攻击样例，属测试数据
            txt = open(fp, encoding="utf-8").read()
            design_exempt = get_design_exempt_lines(txt) if rel == "DESIGN.md" else set()
            lines = txt.splitlines()
            for pat, desc, scope in STALE_PATTERNS:
                if scope == "docs" and f.endswith(".py"):
                    continue  # .py 导入由 AST 扫描负责（含守卫豁免逻辑）
                for m in re.finditer(pat, txt, re.M):
                    ln = txt.count("\n", 0, m.start())
                    if rel == "DESIGN.md" and ln in design_exempt:
                        continue
                    ctx = lines[ln][:80] if ln < len(lines) else "?"
                    bad.append(f"{rel}:{ln + 1}: {desc} -> {ctx!r}")
            # 旧测试数扫描：应用于所有文件；DESIGN 仅豁免历史章节内的引用
            for m in STALE_TEST_COUNT_PATTERN.finditer(txt):
                num = m.group(1) or m.group(2) or m.group(3)
                if int(num) == CURRENT_TESTS:
                    continue  # 当前基线数字，合法
                ln = txt.count("\n", 0, m.start())
                if rel == "DESIGN.md" and ln in design_exempt:
                    continue  # 历史章节引用旧数字属真实历史，豁免
                ctx = lines[ln][:80] if ln < len(lines) else "?"
                bad.append(f"{rel}:{ln + 1}: 活跃文档测试数 {num} ≠ 基线 "
                           f"{CURRENT_TESTS} -> {ctx!r}")
    if bad:
        print("[FAIL] 旧版本内容残留（发布中止）：")
        for b in bad:
            print("    ", b)
        sys.exit(1)
    print(f"[ok] 扫描通过（{len(STALE_PATTERNS)} 条兜底正则 + 旧测试数动态扫描，"
          f"DESIGN 章节级豁免）")


# ─────────────────────────── 章节豁免（纯函数，可单测）───────────────────────────

def get_design_exempt_lines(text: str) -> set:
    """返回 DESIGN.md 中允许豁免的行号集合（0-based）。

    章节级豁免（六轮评审）：
    - H2 标题【精确匹配】DESIGN_HISTORY_SECTIONS 白名单才进入豁免状态；
    - 伪造标题（如"## 5. 快速开始（伪装的历史章节）"）不匹配白名单 → 不豁免；
    - H3/H4 不结束当前 H2 章节（继续豁免到下一个 H2）；
    - 仅豁免该 H2 之后、下一个 H2 之前的行。
    """
    lines = text.splitlines()
    exempt, in_history = set(), False
    for i, line in enumerate(lines):
        if line.startswith("## ") and not line.startswith("### "):
            in_history = line in DESIGN_HISTORY_SECTIONS
            continue
        if in_history:
            exempt.add(i)
    return exempt


# ─────────────────────────── 动态对账（纯函数，可单测）───────────────────────────

def count_test_functions(tests_dir: str) -> int:
    """AST 统计 tests/ 下所有 pytest 文件中的 test_ 函数（含类内方法）。"""
    n = 0
    for root, dirs, files in os.walk(tests_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".pytest_cache")]
        for f in files:
            if not f.startswith("test_") or not f.endswith(".py"):
                continue
            n += len(re.findall(r"^def test_|^\s+def test_",
                                open(os.path.join(root, f), encoding="utf-8").read(),
                                re.M))
    return n


def parse_tool_list(readme: str):
    """解析 README 工具清单行（评审 2.6 严格版）。

    规则：
    - 「**9 个白名单工具**：」清单行必须恰好一条；
    - 按 / 分段，每段必须严格匹配 `名称` 或 `名称（说明）`；
    - 名称必须是已知工具且落在 CURRENT_TOOLS 内；
    - 说明内不允许出现 ASCII 标识符（堵住"说明里夹带另一个工具名"的通道）；
    - 段内只允许一个工具名，不允许重复，最终顺序与集合都要与基线相等。
    返回 (names, err)。
    """
    heads = re.findall(r"\*\*9 个白名单工具\*\*：", readme)
    if len(heads) == 0:
        return None, "README 找不到「9 个白名单工具」清单行"
    if len(heads) > 1:
        return None, f"README 出现 {len(heads)} 条「9 个白名单工具」清单行，应恰好 1 条"
    m = re.search(r"\*\*9 个白名单工具\*\*：(.+)", readme)
    seg_re = re.compile(r"^([a-z][a-z0-9_]*)(?:（([^）]*)）)?$")
    names = []
    for seg in m.group(1).split("/"):
        seg = seg.strip()
        if not seg:
            return None, "清单存在空分段（多余 /）"
        mm = seg_re.match(seg)
        if not mm:
            return None, f"清单段「{seg}」格式非法（应为 名称 或 名称（说明））"
        name, note = mm.group(1), mm.group(2) or ""
        if name not in CURRENT_TOOLS:
            return None, f"清单段「{seg}」含未知工具名 {name!r}"
        if re.search(r"[A-Za-z_]{2,}", note):
            return None, f"清单段「{seg}」说明中夹带标识符，疑似塞入工具名"
        names.append(name)
    return names, None


def extract_registered_tools(gateway_path: str):
    """AST 从 gateway/mcp_gateway.py 提取实际注册的工具名：
    TOOLS_SPEC 列表元素 + TOOLS_SPEC.append({...}) 追加元素（"name" 字段），
    以及 _TOOL_IMPL 字典的键。同时支持 Assign 与 AnnAssign。"""
    tree = ast.parse(open(gateway_path, encoding="utf-8").read())
    spec_names, impl_names = [], []

    def _dict_name(node):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "name" \
                        and isinstance(v, ast.Constant):
                    return v.value
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target.id] if isinstance(node.target, ast.Name) else []
            value = node.value
        else:
            targets, value = [], None
        if "TOOLS_SPEC" in targets and isinstance(value, ast.List):
            for elt in value.elts:
                n = _dict_name(elt)
                if n:
                    spec_names.append(n)
        if "_TOOL_IMPL" in targets and isinstance(value, ast.Dict):
            for k in value.keys:
                if isinstance(k, ast.Constant):
                    impl_names.append(k.value)
        # TOOLS_SPEC.append({"name": ...}) 追加形态（v1.4 引入的 get_report/save_report）
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "append" and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "TOOLS_SPEC" and node.args:
            n = _dict_name(node.args[0])
            if n:
                spec_names.append(n)
    return spec_names, impl_names


def reconcile_versions(readme: str, design: str, tests_dir: str,
                       gateway_path: str, error=fail):
    """动态对账：所有基线必须精确相等（六轮评审：不用 substring 蒙混）。"""
    problems = []
    # 1. README 测试徽章：恰好一个，值精确
    badges = re.findall(r"tests-(\d+)%2F(\d+)", readme)
    if len(badges) != 1:
        problems.append(f"README 测试徽章应恰好 1 个，实际 {len(badges)} 个: {badges}")
    else:
        a, b = badges[0]
        if f"{a}/{b}" != f"{CURRENT_TESTS}/{CURRENT_TESTS}":
            problems.append(f"README 测试徽章 ≠ tests-{CURRENT_TESTS}%2F{CURRENT_TESTS}")
    # 2. README 版本徽章：恰好一个，值精确
    vbadges = re.findall(r"version-(\d+(?:\.\d+)?)", readme)
    if len(vbadges) != 1:
        problems.append(f"README 版本徽章应恰好 1 个，实际 {len(vbadges)} 个: {vbadges}")
    elif vbadges[0] != CURRENT_VERSION:
        problems.append(f"README 版本徽章 ≠ version-{CURRENT_VERSION}")
    # 3. DESIGN 头部版本行：恰好一条，精确匹配
    vlines = re.findall(r"^> 版本：v(\S+?)(?: ｜|$)", design, re.M)
    if len(vlines) != 1:
        problems.append(f"DESIGN 版本元信息行应恰好 1 条，实际 {len(vlines)} 条: {vlines}")
    elif vlines[0] != CURRENT_VERSION:
        problems.append(f"DESIGN 版本行 ≠ v{CURRENT_VERSION}")
    # 4. 测试函数数量：AST 统计（tests/ 全量）
    n = count_test_functions(tests_dir)
    if n != CURRENT_TESTS:
        problems.append(f"测试函数 {n} 个 ≠ 基线 {CURRENT_TESTS}")
    # 5. 工具清单：段内单工具名 + 集合与顺序严格相等
    names, err = parse_tool_list(readme)
    if err:
        problems.append(err)
    else:
        if names != CURRENT_TOOLS:
            problems.append(f"工具清单与基线不一致：{names}")
    # 6. Gateway 实际注册工具 vs 基线（防"README 对、代码错"）
    spec_names, impl_names = extract_registered_tools(gateway_path)
    if spec_names != CURRENT_TOOLS:
        problems.append(f"TOOLS_SPEC 注册 {spec_names} ≠ 基线")
    if impl_names != CURRENT_TOOLS:
        problems.append(f"_TOOL_IMPL 注册 {impl_names} ≠ 基线")
    if problems:
        error("动态对账失败：\n     " + "\n     ".join(problems))
    return True


# ─────────────────────────── AST import 扫描（含动态导入）───────────────────────────

_LEGACY_ROOTS = ("app", "prep")


def scan_ast_imports(source: str, filename: str = "<src>"):
    """返回违规列表（空=通过）。覆盖普通/延迟/动态导入：
    import app.x / from app.x import / importlib.import_module("app.x") /
    __import__("app.x") / importlib 别名（il.import_module）/
    from importlib import import_module [as im] 别名调用。
    非静态可定的动态导入参数：保守失败（不假装已检查）。"""
    tree = ast.parse(source, filename=filename)
    problems = []

    def _legacy(mod: str) -> bool:
        parts = mod.split(".")
        return bool(parts) and parts[0] in _LEGACY_ROOTS

    def _dyn_arg(call):
        """取动态导入调用的第一个参数常量字符串；非静态可定返回 None。"""
        if not call.args:
            return None
        a = call.args[0]
        return a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None

    # 收集 importlib / import_module 的别名（含 as 重命名）
    module_aliases = set()          # "importlib" 或别名 → importlib 模块名
    func_aliases = set()            # 直接 import_module 函数名或别名
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "importlib":
                    module_aliases.add(a.asname or "importlib")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == "importlib":
                for a in node.names:
                    if a.name == "import_module":
                        func_aliases.add(a.asname or "import_module")

    # 守卫豁免（v1.8）：try: from gateway... except (ModuleNotFoundError|ImportError):
    # from app... —— 布局兼容回退，不是仓库侧对 chemask 布局的依赖。
    guarded = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches_import = False
        for h in node.handlers:
            names = []
            if isinstance(h.type, ast.Name):
                names = [h.type.id]
            elif isinstance(h.type, ast.Tuple):
                names = [e.id for e in h.type.elts if isinstance(e, ast.Name)]
            if {"ImportError", "ModuleNotFoundError"} & set(names):
                catches_import = True
        if not catches_import:
            continue
        # 回退导入通常写在 except 体内（try: gateway / except ImportError: app）
        stmts = list(node.body)
        for h in node.handlers:
            stmts.extend(h.body)
        for stmt in stmts:
            for sub in ast.walk(stmt):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    guarded.add((sub.lineno, sub.col_offset))

    for node in ast.walk(tree):
        in_guard = (getattr(node, "lineno", -1), getattr(node, "col_offset", -1)) in guarded
        # 普通/延迟 import
        if isinstance(node, ast.Import):
            if in_guard:
                continue
            for a in node.names:
                if _legacy(a.name):
                    problems.append(f"{filename}:{node.lineno}: import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if in_guard:
                continue
            if node.level == 0 and node.module and _legacy(node.module):
                problems.append(f"{filename}:{node.lineno}: from {node.module} import ...")
        # 动态导入：X.import_module("...") / __import__("...") / 别名调用
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "import_module":
                # 属性调用形态：obj.import_module(...)；obj 为 importlib 或其别名时拦截
                obj = node.func.value
                obj_name = obj.id if isinstance(obj, ast.Name) else None
                if obj_name is None or obj_name in module_aliases or obj_name == "importlib":
                    arg = _dyn_arg(node)
                    if arg is None:
                        problems.append(
                            f"{filename}:{node.lineno}: import_module 参数非静态字符串，保守失败")
                    elif _legacy(arg):
                        problems.append(f"{filename}:{node.lineno}: import_module({arg!r})")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "__import__":
                arg = _dyn_arg(node)
                if arg is None:
                    problems.append(
                        f"{filename}:{node.lineno}: __import__ 参数非静态字符串，保守失败")
                elif _legacy(arg):
                    problems.append(f"{filename}:{node.lineno}: __import__({arg!r})")
            elif node.func.id in func_aliases:
                arg = _dyn_arg(node)
                if arg is None:
                    problems.append(
                        f"{filename}:{node.lineno}: import_module 别名调用参数非静态字符串，保守失败")
                elif _legacy(arg):
                    problems.append(f"{filename}:{node.lineno}: import_module({arg!r})（别名）")
    return problems


def step5_ast_import_scan():
    bad = []
    for rel in AST_SCAN_FILES:
        fp = os.path.join(HERE, rel)
        if not os.path.isfile(fp):
            bad.append(f"{rel}: 文件不存在")
            continue
        bad += scan_ast_imports(open(fp, encoding="utf-8").read(), rel)
    if bad:
        print("[FAIL] AST import 残留（发布中止）：")
        for b in bad:
            print("    ", b)
        sys.exit(1)
    print("[ok] AST import 扫描通过（普通/延迟/importlib/__import__/别名）")


# ─────────────────────────── 扫描与同步 ───────────────────────────

def step1_verify_sources():
    missing = [s for s, _ in PAIRS if not os.path.isfile(os.path.join(SRC, s))]
    if missing:
        fail(f"同步源文件缺失，终止发布：{missing}")
    print(f"[ok] {len(PAIRS)} 个同步源文件全部存在")


def step2_copy():
    for src_rel, dst_rel in PAIRS:
        shutil.copy(os.path.join(SRC, src_rel), os.path.join(HERE, dst_rel))
        print(f"[cp] {src_rel} -> {dst_rel}")


def step3_rewrite():
    gp = os.path.join(HERE, "gateway", "mcp_gateway.py")
    t = open(gp, encoding="utf-8").read()
    t = t.replace(
        "运行（chemask 工作区，app 包路径）：\n"
        "    ./.venv/bin/python -m app.mcp_gateway --port 8765\n"
        "运行（chem-mcp 仓库，gateway 包路径，uvicorn）：\n"
        "    uvicorn gateway.mcp_gateway:app --host 127.0.0.1 --port 8721\n",
        "运行（本地，gateway 包路径，uvicorn）：\n"
        "    uvicorn gateway.mcp_gateway:app --host 127.0.0.1 --port 8721\n")
    open(gp, "w", encoding="utf-8").write(t)
    # ops 脚本自 v1.8 起用"双布局导入"（try gateway / except ModuleNotFoundError: app），
    # 仓库侧与服务器侧字节一致，不再做 import 改写（否则会把兜底分支一起替换掉）。
    tp = os.path.join(HERE, "tests", "test_mcp_gateway.py")
    s = open(tp, encoding="utf-8").read()
    s = s.replace("from app import db\nfrom app.mcp_gateway import",
                  "from gateway import db\nfrom gateway.mcp_gateway import")
    s = s.replace("from app.mcp_gateway import", "from gateway.mcp_gateway import")
    s = s.replace("from app import mcp_gateway", "from gateway import mcp_gateway")
    s = s.replace("from prep import mcp_migrate", "from ops import mcp_migrate")
    open(tp, "w", encoding="utf-8").write(s)
    print("[fix] imports/docstring -> 仓库布局")


def step6_dynamic_reconcile():
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    design = open(os.path.join(HERE, "DESIGN.md"), encoding="utf-8").read()
    reconcile_versions(readme, design, os.path.join(HERE, "tests"),
                       os.path.join(HERE, "gateway", "mcp_gateway.py"))
    print(f"[ok] 动态对账通过（v{CURRENT_VERSION} / {CURRENT_TESTS} 测试 / 9 工具）")


# ─────────────────────────── pytest 门禁与 .env ───────────────────────────

def _find_pytest_python():
    venv_py = os.path.join(SRC, ".venv", "bin", "python")
    if os.path.isfile(venv_py):
        if subprocess.run([venv_py, "-m", "pytest", "--version"],
                          capture_output=True).returncode == 0:
            return venv_py
    if subprocess.run([sys.executable, "-m", "pytest", "--version"],
                      capture_output=True).returncode == 0:
        return sys.executable
    return None


def build_test_env(src_dir: str, base_env=None):
    """构造测试子进程环境（纯函数，可单测）。
    优先级（六轮评审）：chemask .env 覆盖一切 MYSQL_*（含 shell 残留）——
    1. 先剥离 base_env 中全部 MYSQL_*；
    2. 从 ../chemask/.env 注入；优先 python-dotenv 解析，缺失时用受限手写解析；
    3. .env 缺失且必要变量不足 → 抛 ValueError（fail-closed，绝不回退 shell 旧配置）；
    4. 非 MYSQL 变量原样保留；不输出密码。"""
    env = dict(base_env if base_env is not None else os.environ)
    env = {k: v for k, v in env.items() if not k.startswith("MYSQL_")}
    src_env = os.path.join(src_dir, ".env")
    dotenv_used = False
    if os.path.isfile(src_env):
        try:
            from dotenv import dotenv_values
            vals = dotenv_values(src_env)
            dotenv_used = True
        except ImportError:
            vals = {}
            for line in open(src_env, encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip()
            # 手写解析器格式范围声明：仅支持 KEY=VALUE 与 # 注释（不支持 export/引号转义）
        env.update({k: v for k, v in vals.items() if k.startswith("MYSQL_")})
    required = ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DB")
    missing = [k for k in required if not env.get(k)]
    if missing:
        raise ValueError(
            f"测试 MySQL 配置缺失（fail-closed，不回退 shell 环境）：{missing}；"
            f".env 解析方式：{'dotenv' if dotenv_used else '手写（KEY=VALUE/#注释）'}")
    return env


def step7_pytest_gate():
    py = _find_pytest_python()
    if py is None:
        fail("找不到可用的 pytest 环境")
    try:
        test_env = build_test_env(SRC)
    except ValueError as e:
        fail(str(e))
    print(f"[run] 仓库布局 pytest（{os.path.relpath(py, HERE)}）· "
          f"测试库目标（脱敏）：{test_env['MYSQL_HOST']}:{test_env['MYSQL_PORT']}/"
          f"{test_env['MYSQL_DB']}")
    # --basetemp：用独立临时目录，避免与上一次运行的 pytest-of-* 目录冲突
    base = tempfile.mkdtemp(prefix="chem_pytest_")
    r = subprocess.run([py, "-m", "pytest", "tests/", "-q", "--no-header", "-x",
                        "--basetemp", base],
                       cwd=HERE, capture_output=True, text=True, env=test_env)
    shutil.rmtree(base, ignore_errors=True)
    tail = "\n".join((r.stdout or "").strip().splitlines()[-5:])
    print(tail)
    if r.returncode != 0:
        fail(f"pytest 未通过（exit={r.returncode}），发布中止")
    m = re.search(r"(\d+) passed", r.stdout or "")
    passed = int(m.group(1)) if m else None
    if passed != CURRENT_TESTS:
        fail(f"pytest 通过 {passed} 例 ≠ 基线 {CURRENT_TESTS}")
    print(f"[ok] 发布门禁：{passed}/{CURRENT_TESTS} 测试通过（真实 MySQL）")


# ─────────────────────────── manifest 与 --verify（可注入 repo 目录以便单测）───────

def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True).stdout.strip()


def _git_ok(repo, *args) -> bool:
    """执行 git 命令并返回退出码是否为 0（用于 --is-ancestor 等无 stdout 的命令）。"""
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True).returncode == 0


def build_manifest(repo: str, version: str, release_commit: str,
                   workspace_clean: bool) -> dict:
    entries = {}
    for rel in MANIFEST_FILES:
        fp = os.path.join(repo, rel)
        if not os.path.isfile(fp):
            raise ValueError(f"manifest 必需文件缺失：{rel}")
        entries[rel] = hashlib.sha256(open(fp, "rb").read()).hexdigest()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": version,
        # release_commit = 发布内容对应的 commit（finalize 时为当前 HEAD）；
        # 不必然等于仓库最新 HEAD（manifest 修正提交后 HEAD 会前进）
        "release_commit": release_commit,
        "workspace_clean": workspace_clean,
        # build_manifest.json 自身不参与哈希清单（自引用问题），设计如此
        "files": entries,
    }


def step8_manifest():
    missing = [f for f in MANIFEST_FILES if not os.path.isfile(os.path.join(HERE, f))]
    if missing:
        fail(f"manifest 必需文件缺失：{missing}")
    commit = _git(HERE, "rev-parse", "HEAD") or "unknown"
    dirty = bool(_git(HERE, "status", "--porcelain"))
    manifest = build_manifest(HERE, CURRENT_VERSION, commit, not dirty)
    open(os.path.join(HERE, "build_manifest.json"), "w", encoding="utf-8").write(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"[ok] build_manifest.json（{len(manifest['files'])} 文件，"
          f"release={commit[:12]}，workspace_clean={not dirty}）")
    if dirty:
        print("[hint] 工作区有未提交修改：先 commit 业务内容，再 --finalize-manifest，"
              "最后 --verify")


def verify_release(repo: str, manifest_rel: str, version: str) -> list:
    """严格发布验证器（纯逻辑，repo/manifest 路径可注入以便临时仓库测试）。
    返回问题列表（空=全部通过）。检查 A–I 见任务 5.4。"""
    problems = []
    mp = os.path.join(repo, manifest_rel)
    # A. manifest 存在且合法
    if not os.path.isfile(mp):
        return [f"manifest 不存在：{manifest_rel}"]
    try:
        m = json.load(open(mp, encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return [f"manifest JSON 解析失败：{e}"]
    for key in ("version", "release_commit", "workspace_clean", "files"):
        if key not in m:
            problems.append(f"manifest 缺少字段 {key}")
    # manifest version 必须与基线一致（任务 5.4 检查 A 扩展）
    if m.get("version") != version:
        problems.append(f"manifest version={m.get('version')!r} ≠ 基线 v{version}")
    if problems:
        return problems
    # B. 文件集合与固定清单严格相等
    if set(m["files"].keys()) != set(MANIFEST_FILES):
        extra = sorted(set(m["files"].keys()) - set(MANIFEST_FILES))
        missing = sorted(set(MANIFEST_FILES) - set(m["files"].keys()))
        problems.append(f"manifest 文件集合 ≠ 固定清单（多 {extra}，少 {missing}）")
    # C. workspace_clean 必须为 true
    if m.get("workspace_clean") is not True:
        problems.append("workspace_clean 必须为 true")
    # D. release_commit 合法且存在
    release = m.get("release_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", release):
        problems.append(f"release_commit 非完整 commit SHA：{release!r}")
    elif _git(repo, "cat-file", "-t", release) != "commit":
        problems.append(f"release_commit 不存在于仓库：{release[:12]}")
    # E. release_commit 必须是 HEAD 祖先
    head = _git(repo, "rev-parse", "HEAD")
    if release and _git(repo, "cat-file", "-t", release) == "commit":
        if release != head:
            if not _git_ok(repo, "merge-base", "--is-ancestor", release, head):
                problems.append(f"release_commit {release[:12]} 不是 HEAD {head[:12]} 的祖先")
    # F. 两段式发布：release..HEAD 只允许 build_manifest.json
    if release and _git(repo, "cat-file", "-t", release) == "commit" and release != head:
        changed = set(_git(repo, "diff", "--name-only", release, head).splitlines())
        if changed != {manifest_rel}:
            problems.append(f"release_commit 后修改了非 manifest 文件：{sorted(changed)}")
    # G. release_commit 的 git blob 哈希逐文件比对
    for rel, expected in m["files"].items():
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            problems.append(f"{rel}: 哈希格式非法（非 64 位 hex）")
            continue
        if release and _git(repo, "cat-file", "-t", release) == "commit":
            blob = subprocess.run(["git", "show", f"{release}:{rel}"],
                                  cwd=repo, capture_output=True)
            if blob.returncode != 0:
                problems.append(f"{rel}: commit {release[:12]} 中不存在")
            elif hashlib.sha256(blob.stdout).hexdigest() != expected:
                problems.append(f"{rel}: manifest={expected[:12]} blob 不符")
    # H. 当前工作区必须干净（无未提交修改/未跟踪发布文件）
    status = _git(repo, "status", "--porcelain")
    if status:
        problems.append(f"工作区未干净（--verify 要求 clean）：{status.splitlines()[:3]}")
    # I. 当前 HEAD 的发布文件内容也必须与 manifest 一致
    for rel, expected in m["files"].items():
        fp = os.path.join(repo, rel)
        if not os.path.isfile(fp):
            problems.append(f"{rel}: 当前工作区缺失")
        elif hashlib.sha256(open(fp, "rb").read()).hexdigest() != expected:
            problems.append(f"{rel}: HEAD 工作区内容与 manifest 不符")
    return problems


def do_verify():
    mp = os.path.join(HERE, "build_manifest.json")
    if not os.path.isfile(mp):
        fail("build_manifest.json 不存在")
    problems = verify_release(HERE, "build_manifest.json", CURRENT_VERSION)
    if problems:
        print("[FAIL] 发布验证未通过：")
        for b in problems:
            print("    ", b)
        sys.exit(1)
    rel = json.load(open(mp, encoding="utf-8"))
    head = _git(HERE, "rev-parse", "HEAD")
    if rel["release_commit"] == head:
        print(f"[ok] --verify 通过：{len(rel['files'])} 个文件与 HEAD ({head[:12]}) 一致，"
              f"工作区干净，release=HEAD")
    else:
        print(f"[ok] --verify 通过（两段式）：manifest 指向 release "
              f"{rel['release_commit'][:12]}，HEAD={head[:12]}，差异仅 "
              f"build_manifest.json")


def do_finalize_manifest():
    """阶段三：业务内容已 commit 后，固化 manifest 指向当前 HEAD。"""
    if _git(HERE, "status", "--porcelain"):
        fail("工作区不干净：先提交业务发布内容，再 --finalize-manifest")
    commit = _git(HERE, "rev-parse", "HEAD")
    manifest = build_manifest(HERE, CURRENT_VERSION, commit, True)
    open(os.path.join(HERE, "build_manifest.json"), "w", encoding="utf-8").write(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"[ok] manifest 已固化：release={commit[:12]}，{len(manifest['files'])} 文件，"
          f"workspace_clean=true")
    print("[hint] 下一步：git add build_manifest.json && git commit -m "
          "\"chore: finalize release manifest\"，然后 --verify")


def do_check_only():
    """CI 模式：不依赖 ../chemask，只对仓库现状跑静态门禁。

    用途：GitHub Actions（无 chemask 源工作区）执行 扫描/动态对账/AST/清单存在性，
    保证"绕过本地同步脚本直接 commit"的提交也过不了这些检查。
    """
    print(f"=== chem-mcp 静态门禁（CI 模式，v{CURRENT_VERSION} 基线）===")
    missing = [f for f in MANIFEST_FILES if not os.path.isfile(os.path.join(HERE, f))]
    if missing:
        fail(f"发布必需文件缺失：{missing}")
    print(f"[ok] {len(MANIFEST_FILES)} 个发布必需文件全部存在")
    step4_scan()
    step5_ast_import_scan()
    step6_dynamic_reconcile()
    print("[ok] 静态门禁全部通过")


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--verify" in args:
        do_verify()
    elif "--finalize-manifest" in args:
        do_finalize_manifest()
    elif "--check-only" in args:
        do_check_only()
    else:
        print(f"=== chem-mcp 发布同步（v{CURRENT_VERSION} 基线，{CURRENT_TESTS} 测试）===")
        step1_verify_sources()
        step2_copy()
        step3_rewrite()
        step4_scan()
        step5_ast_import_scan()
        step6_dynamic_reconcile()
        step7_pytest_gate()
        step8_manifest()
        print("[ok] 阶段一完成；下一步：git add -A && git commit，然后 "
              "--finalize-manifest → commit manifest → --verify")
