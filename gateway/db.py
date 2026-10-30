"""数据层（MySQL 版）：学生 / 作答（错题本）/ 拍题
2026-10-24 从 SQLite 迁移到服务器 MySQL（独立 chemask 库，专用账号仅授权本库）。
连接配置读 .env：MYSQL_HOST/PORT/USER/PASSWORD/DB（本地开发走 SSH tunnel 3307）。
"""
import json
import os
import time
from contextlib import contextmanager

import pymysql
from dbutils.pooled_db import PooledDB
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

DB_CONF = dict(
    host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    user=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASSWORD"],
    database=os.environ["MYSQL_DB"],
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
    autocommit=False,
)

TOPIC_ZH = {
    "halogenoalkanes": "卤代烃",
    "carbonyl": "羰基化合物",
    "organic-other": "有机-其他",
    "physical-inorganic": "物化-无机",
    "unknown": "未分类",
}

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS students (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(128) NOT NULL UNIQUE,
        created_at DOUBLE NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS attempts (
        id INT AUTO_INCREMENT PRIMARY KEY,
        student_id INT NOT NULL,
        qid VARCHAR(64),
        paper VARCHAR(128),
        question_no INT,
        score INT,
        marks_total INT,
        error_types JSON,
        weak_points JSON,
        topic_detail VARCHAR(64),
        source VARCHAR(16) NOT NULL,
        created_at DOUBLE NOT NULL,
        INDEX idx_attempts_student (student_id),
        INDEX idx_attempts_qid (qid)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS uploads (
        id INT AUTO_INCREMENT PRIMARY KEY,
        filename VARCHAR(512) NOT NULL,
        sha256 CHAR(64) NOT NULL UNIQUE,
        doc_type VARCHAR(32) NOT NULL,
        status VARCHAR(16) NOT NULL,
        chunks_count INT DEFAULT 0,
        error TEXT,
        stored_path VARCHAR(512) NULL,
        created_at DOUBLE NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS rewrite_cache (
        question VARCHAR(700) NOT NULL PRIMARY KEY,
        en VARCHAR(700) NOT NULL,
        created_at DOUBLE NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS photos (
        id INT AUTO_INCREMENT PRIMARY KEY,
        student_id INT NOT NULL,
        image_path VARCHAR(512) NOT NULL,
        ocr_question MEDIUMTEXT,
        ocr_answer MEDIUMTEXT,
        topic VARCHAR(64),
        created_at DOUBLE NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(64) NOT NULL UNIQUE,
        display_name VARCHAR(128) NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        role VARCHAR(24) NOT NULL DEFAULT 'teacher',
        status VARCHAR(16) NOT NULL DEFAULT 'active',
        created_at DOUBLE NOT NULL,
        last_login_at DOUBLE NULL,
        avatar_path VARCHAR(512) NULL,
        INDEX idx_users_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS conversations (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        title VARCHAR(180) NOT NULL,
        created_at DOUBLE NOT NULL,
        updated_at DOUBLE NOT NULL,
        INDEX idx_conversations_user_time (user_id, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS chat_messages (
        id INT AUTO_INCREMENT PRIMARY KEY,
        conversation_id INT NOT NULL,
        user_id INT NOT NULL,
        role VARCHAR(16) NOT NULL,
        content MEDIUMTEXT NOT NULL,
        citations JSON NULL,
        followups JSON NULL,
        image_path VARCHAR(512) NULL,
        created_at DOUBLE NOT NULL,
        INDEX idx_chat_messages_conv (conversation_id, id),
        INDEX idx_chat_messages_user (user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    # ---- MCP 数据层（v1.2 方案 §8.5，关联键=student_id，与 attempts 同轴）----
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
    """CREATE TABLE IF NOT EXISTS api_key_calls (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        key_id INT NOT NULL,
        called_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_api_key_calls (key_id, called_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


_POOL = PooledDB(creator=pymysql, maxconnections=8, blocking=True, **DB_CONF)


@contextmanager
def get_conn():
    conn = _POOL.connection()   # E6: 池化借还，commit/rollback 语义不变
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            for ddl in SCHEMA:
                cur.execute(ddl)
            # 老库平滑迁移：uploads 记录实际存储名，chat_messages 保留提问图片。
            cur.execute(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS"
                " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='uploads'"
                " AND COLUMN_NAME='stored_path'")
            if not cur.fetchone()["n"]:
                cur.execute("ALTER TABLE uploads ADD COLUMN stored_path VARCHAR(512) NULL")
            cur.execute(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS"
                " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='chat_messages'"
                " AND COLUMN_NAME='image_path'")
            if not cur.fetchone()["n"]:
                cur.execute("ALTER TABLE chat_messages ADD COLUMN image_path VARCHAR(512) NULL")
            # MCP 数据层（v1.2）：新表在 SCHEMA 中自建；students.user_id 幂等迁移
            _touch_student_user_id(cur)
            # v1.3（评审修复）：原子幂等唯一键 / RBAC role / 乐观锁 version
            _migrate_mcp_v1_3(cur)


# ---------------- 学生 ----------------

def list_students():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, created_at FROM students ORDER BY id")
            return cur.fetchall()


def create_student(name):
    name = name.strip()
    if not name:
        raise ValueError("姓名不能为空")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO students(name, created_at) VALUES (%s, %s)",
                        (name, time.time()))
            cur.execute("SELECT id, name FROM students WHERE name=%s", (name,))
            return cur.fetchone()


# ---------------- 作答 ----------------

def find_attempt_by_idem(student_id: int, qid: str, idem_key: str):
    """幂等检查（历史数据兼容路径）：weak_points 里存过 idem:<key> 视为同一作答。
    新写入统一走 add_attempt_idempotent（数据库唯一约束保证原子性）。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, weak_points FROM attempts"
                " WHERE student_id=%s AND qid=%s ORDER BY id DESC LIMIT 50",
                (student_id, qid))
            for r in cur.fetchall():
                try:
                    wps = json.loads(r["weak_points"] or "[]")
                except (ValueError, TypeError):
                    continue
                if f"idem:{idem_key}" in wps:
                    return r["id"]
            return None


def add_attempt_idempotent(student_id: int, idem_key: str, qid: str, **fields) -> tuple[int, bool]:
    """原子幂等插入：UNIQUE(student_id, qid, idem_key) 由数据库保证，并发重试只落一行。
    返回 (attempt_id, duplicate)。捕获 1062 唯一键冲突后回读已有行——
    教训：check→insert 两步在并发下有竞态窗口，幂等必须由唯一约束+原子写入保证。"""
    assert idem_key, "idem_key required"
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO attempts(student_id, qid, idem_key, paper, question_no,"
                    " score, marks_total, error_types, weak_points, topic_detail, source,"
                    " created_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (student_id, qid, idem_key, fields.get("paper"),
                     fields.get("question_no"), fields.get("score"),
                     fields.get("marks_total"),
                     json.dumps(fields.get("error_types") or [], ensure_ascii=False),
                     json.dumps(fields.get("weak_points") or [], ensure_ascii=False),
                     fields.get("topic_detail"), fields.get("source", "mcp"),
                     time.time()))
                return cur.lastrowid, False
            except pymysql.err.IntegrityError as e:
                if e.args[0] != 1062:
                    raise
        # 冲突：回读已存在的那一行
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM attempts WHERE student_id=%s AND qid=%s AND idem_key=%s",
                (student_id, qid, idem_key))
            return cur.fetchone()["id"], True


def add_attempt(student_id, qid, paper=None, question_no=None, score=None,
                marks_total=None, error_types=None, weak_points=None,
                topic_detail=None, source="drill"):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO attempts(student_id, qid, paper, question_no, score, marks_total,"
                " error_types, weak_points, topic_detail, source, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (student_id, qid, paper, question_no, score, marks_total,
                 json.dumps(error_types or [], ensure_ascii=False),
                 json.dumps(weak_points or [], ensure_ascii=False),
                 topic_detail, source, time.time()))
            return cur.lastrowid


_ATTEMPT_FIELDS = ("id, student_id, qid, paper, question_no, score, marks_total,"
                   " error_types, weak_points, topic_detail, source, created_at")


def attempts_of(student_id, only_qbank=True):
    q = f"SELECT {_ATTEMPT_FIELDS} FROM attempts WHERE student_id=%s"
    if only_qbank:
        q += " AND qid NOT LIKE 'photo-%%'"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q + " ORDER BY created_at", (student_id,))
            return cur.fetchall()


def mistake_groups(student_id):
    """按 qid 聚合作答历史（错题本视图）"""
    rows = attempts_of(student_id, only_qbank=True)
    groups = {}
    for r in rows:
        g = groups.setdefault(r["qid"], {"qid": r["qid"], "paper": r["paper"],
                                         "question_no": r["question_no"],
                                         "topic_detail": r["topic_detail"],
                                         "history": []})
        g["history"].append({
            "score": r["score"], "marks_total": r["marks_total"],
            "error_types": json.loads(r["error_types"] or "[]"),
            "weak_points": json.loads(r["weak_points"] or "[]"),
            "source": r["source"], "at": r["created_at"],
        })
    out = []
    for g in groups.values():
        last = g["history"][-1]
        g["last_score"] = last["score"]
        g["last_marks"] = last["marks_total"]
        g["attempts_count"] = len(g["history"])
        g["best_score"] = max((h["score"] or 0) for h in g["history"])
        # D1: 只保留最后一次未拿满分的题（注释语义对齐；重练满分的自动移出错题本）
        if g["last_score"] is not None and g["last_marks"] and g["last_score"] >= g["last_marks"]:
            continue
        out.append(g)
    return sorted(out, key=lambda g: -g["attempts_count"])


# ---------------- 掌握度 ----------------

def mastery_by_topic(student_id):
    """按主题聚合：掌握度 = 得分/总分（仅题库题）；错因分布含拍题"""
    rows = attempts_of(student_id, only_qbank=True)
    agg = {}
    for r in rows:
        if r["score"] is not None and r["marks_total"]:  # D2: 仅统计有分值且总分>0的作答
            t = agg.setdefault(r["topic_detail"] or "unknown",
                               {"attempts": 0, "score": 0, "marks": 0})
            t["attempts"] += 1
            t["score"] += r["score"] or 0
            t["marks"] += r["marks_total"] or 0
    for t in agg.values():
        t["mastery"] = round(t["score"] / t["marks"], 3) if t["marks"] else None
    # 错因分布（全部来源）
    err = {}
    all_rows = attempts_of(student_id, only_qbank=False)
    for r in all_rows:
        for e in json.loads(r["error_types"] or "[]"):
            err[e] = err.get(e, 0) + 1
    return agg, err


def class_mastery():
    """班级视图：学生 × 主题 掌握度矩阵（主题名统一中文）"""
    result = {}
    for s in list_students():
        agg, _ = mastery_by_topic(s["id"])
        result[s["name"]] = {TOPIC_ZH.get(k, k): v.get("mastery") for k, v in agg.items()} | \
                            {"_total_attempts": sum(v["attempts"] for v in agg.values())}
    return result


# ---------------- 拍题 ----------------

def add_photo(student_id, image_path, ocr_question="", ocr_answer="", topic=""):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO photos(student_id, image_path, ocr_question, ocr_answer, topic, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s)",
                (student_id, image_path, ocr_question, ocr_answer, topic, time.time()))
            return cur.lastrowid


def _touch_student_user_id(cur):
    """老库平滑迁移：students.user_id 列（幂等）"""
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='students'"
        " AND COLUMN_NAME='user_id'")
    if not cur.fetchone()["n"]:
        cur.execute("ALTER TABLE students ADD COLUMN user_id INT NULL")


def _migrate_mcp_v1_3(cur):
    """v1.3 迁移（评审修复）：幂等走唯一约束 + RBAC role 列 + 乐观锁 version。
    全部幂等，可重复执行。"""
    # 1) attempts.idem_key 列 + 唯一约束（原子幂等）
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='attempts'"
        " AND COLUMN_NAME='idem_key'")
    if not cur.fetchone()["n"]:
        cur.execute("ALTER TABLE attempts ADD COLUMN idem_key VARCHAR(128) NULL")
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.STATISTICS"
        " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='attempts'"
        " AND INDEX_NAME='uq_attempts_idem'")
    if not cur.fetchone()["n"]:
        # 存量 weak_points 里的 idem:xxx 搬到新列（一次性数据迁移）
        cur.execute("SELECT id, weak_points FROM attempts WHERE idem_key IS NULL")
        for r in cur.fetchall():
            try:
                wps = json.loads(r["weak_points"] or "[]")
            except (ValueError, TypeError):
                continue
            keys = [w[5:] for w in wps if isinstance(w, str) and w.startswith("idem:")]
            if keys:
                cur.execute("UPDATE attempts SET idem_key=%s WHERE id=%s",
                            (keys[0], r["id"]))
        # NULL 不参与唯一约束，历史无 idem 数据不受影响
        cur.execute("ALTER TABLE attempts ADD UNIQUE KEY uq_attempts_idem"
                    " (student_id, qid, idem_key)")
    # 2) api_keys.role 列（RBAC，替代 label 字符串约定）
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='api_keys'"
        " AND COLUMN_NAME='role'")
    if not cur.fetchone()["n"]:
        cur.execute("ALTER TABLE api_keys ADD COLUMN role VARCHAR(16)"
                    " NOT NULL DEFAULT 'student'")
        # 存量 teacher-* label 升级为正式 role
        cur.execute("UPDATE api_keys SET role='teacher' WHERE label LIKE '%teacher%'")
    # 3) student_memory.version 列（乐观锁，防并发 Lost Update）
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='student_memory'"
        " AND COLUMN_NAME='version'")
    if not cur.fetchone()["n"]:
        cur.execute("ALTER TABLE student_memory ADD COLUMN version INT"
                    " NOT NULL DEFAULT 1")


init_db()


def set_user_avatar(user_id: int, avatar_path: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET avatar_path=%s WHERE id=%s",
                        (avatar_path, user_id))


def get_user_avatar_path(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT avatar_path FROM users WHERE id=%s AND status='active'",
                        (user_id,))
            return cur.fetchone()["avatar_path"]


# ---------------- 对话历史（按登录账号隔离） ----------------

def create_conversation(user_id: int, title: str):
    title = title.strip()[:180] or "新的化学问题"
    now = time.time()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations(user_id, title, created_at, updated_at)"
                " VALUES (%s,%s,%s,%s)", (user_id, title, now, now))
            return cur.lastrowid


def list_conversations(user_id: int, limit: int = 100):
    limit = max(1, min(int(limit), 200))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, c.title, c.created_at, c.updated_at,"
                " COUNT(m.id) AS message_count"
                " FROM conversations c LEFT JOIN chat_messages m ON m.conversation_id=c.id"
                " WHERE c.user_id=%s"
                " GROUP BY c.id, c.title, c.created_at, c.updated_at"
                " ORDER BY c.updated_at DESC LIMIT %s", (user_id, limit))
            return cur.fetchall()


def get_conversation(user_id: int, conversation_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, user_id, title, created_at, updated_at"
                " FROM conversations WHERE id=%s AND user_id=%s",
                (conversation_id, user_id))
            return cur.fetchone()


def add_chat_message(user_id: int, conversation_id: int, role: str, content: str,
                     citations=None, followups=None, image_path: str | None = None):
    if role not in {"user", "assistant"}:
        raise ValueError("不支持的对话角色")
    now = time.time()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_messages(conversation_id, user_id, role, content,"
                " citations, followups, image_path, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (conversation_id, user_id, role, content,
                 json.dumps(citations or [], ensure_ascii=False),
                 json.dumps(followups or [], ensure_ascii=False), image_path, now))
            cur.execute("UPDATE conversations SET updated_at=%s WHERE id=%s",
                        (now, conversation_id))
            return cur.lastrowid


def list_chat_messages(user_id: int, conversation_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, role, content, citations, followups, image_path, created_at"
                " FROM chat_messages WHERE conversation_id=%s AND user_id=%s"
                " ORDER BY id", (conversation_id, user_id))
            rows = cur.fetchall()
    for row in rows:
        for key in ("citations", "followups"):
            if isinstance(row.get(key), str):
                try:
                    row[key] = json.loads(row[key])
                except json.JSONDecodeError:
                    row[key] = []
    return rows


def rename_conversation(user_id: int, conversation_id: int, title: str):
    title = title.strip()[:180]
    if not title:
        raise ValueError("标题不能为空")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE conversations SET title=%s WHERE id=%s AND user_id=%s",
                        (title, conversation_id, user_id))
            return cur.rowcount > 0


def delete_conversation(user_id: int, conversation_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM chat_messages WHERE conversation_id=%s AND user_id=%s",
                (conversation_id, user_id))
            cur.execute("DELETE FROM conversations WHERE id=%s AND user_id=%s",
                        (conversation_id, user_id))
            return cur.rowcount > 0


# ---------------- 资料上传（B2） ----------------

def add_upload(filename, sha256, doc_type, stored_path: str | None = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO uploads(filename, sha256, doc_type, status,"
                        " stored_path, created_at)"
                        " VALUES (%s,%s,%s,'processing',%s,%s)",
                        (filename, sha256, doc_type, stored_path, time.time()))
            return cur.lastrowid


def get_upload(upload_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, filename, sha256, doc_type, status, chunks_count,"
                        " stored_path,"
                        " error, created_at FROM uploads WHERE id=%s", (upload_id,))
            return cur.fetchone()


def find_upload_by_hash(sha256):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, filename, status FROM uploads WHERE sha256=%s", (sha256,))
            return cur.fetchone()


def list_uploads():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, filename, doc_type, status, chunks_count,"
                        " stored_path, error, created_at"
                        " FROM uploads ORDER BY id DESC LIMIT 100")
            return cur.fetchall()


def set_upload_status(upload_id, status, chunks_count=0, error=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE uploads SET status=%s, chunks_count=%s, error=%s WHERE id=%s",
                        (status, chunks_count, error, upload_id))


# ---------------- C1: 查询改写持久缓存 ----------------

def get_rewrite(question):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT en FROM rewrite_cache WHERE question=%s", (question,))
            row = cur.fetchone()
            return row["en"] if row else None


def put_rewrite(question, en):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO rewrite_cache(question, en, created_at)"
                        " VALUES (%s,%s,%s)", (question, en, time.time()))


# ---------------- MCP 数据层（v1.2 方案 §8.4/8.5）----------------
# 关联键统一为 student_id（与 attempts 同轴）。
# 注意：students.name 带 UNIQUE 约束——MCP 建档一律走 Token→student_id，
# 禁止按名字查重/建档（重名 INSERT 会直接报错）。


# ---- Token 鉴权 ----

def lookup_api_key(token_hash: str):
    """按 SHA-256 哈希查有效 Token，返回 {key_id, student_id, rate_limit_h, label, role} 或 None"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, student_id, rate_limit_h, label, role FROM api_keys"
                " WHERE token_hash=%s AND revoked_at IS NULL", (token_hash,))
            return cur.fetchone()


def touch_api_key(key_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE api_keys SET last_used_at=NOW() WHERE id=%s", (key_id,))


def rate_count_last_hour(key_id: int) -> int:
    """网关调用日志近 1 小时计数（mcp_call_log 复用 dialogue_log 表结构不适合，用独立轻量表）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM api_key_calls"
                " WHERE key_id=%s AND called_at > NOW() - INTERVAL 1 HOUR",
                (key_id,))
            n = cur.fetchone()["n"]
            cur.execute("INSERT INTO api_key_calls(key_id) VALUES (%s)", (key_id,))
            return n


def issue_api_key(student_id: int, token_hash: str, label: str | None = None,
                  rate_limit_h: int = 120, role: str = "student") -> int:
    """签发 Token。role 是正式权限字段（student/teacher）——
    教训：'label 包含 teacher' 的字符串约定不是权限模型。"""
    assert role in ("student", "teacher"), f"invalid role: {role}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys(token_hash, student_id, label, rate_limit_h, role)"
                " VALUES (%s,%s,%s,%s,%s)",
                (token_hash, student_id, label, rate_limit_h, role))
            return cur.lastrowid


def revoke_api_key(token_hash: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET revoked_at=NOW()"
                " WHERE token_hash=%s AND revoked_at IS NULL", (token_hash,))
            return cur.rowcount


# ---- 画像 / 记忆 ----

def get_student_memory(student_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM student_memory WHERE student_id=%s",
                        (student_id,))
            return cur.fetchone()


def get_memory_version(student_id: int) -> int | None:
    """当前画像行版本号（无行返回 None）——乐观锁的读取入口"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM student_memory WHERE student_id=%s",
                        (student_id,))
            r = cur.fetchone()
            return r["version"] if r else None


def upsert_student_memory(student_id: int, expected_version: int | None = None,
                          **fields):
    """只更新传入字段；新增画像行时其余字段留 NULL。
    乐观锁：传 expected_version 时，版本不匹配抛 MemoryConflict（调用方重读重试）——
    教训：read-merge-write 在并发下会 Lost Update，靠 version 条件更新保证。"""
    allowed = ("exam_board", "target_grade", "current_score", "weak_topics",
               "taught_topics", "plan_stage", "preferences", "summary")
    cols, vals = [], []
    for k in allowed:
        if k in fields and fields[k] is not None:
            v = fields[k]
            if isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False)
            cols.append(k)
            vals.append(v)
    if not cols:
        # 空建档也确保行存在（幂等）
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT IGNORE INTO student_memory(student_id)"
                            " VALUES (%s)", (student_id,))
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            if expected_version is None:
                placeholders = ", ".join(["%s"] * (len(cols) + 1))
                set_clause = ", ".join(f"{c}=%s" for c in cols)
                cur.execute(
                    f"INSERT INTO student_memory(student_id, {', '.join(cols)})"
                    f" VALUES ({placeholders})"
                    f" ON DUPLICATE KEY UPDATE {set_clause}, version=version+1",
                    (student_id, *vals, *vals))
            else:
                # 乐观锁路径（标准两步，同一事务内原子生效）：
                # 1) 确保行存在（新学生首次并发建档也安全）
                cur.execute("INSERT IGNORE INTO student_memory(student_id, version)"
                            " VALUES (%s, 1)", (student_id,))
                # 2) 条件 UPDATE：版本匹配才写入并自增；rowcount==0 → 并发冲突
                set_clause = ", ".join(f"{c}=%s" for c in cols)
                cur.execute(
                    f"UPDATE student_memory SET {set_clause}, version=version+1"
                    f" WHERE student_id=%s AND version=%s",
                    (*vals, student_id, expected_version))
                if cur.rowcount == 0:
                    raise MemoryConflict(student_id, expected_version)


class MemoryConflict(Exception):
    """乐观锁冲突：读取到的 version 已过期（并发写入发生）"""

    def __init__(self, student_id: int, expected_version: int):
        self.student_id, self.expected_version = student_id, expected_version


def add_dialogue(student_id: int, session_id: str, role: str, digest: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dialogue_log(student_id, session_id, role, digest)"
                " VALUES (%s,%s,%s,%s)", (student_id, session_id, role, digest[:500]))
            return cur.lastrowid


def recent_dialogues(student_id: int, limit: int = 3):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role, digest, session_id, created_at FROM dialogue_log"
                " WHERE student_id=%s ORDER BY id DESC LIMIT %s",
                (student_id, limit))
            return cur.fetchall()


def add_report(student_id: int, report_type: str, content_md: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reports(student_id, report_type, content_md)"
                " VALUES (%s,%s,%s)", (student_id, report_type, content_md))
            return cur.lastrowid


def latest_report_meta(student_id: int, summary_chars: int = 200):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, report_type, created_at,"
                " LEFT(content_md, %s) AS summary, CHAR_LENGTH(content_md) AS full_len"
                " FROM reports WHERE student_id=%s ORDER BY id DESC LIMIT 1",
                (summary_chars, student_id))
            return cur.fetchone()


def latest_report_full(student_id: int):
    """最近一份报告全文（get_report 工具专用，SQL 收敛在 db 层）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT report_type, content_md, created_at FROM reports"
                " WHERE student_id=%s ORDER BY id DESC LIMIT 1", (student_id,))
            return cur.fetchone()


def clear_student_memory(student_id: int) -> int:
    """学生可申请清空自己的记忆（画像+对话摘要；attempts 明细保留可重建画像核心）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dialogue_log WHERE student_id=%s", (student_id,))
            n = cur.rowcount
            cur.execute("DELETE FROM student_memory WHERE student_id=%s", (student_id,))
            return n + cur.rowcount
