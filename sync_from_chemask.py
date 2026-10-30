"""从 chemask 工作区同步文件到本仓库（评审事故的流程性修复）。
用法：python3 sync_from_chemask.py
同步后自动：替换 import 路径（app→gateway / prep→ops）、校验旧数字残留。
发布前必须运行本脚本 + 在仓库布局下跑 pytest 全绿。"""
import os, re, sys, shutil

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
for src_rel, dst_rel in PAIRS:
    src = os.path.join(SRC, src_rel)
    dst = os.path.join(HERE, dst_rel)
    if os.path.exists(src):
        shutil.copy(src, dst)
        print(f"[cp] {src_rel} -> {dst_rel}")

# ops: import 路径统一替换（幂等）
for f in ("ops/mcp_token.py", "ops/mcp_migrate.py"):
    fp = os.path.join(HERE, f)
    t = open(fp, encoding="utf-8").read()
    t = t.replace("from app import db", "from gateway import db")
    t = t.replace("from app.db import get_conn", "from gateway.db import get_conn")
    open(fp, "w", encoding="utf-8").write(t)
print("[fix] ops imports -> gateway")

# ops: import 路径统一替换（幂等）
for f in ("ops/mcp_token.py", "ops/mcp_migrate.py"):
    fp = os.path.join(HERE, f)
    t = open(fp, encoding="utf-8").read()
    t = t.replace("from app import db", "from gateway import db")
    t = t.replace("from app.db import get_conn", "from gateway.db import get_conn")
    open(fp, "w", encoding="utf-8").write(t)
print("[fix] ops imports -> gateway")

# tests: import 路径统一替换（幂等）
tp = os.path.join(HERE, "tests", "test_mcp_gateway.py")
s = open(tp, encoding="utf-8").read()
s = s.replace("from app import db\nfrom app.mcp_gateway import", "from gateway import db\nfrom gateway.mcp_gateway import")
s = s.replace("from app.mcp_gateway import", "from gateway.mcp_gateway import")
s = s.replace("from app import mcp_gateway", "from gateway import mcp_gateway")
s = s.replace("from prep import mcp_migrate", "from ops import mcp_migrate")
open(tp, "w", encoding="utf-8").write(s)
print("[fix] tests imports -> gateway/ops")

# 校验：不得残留 app/prep 引用 + 旧数字（跳过本脚本自身与其对账说明引用）
bad = []
for root, dirs, files in os.walk(HERE):
    dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
    for f in files:
        if not f.endswith((".py", ".md", ".json")):
            continue
        fp = os.path.join(root, f)
        if os.path.basename(fp) == "sync_from_chemask.py":
            continue  # 本脚本含搜索模式字符串，自我引用非残留
        txt = open(fp, encoding="utf-8").read()
        if re.search(r"^\s*from (app|prep)[. ]", txt, re.M):
            bad.append(f"{fp}: stale import")
        for pat in ("8 个白名单工具**", "30 个测试用例", "30 用例：", "7 个白名单 MCP"):
            if pat in txt:
                bad.append(f"{fp}: stale '{pat}'")
if bad:
    print("[FAIL] 残留：")
    [print("   ", b) for b in bad]
    sys.exit(1)
print("[ok] 同步完成，无残留")
