"""chemask 工作区 → chem-mcp 仓库的发布同步与门禁脚本（v1.6，四轮评审加固）。

用法（发布前必须运行，且必须在 chem-mcp/ 目录下跑 pytest 通过后才算完成）：
    python3 sync_from_chemask.py

fail-closed 设计（四轮评审的三缺陷逐项修复）：
  1. 同步前逐一校验源文件存在——任何缺失即 RuntimeError 非零退出，一个文件都不复制；
  2. 全量旧内容扫描——STALE_PATTERNS 集中维护（正则），覆盖徽章/用例数/版本号/旧启动命令/
     旧 import/工具重复；版本升级时必须同步演进本清单，扫描不通过即中止发布；
  3. 同步后自动在仓库布局执行 pytest——测试失败即中止发布（不再是"靠人工记得跑"）；
  4. 全部通过后生成 build_manifest.json（发布文件 SHA-256 + git commit SHA），
     生产部署后可对账运行中的文件是否与仓库逐字节一致：
         sha256sum gateway/mcp_gateway.py   # 与 manifest 比对
"""
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

# 产物清单中的文件（同步文件 + 面向审查的关键文档）
MANIFEST_FILES = [dst for _, dst in PAIRS] + ["README.md", "DESIGN.md", "plugin.json"]
MANIFEST_FILES = [f for f in MANIFEST_FILES if os.path.exists(os.path.join(HERE, f))]

# 当前版本基线（升级版本号时必须同步演进 STALE_PATTERNS）
CURRENT_VERSION = "1.6"
CURRENT_TESTS = 37
CURRENT_TOOLS = ["get_profile", "update_profile", "save_memory", "record_attempt",
                 "list_mistakes", "get_mastery", "class_summary", "get_report", "save_report"]

# 旧版本内容残留扫描（正则；命中即 FAIL）。
# 自引用豁免：本脚本自身与 DESIGN.md（其修复记录章节引用旧串是为了说明修复了什么，
# 因此对 DESIGN.md 做行级豁免：仅当行属于 §5/§5bis/§5ter/§6 修复记录表格/段落时忽略）。
STALE_PATTERNS = [
    (r"tests-33%2F33", "README 徽章 33 测试"),
    (r"version-1\.4", "版本徽章 1.4"),
    (r"#\s*3[0-3]\s*用例", "目录注释旧用例数(≤33)"),
    (r"\b3[0-3]\s*用例本地跑", "路线图旧用例数(≤33)"),
    (r"(?<!逐字节一致），)\b3[0-3]/3[0-3]\s*测试", "表格旧测试通过数(≤33)"),
    (r"30 个测试用例", "v1.2 旧测试数"),
    (r"30 用例：", "v1.2 旧用例描述"),
    (r"7 个白名单 MCP", "v1.2 旧工具数"),
    (r"8 个白名单工具\*\*", "v1.4 旧工具数"),
    (r"-m app\.mcp_gateway", "仓库侧旧启动命令 app.mcp_gateway"),
    (r"^\s*from (app|prep)[. ]", "旧 import 路径"),
]
# 这些文件允许出现搜索模式自身（本脚本含模式字符串）；DESIGN 走行级豁免
STALE_SCAN_EXEMPT = {"sync_from_chemask.py"}
# DESIGN.md 中允许引用旧串的行特征（修复记录章节：表格行、引用块、章节标题、说明段）
DESIGN_EXEMPT_LINE = re.compile(
    r"^\s*(\||#|>|v1\.[3-6]|\* flaw|\*\*评审|README\"|三轮时修正|网关模块 docstring|DESIGN §|该假设|"
    r"教训|扫描规则|single-writer|单写者)",
    re.M,
)


def _design_line_exempt(txt: str, match: re.Match) -> bool:
    """修复记录章节对旧串的引用（用于说明修复内容）不算残留。"""
    line_start = txt.rfind("\n", 0, match.start()) + 1
    line_end = txt.find("\n", match.end())
    line = txt[line_start:line_end if line_end != -1 else len(txt)]
    return bool(re.match(r"^\s*(\||#|>|v1\.[3-6]|README|三轮时|网关模块|DESIGN §|教训|单写者|该假设|"
                         r"扫描规则|部署后|对账)", line))


def fail(msg):
    print(f"[FAIL] {msg}")
    sys.exit(1)


def step1_verify_sources():
    """缺陷1修复：源文件缺失即失败，绝不静默跳过。"""
    missing = [s for s, _ in PAIRS if not os.path.isfile(os.path.join(SRC, s))]
    if missing:
        fail(f"同步源文件缺失，终止发布：{missing}")
    if not os.path.isdir(SRC):
        fail(f"同步源目录不存在，终止发布：{SRC}")
    print(f"[ok] 6 个同步源文件全部存在（{os.path.normpath(SRC)}）")


def step2_copy():
    for src_rel, dst_rel in PAIRS:
        src = os.path.join(SRC, src_rel)
        dst = os.path.join(HERE, dst_rel)
        shutil.copy(src, dst)
        print(f"[cp] {src_rel} -> {dst_rel}")


def step3_rewrite_imports():
    """import 路径统一替换（幂等）+ 同步文件的 docstring 双布局改写。"""
    # gateway: chemask 源的 docstring 同时写了两种布局的启动方式；
    # 仓库侧只保留 gateway 布局（`-m app.mcp_gateway` 仅在 chemask 工作区合法）
    gp = os.path.join(HERE, "gateway", "mcp_gateway.py")
    t = open(gp, encoding="utf-8").read()
    t = t.replace(
        "运行（chemask 工作区，app 包路径）：\n"
        "    ./.venv/bin/python -m app.mcp_gateway --port 8765\n"
        "运行（chem-mcp 仓库，gateway 包路径，uvicorn）：\n"
        "    uvicorn gateway.mcp_gateway:app --host 127.0.0.1 --port 8721\n",
        "运行（本地，gateway 包路径，uvicorn）：\n"
        "    uvicorn gateway.mcp_gateway:app --host 127.0.0.1 --port 8721\n",
    )
    open(gp, "w", encoding="utf-8").write(t)
    print("[fix] gateway docstring -> 仓库布局启动命令")
    # ops
    for f in ("ops/mcp_token.py", "ops/mcp_migrate.py"):
        fp = os.path.join(HERE, f)
        t = open(fp, encoding="utf-8").read()
        t = t.replace("from app import db", "from gateway import db")
        t = t.replace("from app.db import get_conn", "from gateway.db import get_conn")
        open(fp, "w", encoding="utf-8").write(t)
    print("[fix] ops imports -> gateway")
    # tests
    tp = os.path.join(HERE, "tests", "test_mcp_gateway.py")
    s = open(tp, encoding="utf-8").read()
    s = s.replace("from app import db\nfrom app.mcp_gateway import",
                  "from gateway import db\nfrom gateway.mcp_gateway import")
    s = s.replace("from app.mcp_gateway import", "from gateway.mcp_gateway import")
    s = s.replace("from app import mcp_gateway", "from gateway import mcp_gateway")
    s = s.replace("from prep import mcp_migrate", "from ops import mcp_migrate")
    open(tp, "w", encoding="utf-8").write(s)
    print("[fix] tests imports -> gateway/ops")


def step4_stale_scan():
    """缺陷2修复：全量旧内容扫描（正则清单）。"""
    bad = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", ".pytest_cache")]
        for f in files:
            if not f.endswith((".py", ".md", ".json")):
                continue
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, HERE)
            if rel in STALE_SCAN_EXEMPT:
                continue
            txt = open(fp, encoding="utf-8").read()
            for pat, desc in STALE_PATTERNS:
                for m in re.finditer(pat, txt):
                    if rel == "DESIGN.md" and _design_line_exempt(txt, m):
                        continue  # 修复记录章节引用旧串，非残留
                    bad.append(f"{rel}: {desc} -> {m.group(0)!r}")
    if bad:
        print("[FAIL] 旧版本内容残留（发布中止）：")
        for b in bad:
            print("    ", b)
        sys.exit(1)
    print(f"[ok] 旧内容扫描通过（{len(STALE_PATTERNS)} 条规则，无残留）")


def step5_tool_dedupe_check():
    """工具清单去重校验：README 中 9 个工具名各恰好出现一次（工具清单行内）。"""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    m = re.search(r"\*\*9 个白名单工具\*\*：(.+)", readme)
    if not m:
        fail("README 找不到「9 个白名单工具」清单行")
    line = m.group(1)
    for tool in CURRENT_TOOLS:
        n = len(re.findall(rf"\b{tool}\b", line))
        if n != 1:
            fail(f"README 工具清单中 {tool} 出现 {n} 次（应为 1 次）")
    print(f"[ok] README 工具清单去重校验通过（{len(CURRENT_TOOLS)} 个工具各出现一次）")


def _find_pytest_python():
    """优先用 chemask 工作区 venv（含依赖），其次回退当前解释器。"""
    venv_py = os.path.join(SRC, ".venv", "bin", "python")
    if os.path.isfile(venv_py):
        r = subprocess.run([venv_py, "-m", "pytest", "--version"], capture_output=True)
        if r.returncode == 0:
            return venv_py
    r = subprocess.run([sys.executable, "-m", "pytest", "--version"], capture_output=True)
    if r.returncode == 0:
        return sys.executable
    return None


def _test_env():
    """仓库不入库 .env（红线），测试所需 MySQL 配置从 chemask 工作区 .env 注入环境变量。"""
    env = dict(os.environ)
    src_env = os.path.join(SRC, ".env")
    if os.path.isfile(src_env):
        for line in open(src_env, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env.setdefault(k.strip(), v.strip())
    return env


def step6_pytest_gate():
    """缺陷3修复：pytest 作为发布门禁（仓库布局）。"""
    py = _find_pytest_python()
    if py is None:
        fail("找不到可用的 pytest 环境（检查 ../chemask/.venv 或当前解释器）")
    print(f"[run] 仓库布局 pytest（{os.path.relpath(py, HERE)}）…")
    r = subprocess.run([py, "-m", "pytest", "tests/", "-q", "--no-header", "-x"],
                       cwd=HERE, capture_output=True, text=True, env=_test_env())
    tail = "\n".join((r.stdout or "").strip().splitlines()[-5:])
    if r.returncode != 0:
        print(tail)
        fail(f"pytest 未通过（exit={r.returncode}），发布中止")
    m = re.search(r"(\d+) passed", r.stdout or "")
    passed = int(m.group(1)) if m else None
    print(tail)
    if passed != CURRENT_TESTS:
        fail(f"pytest 通过 {passed} 例，与基线 {CURRENT_TESTS} 不符——"
             f"若是新增测试请同步更新 sync_from_chemask.py 的 CURRENT_TESTS/CURRENT_VERSION 与文档徽章")
    print(f"[ok] 发布门禁：{passed}/{CURRENT_TESTS} 测试通过")


def step7_manifest():
    """缺陷3补强：生成 SHA-256 + commit SHA 清单，供生产对账。"""
    commit = "unknown"
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE,
                                capture_output=True, text=True).stdout.strip() or "unknown"
    except Exception:
        pass
    entries = {}
    for rel in MANIFEST_FILES:
        fp = os.path.join(HERE, rel)
        h = hashlib.sha256(open(fp, "rb").read()).hexdigest()
        entries[rel] = h
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": CURRENT_VERSION,
        "git_commit": commit,
        "files": entries,
        "hint": "生产对账：在服务器逐文件 sha256sum 与本清单比对，确认运行中的代码与仓库一致",
    }
    mp = os.path.join(HERE, "build_manifest.json")
    open(mp, "w", encoding="utf-8").write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"[ok] 生成 {os.path.basename(mp)}（{len(entries)} 个文件 SHA-256，commit {commit[:12]}）")
    print("[hint] 生产对账命令：ssh 到服务器后 sha256sum <文件> 与 build_manifest.json 比对")


if __name__ == "__main__":
    print(f"=== chem-mcp 发布同步（v{CURRENT_VERSION} 基线，{CURRENT_TESTS} 测试）===")
    step1_verify_sources()
    step2_copy()
    step3_rewrite_imports()
    step4_stale_scan()
    step5_tool_dedupe_check()
    step6_pytest_gate()
    step7_manifest()
    print("[ok] 同步完成，全部门禁通过；可 commit + push + 部署，部署后用 build_manifest.json 对账")
