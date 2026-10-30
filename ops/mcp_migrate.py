"""MCP 数据层迁移脚本（v1.2 方案 §8.5）
新增：api_keys / student_memory / dialogue_log / reports 四张表
修改：students 加 user_id 列（登录账号与学情主体打通）

用法（本地连服务器库，走 SSH 隧道 3307）：
    ./.venv/bin/python prep/mcp_migrate.py          # 执行迁移
    ./.venv/bin/python prep/mcp_migrate.py --check  # 只检查不执行
安全：幂等（重复执行无副作用）；不删除任何既有列/表。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:                                    # chem-mcp 仓库布局（gateway 包）
    from gateway.db import get_conn  # noqa: E402
except ModuleNotFoundError:             # 服务器 chemask 布局（app 包）
    from app.db import get_conn  # noqa: E402

DDL = [
    """CREATE TABLE IF NOT EXISTS api_keys (
        id INT AUTO_INCREMENT PRIMARY KEY,
        token_hash CHAR(64) UNIQUE NOT NULL,
        student_id INT NOT NULL,
        label VARCHAR(64) NULL,
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
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_reports_student_time (student_id, created_at)
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
                # "CREATE TABLE IF NOT EXISTS students (" → name 在 "EXISTS" 后
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

            if apply:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM information_schema.TABLES"
                    " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN"
                    " ('api_keys','student_memory','dialogue_log','reports')"
                )
                n = cur.fetchone()["n"]
                print(f"[verify] {n}/4 MCP tables present")


if __name__ == "__main__":
    run(apply="--check" not in sys.argv)
