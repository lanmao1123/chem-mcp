#!/usr/bin/env python3
"""生产部署对账脚本（六轮评审任务 9.1）。

在生产服务器本地运行：读取 build_manifest.json，逐文件计算 SHA-256 并比对。
不主动 SSH、不联网、不读取任何凭据；只做本地文件哈希对账。

用法：
    python3 ops/verify_deployment.py                          # 默认 ./build_manifest.json + 当前目录为根
    python3 ops/verify_deployment.py --manifest build_manifest.json --root /home/ubuntu/chemask
    python3 ops/verify_deployment.py --strict-missing         # 所有缺失文件一律 FAIL（默认 SKIP 未部署文件）

语义：
    - 发布文件在服务器上的布局与仓库不同（chemask 布局：gateway/→app/，ops/→prep/）。
      DEFAULT_SERVER_MAP 提供仓库路径 → 服务器路径映射；可用 --map-file 覆盖。
    - README/DESIGN/专家包/test_release_gate.py 属仓库侧产物，不部署到服务器，
      默认记为 SKIP（可用 --strict-missing 改为 FAIL）。
    - 任何哈希不匹配（MISMATCH）或非预期缺失（MISSING）→ 退出码 1。
    - 输出 release_commit 与 version；不输出任何敏感值。
"""
import argparse
import hashlib
import json
import os
import sys

# 仓库路径 → 服务器（chemask 布局）相对路径映射
DEFAULT_SERVER_MAP = {
    "gateway/mcp_gateway.py": "app/mcp_gateway.py",
    "gateway/db.py": "app/db.py",
    "tests/test_mcp_gateway.py": "tests/test_mcp_gateway.py",
    "tests/test_release_gate.py": None,          # 仓库侧测试产物，不部署
    "ops/mcp_migrate.py": "prep/mcp_migrate.py",
    "ops/mcp_token.py": "prep/mcp_token.py",
    "ops/mcp_echo_server.py": "prep/mcp_echo_server.py",
    "ops/verify_deployment.py": "ops/verify_deployment.py",
    "README.md": None,                            # 仓库侧文档，不部署
    "requirements-release.txt": None,             # 仅 CI/部署环境用，网关运行时不读
    "DESIGN.md": None,
    "expert-package/.codebuddy-plugin/plugin.json": None,
    "expert-package/agents/cie-chem-coach.md": None,
}


def sha256_file(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description="生产部署逐文件哈希对账")
    ap.add_argument("--manifest", default="build_manifest.json")
    ap.add_argument("--root", default=".", help="服务器上发布文件的根目录")
    ap.add_argument("--map-file", default=None,
                    help="自定义仓库路径→服务器路径映射 JSON（覆盖默认）")
    ap.add_argument("--strict-missing", action="store_true",
                    help="所有缺失文件一律 FAIL（默认未部署文件记 SKIP）")
    args = ap.parse_args()

    if not os.path.isfile(args.manifest):
        print(f"[FAIL] manifest 不存在：{args.manifest}")
        sys.exit(1)
    try:
        m = json.load(open(args.manifest, encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[FAIL] manifest JSON 解析失败：{e}")
        sys.exit(1)

    server_map = dict(DEFAULT_SERVER_MAP)
    if args.map_file:
        server_map.update(json.load(open(args.map_file, encoding="utf-8")))

    print(f"release_commit: {m.get('release_commit', '?')[:12]}")
    print(f"version: {m.get('version', '?')}")
    print(f"根目录: {os.path.abspath(args.root)}")
    print("-" * 60)

    ok = miss = mismatch = skip = 0
    problems = []
    for rel, expected in m.get("files", {}).items():
        srv = server_map.get(rel)
        if srv is None:
            skip += 1
            print(f"[SKIP] {rel} —— 仓库侧产物，不部署到服务器")
            continue
        fp = os.path.join(args.root, srv)
        if not os.path.isfile(fp):
            miss += 1
            verdict = "FAIL" if args.strict_missing else "MISSING"
            problems.append(f"{rel} -> {srv} 缺失")
            print(f"[{verdict}] {rel} -> {srv} 缺失")
            continue
        actual = sha256_file(fp)
        if actual != expected:
            mismatch += 1
            problems.append(f"{rel} -> {srv} 哈希不符 manifest={expected[:12]} 实际={actual[:12]}")
            print(f"[MISMATCH] {rel} -> {srv} manifest={expected[:12]} 实际={actual[:12]}")
        else:
            ok += 1
            print(f"[OK] {rel} -> {srv}")

    print("-" * 60)
    print(f"结果：OK={ok} MISMATCH={mismatch} MISSING={miss} SKIP={skip}")
    if problems:
        print("[FAIL] 对账未通过：")
        for p in problems:
            print("    ", p)
        sys.exit(1)
    print("[PASS] 全部已部署文件与 manifest 逐字节一致")
    sys.exit(0)


if __name__ == "__main__":
    main()
