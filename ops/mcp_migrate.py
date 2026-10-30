"""MCP 数据层迁移脚本（v1.2 方案 §8.5 + v1.3 评审修复）

v1.2 新增表：api_keys / student_memory / dialogue_log / reports
v1.2 修改：students 加 user_id 列（登录账号与学情主体打通）
v1.3（外部评审修复 2026-10-30）：
    - attempts.idem_key 列 + UNIQUE(student_id, qid, idem_key)（原子幂等，
      替代 check→insert 的竞态实现；存量 weak_points 里的 idem:xxx 自动搬迁）
    - api_keys.role 列（RBAC，替代 "label 含 teacher" 的字符串约定；
      存量 teacher-* label 自动升级为 role='teacher'）
    - student_memory.version 列（乐观锁，防并发 read-merge-write Lost Update）

用法（本地连服务器库，走 SSH 隧道 3307）：
    ./.venv/bin/python ops/mcp_migrate.py          # 执行迁移
安全：幂等（重复执行无副作用）；不删除任何既有列/表。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.db import get_conn  # noqa: E402

DDL = [
    """CREATE TABLE IF NOT EXISTS api_keys (
        id INT AUTO_INCREMENT PRIMARY KEY,
        token_hash CHAR(64) UNIQUE NOT NULL,
        student_id INT NOT NULL,
        label VARCHAR(64) NULL,
        role VARCHAR(16) NOT NULL DEFAULT 'student',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_used_at DATETIME NULL,
        revoked_at DATETIME NULL,
        rate_limit_h INT NOT NULL DEFAULT 120,
        INDEX idx_api_keys_student (student_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS student_memory (
        id INT AUTO_INCREMENT PRIMARY KEY,
        student_id INT UNIQUE NOT NULL,
        exam_board VARCHAR(32) NULL,
        target_grade VARCHAR(8) NULL,
        current_score VARCHAR(8) NULL,
        weak_topics JSON NULL,
        taught_topics JSON NULL,
        plan_stage VARCHAR(32) NULL,
        preferences JSON NULL,
        summary TEXT NULL,
        version INT NOT NULL DEFAULT 1,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS dialogue_log (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        student_id INT NOT NULL,
        session_id VARCHAR(64) NOT NULL,
        role ENUM('student', 'tutor') NOT NULL,
        digest VARCHAR(500) NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_dialogue_student_time (student_id, created_at),
        INDEX idx_dialogue_session (session_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS reports (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        student_id INT NOT NULL,
        report_type ENUM('six_section', 'weekly', 'milestone') NOT NULL,
        content_md MEDIUMTEXT NOT NULL,
        report_key VARCHAR(128) NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_reports_key (student_id, report_key),
        INDEX idx_reports_student_time (student_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS api_key_calls (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        key_id INT NOT NULL,
        called_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_api_key_calls (key_id, called_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

STUDENTS_USER_ID = (
    "ALTER TABLE students ADD COLUMN user_id INT NULL, "
    "ADD INDEX idx_students_user (user_id)"
)


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
        (table, column),
    )
    return bool(cur.fetchone()["n"])


def run(apply: bool = False):
    with get_conn() as conn:
        with conn.cursor() as cur:
            for ddl in DDL:
                toks = ddl.split()
                name = toks[toks.index("EXISTS") + 1]
                if apply:
                    cur.execute(ddl)
                    print(f"[ok] ensured table {name}")
                else:
                    print(f"[check] would ensure table {name}")

            if _column_exists(cur, "students", "user_id"):
                print("[skip] students.user_id already exists")
            elif apply:
                cur.execute(STUDENTS_USER_ID)
                print("[ok] added students.user_id")
            else:
                print("[check] would add students.user_id")

            # ---- v1.3 ----
            # attempts.idem_key + 唯一约束（原子幂等；含存量数据搬迁）
            if _column_exists(cur, "attempts", "idem_key"):
                print("[skip] attempts.idem_key already exists")
            elif apply:
                import json as _json
                cur.execute("ALTER TABLE attempts ADD COLUMN idem_key VARCHAR(128) NULL")
                cur.execute(
                    "SELECT id, weak_points FROM attempts WHERE idem_key IS NULL")
                for r in cur.fetchall():
                    try:
                        wps = _json.loads(r["weak_points"] or "[]")
                    except (ValueError, TypeError):
                        continue
                    keys = [w[5:] for w in wps
                            if isinstance(w, str) and w.startswith("idem:")]
                    if keys:
                        cur.execute("UPDATE attempts SET idem_key=%s WHERE id=%s",
                                    (keys[0], r["id"]))
                cur.execute("ALTER TABLE attempts ADD UNIQUE KEY uq_attempts_idem"
                            " (student_id, qid, idem_key)")
                print("[ok] attempts.idem_key + UNIQUE(student_id,qid,idem_key)")
            else:
                print("[check] would add attempts.idem_key + unique key")

            # api_keys.role（RBAC；存量 teacher-* label 升级）
            if _column_exists(cur, "api_keys", "role"):
                print("[skip] api_keys.role already exists")
            elif apply:
                cur.execute("ALTER TABLE api_keys ADD COLUMN role VARCHAR(16)"
                            " NOT NULL DEFAULT 'student'")
                cur.execute("UPDATE api_keys SET role='teacher'"
                            " WHERE label LIKE '%teacher%'")
                print("[ok] api_keys.role (legacy teacher labels upgraded)")
            else:
                print("[check] would add api_keys.role")

            # student_memory.version（乐观锁）
            if _column_exists(cur, "student_memory", "version"):
                print("[skip] student_memory.version already exists")
            elif apply:
                cur.execute("ALTER TABLE student_memory ADD COLUMN version INT"
                            " NOT NULL DEFAULT 1")
                print("[ok] student_memory.version")
            else:
                print("[check] would add student_memory.version")

            # v1.4：reports.report_key（报告幂等写入）
            if _column_exists(cur, "reports", "report_key"):
                print("[skip] reports.report_key already exists")
            elif apply:
                cur.execute("ALTER TABLE reports ADD COLUMN report_key VARCHAR(128) NULL")
                cur.execute("ALTER TABLE reports ADD UNIQUE KEY uq_reports_key"
                            " (student_id, report_key)")
                print("[ok] reports.report_key + UNIQUE(student_id,report_key)")
            else:
                print("[check] would add reports.report_key + unique key")

            if apply:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM information_schema.TABLES"
                    " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN"
                    " ('api_keys','student_memory','dialogue_log','reports','api_key_calls')"
                )
                n = cur.fetchone()["n"]
                print(f"[verify] {n}/5 MCP tables present")


if __name__ == "__main__":
    run(apply="--check" not in sys.argv)
