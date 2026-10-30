"""Token 签发/吊销工具（v1.2 方案 §8.4：一人一 Token，人工签发）

用法：
    # 给既有学生签发（按 students.id）
    ./.venv/bin/python prep/mcp_token.py issue --student-id 3 --label 小明

    # 新学生 + 签发一步完成（名字会带日期后缀避免 UNIQUE 冲突时手动处理）
    ./.venv/bin/python prep/mcp_token.py issue --new-student 小明

    # 教师聚合 Token（class_summary 可用）
    ./.venv/bin/python prep/mcp_token.py issue --student-id 3 --label teacher-班主任 --teacher

    # 吊销（学生退班/泄露）
    ./.venv/bin/python prep/mcp_token.py revoke --token ck_xxx

    # 列出
    ./.venv/bin/python prep/mcp_token.py list

安全：明文 Token 只在签发时打印一次，库里只存 SHA-256。
"""
import argparse
import hashlib
import secrets
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway import db  # noqa: E402


def _new_token() -> str:
    return "ck_" + secrets.token_urlsafe(24)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cmd_issue(ns):
    if ns.new_student:
        name = ns.new_student.strip()
        st = db.create_student(name)  # INSERT IGNORE：重名时返回既有行
        student_id = st["id"]
    elif ns.student_id:
        student_id = ns.student_id
        rows = db.list_students()
        if not any(r["id"] == student_id for r in rows):
            sys.exit(f"student_id={student_id} 不存在")
    else:
        sys.exit("需要 --student-id 或 --new-student")

    raw = _new_token()
    label = ns.label or ""
    if ns.teacher and "teacher" not in label.lower():
        label = f"teacher-{label}" if label else "teacher"
    key_id = db.issue_api_key(student_id, _hash(raw), label=label)
    print("=" * 56)
    print(f"key_id      : {key_id}")
    print(f"student_id  : {student_id}")
    print(f"label       : {label or '-'}")
    print(f"rate_limit  : 120 次/小时")
    print(f"TOKEN(只显示一次): {raw}")
    print("=" * 56)
    print("请立即复制保存。库中仅存 SHA-256 哈希，遗失只能吊销重签。")


def cmd_revoke(ns):
    n = db.revoke_api_key(_hash(ns.token))
    print(f"revoked {n} key(s)" if n else "未找到有效 Token（可能已吊销）")


def cmd_list(ns):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT k.id, k.student_id, k.label, k.created_at, k.last_used_at,"
                " k.revoked_at, s.name"
                " FROM api_keys k LEFT JOIN students s ON s.id=k.student_id"
                " ORDER BY k.id DESC LIMIT 100")
            rows = cur.fetchall()
    if not rows:
        print("(无 Token)")
        return
    for r in rows:
        status = "REVOKED" if r["revoked_at"] else "active"
        used = r["last_used_at"] or "never"
        print(f"#{r['id']} student={r['student_id']}({r['name']}) "
              f"label={r['label'] or '-'} {status} used={used} "
              f"created={r['created_at']}")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("issue")
    pi.add_argument("--student-id", type=int)
    pi.add_argument("--new-student", type=str)
    pi.add_argument("--label", type=str, default="")
    pi.add_argument("--teacher", action="store_true")
    pi.set_defaults(fn=cmd_issue)

    pr = sub.add_parser("revoke")
    pr.add_argument("--token", type=str, required=True)
    pr.set_defaults(fn=cmd_revoke)

    pl = sub.add_parser("list")
    pl.set_defaults(fn=cmd_list)

    ns = p.parse_args()
    ns.fn(ns)


if __name__ == "__main__":
    main()
