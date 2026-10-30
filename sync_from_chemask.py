"""chemask 工作区 → chem-mcp 仓库的发布同步与门禁脚本（v1.7，五轮评审加固）。

用法：
    python3 sync_from_chemask.py             # 完整发布门禁：同步→扫描→测试→manifest
    python3 sync_from_chemask.py --verify    # 校验 manifest 哈希与 release commit git blob 是否一致

fail-closed 设计（四/五轮评审逐项落实）：
  1. 同步前逐一校验源文件存在——任何缺失即非零退出，一个文件都不复制；
  2. 全量旧内容扫描——STALE_PATTERNS 兜底正则 + 动态对账（README 徽章/DESIGN 头部
     版本与基线精确相等、工具清单集合相等）+ 同步产物 AST import 扫描；
     DESIGN 豁免按【章节】判定（仅 §5/§5bis/§5ter/§5quater/§6 修复记录标题之后、
     下一个 H2 之前的行），不是按行首字符——blockquote/表格里的活旧命令无处遁形；
  3. 同步后自动在仓库布局执行 pytest——数量必须等于 CURRENT_TESTS，失败即中止；
  4. 生成 build_manifest.json：固定必需清单（不静默过滤，缺失即失败），
     含专家包文件；记录 release_commit + 工作区 dirty 标志；
  5. --verify：从 release commit 的 git blob 重新计算哈希与 manifest 比对——
     不一致 = 发布三步未完成。

发布三步（缺一不可，DESIGN §5quater 有说明）：
    python3 sync_from_chemask.py && git add -A && git commit -m "release vX.Y"
    python3 sync_from_chemask.py --verify          # blob 对账
    # 若 manifest 记录的 commit 落后（dirty 时生成），追加提交修正 manifest
"""
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
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

# 发布清单：固定必需文件（五轮评审：不做 exists() 静默过滤，缺失即失败）
MANIFEST_FILES = [
    "gateway/mcp_gateway.py", "gateway/db.py", "tests/test_mcp_gateway.py",
    "ops/mcp_migrate.py", "ops/mcp_token.py", "ops/mcp_echo_server.py",
    "README.md", "DESIGN.md",
    "expert-package/.codebuddy-plugin/plugin.json",   # 专家包纳入对账（五轮评审）
    "expert-package/agents/cie-chem-coach.md",
]

# ── 版本基线（升级版本时必须同步更新这里 + README 徽章 + DESIGN 头部）────────
CURRENT_VERSION = "1.7"
CURRENT_TESTS = 38
CURRENT_TOOLS = ["get_profile", "update_profile", "save_memory", "record_attempt",
                 "list_mistakes", "get_mastery", "class_summary", "get_report",
                 "save_report"]

# ── 兜底正则（非徽章位置的历史串；徽章/版本/测试数由 step6 动态对账精确校验）──────
STALE_PATTERNS = [
    (r"-m app\.mcp_gateway", "仓库侧旧启动命令 app.mcp_gateway"),
    (r"^\s*(from|import) (app|prep)\b", "旧 import 路径"),
    (r"30 个测试用例|30 用例：|7 个白名单 MCP|8 个白名单工具\*\*", "v1.2/v1.4 历史旧串"),
]

# DESIGN 修复记录章节标题（白名单前缀）：章节内的行允许引用旧串说明历史
DESIGN_HISTORY_SECTIONS = ("## 5.", "## 5bis.", "## 5ter.", "## 5quater.", "## 6.")

# 同步产物文件的 AST import 扫描（五轮评审：import app.mcp_gateway 也能被查）
AST_SCAN_FILES = ["gateway/mcp_gateway.py", "gateway/db.py",
                  "ops/mcp_migrate.py", "ops/mcp_token.py",
                  "ops/mcp_echo_server.py", "tests/test_mcp_gateway.py"]


def fail(msg):
    print(f"[FAIL] {msg}")
    sys.exit(1)


def step1_verify_sources():
    """源文件缺失即失败，绝不静默跳过。"""
    missing = [s for s, _ in PAIRS if not os.path.isfile(os.path.join(SRC, s))]
    if missing:
        fail(f"同步源文件缺失，终止发布：{missing}")
    print(f"[ok] {len(PAIRS)} 个同步源文件全部存在")


def step2_copy():
    for src_rel, dst_rel in PAIRS:
        shutil.copy(os.path.join(SRC, src_rel), os.path.join(HERE, dst_rel))
        print(f"[cp] {src_rel} -> {dst_rel}")


def step3_rewrite():
    """import 路径统一替换（幂等）+ gateway docstring 双布局改写。"""
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
    for f in ("ops/mcp_token.py", "ops/mcp_migrate.py"):
        fp = os.path.join(HERE, f)
        t = open(fp, encoding="utf-8").read()
        t = t.replace("from app import db", "from gateway import db")
        t = t.replace("from app.db import get_conn", "from gateway.db import get_conn")
        open(fp, "w", encoding="utf-8").write(t)
    tp = os.path.join(HERE, "tests", "test_mcp_gateway.py")
    s = open(tp, encoding="utf-8").read()
    s = s.replace("from app import db\nfrom app.mcp_gateway import",
                  "from gateway import db\nfrom gateway.mcp_gateway import")
    s = s.replace("from app.mcp_gateway import", "from gateway.mcp_gateway import")
    s = s.replace("from app import mcp_gateway", "from gateway import mcp_gateway")
    s = s.replace("from prep import mcp_migrate", "from ops import mcp_migrate")
    open(tp, "w", encoding="utf-8").write(s)
    print("[fix] imports/docstring -> 仓库布局")


def _design_exempt_lines(txt: str) -> set:
    """返回 DESIGN.md 中允许豁免的行号集合（0-based）。
    章节级判定（五轮评审修复）：只有处于修复记录章节标题（DESIGN_HISTORY_SECTIONS）
    之后、下一个 H2（## ）之前的行才豁免——blockquote/表格/任何行首字符不再构成豁免，
    「> 生产启动命令：python -m app.mcp_gateway」这类活指令在快速开始等章节必被拦截。
    """
    lines = txt.splitlines()
    exempt, in_history = set(), False
    for i, line in enumerate(lines):
        if re.match(r"^## ", line):
            in_history = line.startswith(DESIGN_HISTORY_SECTIONS)
            continue
        if in_history:
            exempt.add(i)
    return exempt


def step4_scan():
    """全量扫描：兜底正则 + DESIGN 章节级豁免。"""
    bad = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", ".venv", ".pytest_cache")]
        for f in files:
            if not f.endswith((".py", ".md", ".json")):
                continue
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, HERE)
            if rel == "sync_from_chemask.py":
                continue  # 本脚本含模式字符串
            txt = open(fp, encoding="utf-8").read()
            design_exempt = _design_exempt_lines(txt) if rel == "DESIGN.md" else set()
            lines = txt.splitlines()
            for pat, desc in STALE_PATTERNS:
                for m in re.finditer(pat, txt, re.M):
                    ln = txt.count("\n", 0, m.start())
                    if rel == "DESIGN.md" and ln in design_exempt:
                        continue  # 修复记录章节内引用旧串，非残留
                    ctx = lines[ln][:80] if ln < len(lines) else "?"
                    bad.append(f"{rel}:{ln + 1}: {desc} -> {ctx!r}")
    if bad:
        print("[FAIL] 旧版本内容残留（发布中止）：")
        for b in bad:
            print("    ", b)
        sys.exit(1)
    print(f"[ok] 兜底正则扫描通过（{len(STALE_PATTERNS)} 条规则，DESIGN 章节级豁免）")


def step5_ast_import_scan():
    """同步产物 AST 级 import 检查：import app / import prep 任意形式（含函数内延迟导入）。"""
    bad = []
    for rel in AST_SCAN_FILES:
        fp = os.path.join(HERE, rel)
        if not os.path.isfile(fp):
            bad.append(f"{rel}: 文件不存在")
            continue
        tree = ast.parse(open(fp, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "app" or a.name.startswith("app.") \
                            or a.name == "prep" or a.name.startswith("prep."):
                        bad.append(f"{rel}:{node.lineno}: AST import {a.name}")
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == "app" or node.module.startswith("app.") \
                        or node.module == "prep" or node.module.startswith("prep."):
                    bad.append(f"{rel}:{node.lineno}: AST from {node.module} import ...")
    if bad:
        print("[FAIL] AST import 残留（发布中止）：")
        for b in bad:
            print("    ", b)
        sys.exit(1)
    print("[ok] AST import 扫描通过（含函数内延迟导入）")


def step6_dynamic_reconcile():
    """动态对账（五轮评审：比枚举旧串更可靠）：当前基线精确匹配。"""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    design = open(os.path.join(HERE, "DESIGN.md"), encoding="utf-8").read()
    # 1. README 徽章必须精确等于基线（version-1.5 / tests-36/36 回退都能拦住）
    if f"tests-{CURRENT_TESTS}%2F{CURRENT_TESTS}" not in readme:
        fail(f"README 测试徽章 ≠ tests-{CURRENT_TESTS}%2F{CURRENT_TESTS}")
    if f"version-{CURRENT_VERSION}" not in readme:
        fail(f"README 版本徽章 ≠ version-{CURRENT_VERSION}")
    # 2. DESIGN 头部版本行必须精确等于基线
    if not re.search(rf"^> 版本：v{re.escape(CURRENT_VERSION)} ", design, re.M):
        fail(f"DESIGN 头部版本行 ≠ 「> 版本：v{CURRENT_VERSION} …」")
    # 3. 测试函数数量必须等于基线
    tp = os.path.join(HERE, "tests", "test_mcp_gateway.py")
    n_tests = len(re.findall(r"^def test_", open(tp, encoding="utf-8").read(), re.M))
    if n_tests != CURRENT_TESTS:
        fail(f"测试函数 {n_tests} 个 ≠ 基线 {CURRENT_TESTS}（新增测试请同步更新基线与文档）")
    # 4. README 工具清单：按 / 分段解析每段首个标识符，集合精确相等
    #    （防第 10 个未知工具混入；段内中文注释如"（仅教师 role)"不参与解析）
    m = re.search(r"\*\*9 个白名单工具\*\*：(.+)", readme)
    if not m:
        fail("README 找不到「9 个白名单工具」清单行")
    segments = [seg for seg in m.group(1).split("/") if seg.strip()]
    parsed = []
    for seg in segments:
        w = re.findall(r"[a-z_]+", seg)
        if w:
            parsed.append(w[0])   # 每段第一个标识符 = 工具名；后续词为中文注释混排
    if len(parsed) != len(CURRENT_TOOLS) or set(parsed) != set(CURRENT_TOOLS):
        fail(f"README 工具清单与基线不一致：解析出 {parsed}")
    print(f"[ok] 动态对账通过（v{CURRENT_VERSION} / {CURRENT_TESTS} 测试 / 工具集合相等）")


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


def _test_env():
    """MySQL 配置注入。优先级（五轮评审）：chemask .env 覆盖 shell 残留变量——
    测试必须连预期库，而不是开发者 shell 里碰巧存在的另一套配置。"""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("MYSQL_")}          # 先剥离 shell 残留
    src_env = os.path.join(SRC, ".env")
    if os.path.isfile(src_env):
        for line in open(src_env, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    print(f"[run] 测试库目标（脱敏）：{env.get('MYSQL_HOST')}:"
          f"{env.get('MYSQL_PORT')}/{env.get('MYSQL_DB')}")
    return env


def step7_pytest_gate():
    py = _find_pytest_python()
    if py is None:
        fail("找不到可用的 pytest 环境（检查 ../chemask/.venv 或当前解释器）")
    print(f"[run] 仓库布局 pytest（{os.path.relpath(py, HERE)}）…")
    r = subprocess.run([py, "-m", "pytest", "tests/", "-q", "--no-header", "-x"],
                       cwd=HERE, capture_output=True, text=True, env=_test_env())
    tail = "\n".join((r.stdout or "").strip().splitlines()[-5:])
    print(tail)
    if r.returncode != 0:
        fail(f"pytest 未通过（exit={r.returncode}），发布中止")
    m = re.search(r"(\d+) passed", r.stdout or "")
    passed = int(m.group(1)) if m else None
    if passed != CURRENT_TESTS:
        fail(f"pytest 通过 {passed} 例 ≠ 基线 {CURRENT_TESTS}")
    print(f"[ok] 发布门禁：{passed}/{CURRENT_TESTS} 测试通过（真实 MySQL）")


def _git(*args):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True,
                          text=True).stdout.strip()


def step8_manifest():
    """生成 manifest：固定必需清单（缺失即失败）+ release commit + dirty 标志。"""
    missing = [f for f in MANIFEST_FILES if not os.path.isfile(os.path.join(HERE, f))]
    if missing:
        fail(f"manifest 必需文件缺失：{missing}")
    commit = _git("rev-parse", "HEAD") or "unknown"
    dirty = bool(_git("status", "--porcelain"))
    entries = {rel: hashlib.sha256(open(os.path.join(HERE, rel), "rb").read()).hexdigest()
               for rel in MANIFEST_FILES}
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": CURRENT_VERSION,
        "release_commit": commit,   # 生成时 HEAD；工作区 dirty 时内容领先该 commit，
        "workspace_clean": not dirty,  # 此时必须 commit 后跑 --verify 完成 blob 对账
        "files": entries,
        "hint": ("release_commit 是生成 manifest 时的 HEAD，未必等于仓库最新 HEAD"
                 "（两段式发布）；生产对账：逐文件 sha256sum 比对本清单，"
                 "仓库侧以 --verify 做 git blob 对账"),
    }
    open(os.path.join(HERE, "build_manifest.json"), "w", encoding="utf-8").write(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"[ok] build_manifest.json（{len(entries)} 文件，release={commit[:12]}，"
          f"workspace_clean={not dirty}）")
    if dirty:
        print("[hint] 工作区有未提交修改：commit 后必须运行 --verify 校验 blob 一致性")


def verify():
    """--verify：release commit 的 git blob 哈希 vs manifest。不一致 = 发布未完成。"""
    mp = os.path.join(HERE, "build_manifest.json")
    if not os.path.isfile(mp):
        fail("build_manifest.json 不存在")
    m = json.load(open(mp, encoding="utf-8"))
    release = m.get("release_commit", "")
    head = _git("rev-parse", "HEAD")
    bad = []
    for rel, expected in m["files"].items():
        blob = subprocess.run(["git", "show", f"{release}:{rel}"],
                              cwd=HERE, capture_output=True)
        if blob.returncode != 0:
            bad.append(f"{rel}: commit {release[:12]} 中不存在")
            continue
        actual = hashlib.sha256(blob.stdout).hexdigest()
        if actual != expected:
            bad.append(f"{rel}: manifest={expected[:12]} blob={actual[:12]}")
    if bad:
        print("[FAIL] manifest 与 git blob 不一致（发布三步未完成）：")
        for b in bad:
            print("    ", b)
        print("[hint] 若工作区文件已是最终内容：git add -A && git commit -m "
              "\"chore: point manifest at release commit\" 后重新生成核对")
        sys.exit(1)
    if release == head:
        print(f"[ok] --verify 通过：manifest {len(m['files'])} 个文件与 HEAD "
              f"({head[:12]}) git blob 逐字节一致")
    else:
        print(f"[ok] --verify 通过（两段式发布）：manifest 指向 release {release[:12]}，"
              f"当前 HEAD={head[:12]}（manifest 修正提交）")


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify()
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
        print("[ok] 全部门禁通过；下一步：git add -A && git commit，然后 "
              "python3 sync_from_chemask.py --verify 做 blob 对账")
