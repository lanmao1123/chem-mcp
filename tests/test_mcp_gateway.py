"""MCP 网关链路测试（D1-0b，v1.2 方案 §8.4/8.5）

覆盖：迁移幂等 / Token 签发-鉴权-吊销 / 限流 / 7+1 工具语义 /
幂等 record_attempt / weak_topics 合并 / 自动 solved / 权限边界 / 错误路径。
跑法：./.venv/bin/python -m pytest tests/test_mcp_gateway.py -q
说明：直接打真实 MySQL（走 .env，本地即 SSH 隧道 3307），测试数据用
      test-mcp- 前缀隔离，结束清理。
"""
import hashlib
import secrets
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from gateway import db
from gateway.mcp_gateway import app, _merge_weak_topics, _token_hash

client = TestClient(app)


# ---------------- fixtures ----------------

@pytest.fixture(scope="module")
def student():
    """专用测试学生（走正常建档，避免动既有 students）"""
    name = f"test-mcp-{uuid.uuid4().hex[:8]}"
    st = db.create_student(name)
    yield st["id"]
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for t in ("api_keys", "student_memory", "dialogue_log", "reports",
                      "api_key_calls", "attempts"):
                col = "student_id" if t != "api_keys" and t != "api_key_calls" else (
                    "student_id" if t == "api_keys" else "key_id")
                cur.execute(f"DELETE FROM {t} WHERE {col}=%s",
                            (st["id"] if col == "student_id" else -1,))
            cur.execute("DELETE FROM students WHERE id=%s", (st["id"],))


@pytest.fixture(scope="module")
def token(student):
    raw = "ck_test_" + secrets.token_urlsafe(18)
    db.issue_api_key(student, _token_hash(raw), label="pytest")
    yield raw
    db.revoke_api_key(_token_hash(raw))


def _call(method, token, params=None, raw_token=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body,
                       headers={"Authorization": f"Bearer {raw_token or token}"})


def _tool(name, args, token):
    r = _call("tools/call", token, {"name": name, "arguments": args})
    assert r.status_code == 200, r.text
    return r.json()["result"]["content"][0]["text"]


# ---------------- 基础协议 ----------------

def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"]


def test_initialize_no_auth():
    r = _call("initialize", token=None, raw_token="whatever")
    assert r.status_code == 200
    assert r.json()["result"]["serverInfo"]["name"] == "chemask-mcp"


def test_tools_list_requires_auth(token):
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list"}).status_code == 401
    r = _call("tools/list", token)
    body = r.json()
    assert "result" in body, f"tools/list 失败: {r.status_code} {body}"
    names = [t["name"] for t in body["result"]["tools"]]
    for must in ("get_profile", "update_profile", "save_memory", "record_attempt",
                 "list_mistakes", "get_mastery", "class_summary", "get_report"):
        assert must in names


def test_bad_token_401():
    r = _call("tools/list", None, raw_token="ck_invalid_xxx")
    assert r.status_code == 401


def test_revoked_token_401(student):
    raw = "ck_revoke_" + secrets.token_urlsafe(12)
    db.issue_api_key(student, _token_hash(raw), label="pytest-revoke")
    assert _call("tools/list", None, raw_token=raw).status_code == 200
    db.revoke_api_key(_token_hash(raw))
    assert _call("tools/list", None, raw_token=raw).status_code == 401


def test_unknown_tool_404(token):
    r = _call("tools/call", token, {"name": "drop_table", "arguments": {}})
    assert r.status_code == 404


# ---------------- get_profile / update_profile ----------------

def test_profile_empty_then_update(student, token):
    out = _tool("get_profile", {}, token)
    import json
    p = json.loads(out)
    assert p["student_id"] == student
    assert p["memory"]["stale"] is True  # 无画像=视为陈旧，提示词会主动问近况

    out = _tool("update_profile", {
        "exam_board": "9701/22", "target_grade": "a", "current_score": "55-65%",
        "summary": "首访建档"}, token)
    assert '"ok": true' in out

    p = json.loads(_tool("get_profile", {}, token))
    assert p["memory"]["exam_board"] == "9701/22"
    assert p["memory"]["target_grade"] == "a"


def test_update_profile_empty_ok(student, token):
    """空 update_profile 只确保行存在，不报错"""
    out = _tool("update_profile", {}, token)
    assert '"ok": true' in out


# ---------------- save_memory 合并语义 ----------------

def test_save_memory_merges(student, token):
    import json
    _tool("save_memory", {"weak_topic": "卤代烃消去机理",
                          "session_id": "s1", "digest": "问了消去反应"}, token)
    _tool("save_memory", {"weak_topic": "羰基亲核加成",
                          "taught_topic": "SN2 与 E2 竞争",
                          "plan_stage": "第二轮·第3天",
                          "summary_patch": "2026-10-29 讲了E2"}, token)
    m = json.loads(_tool("get_profile", {}, token))["memory"]
    topics = {w["topic"]: w["status"] for w in m["weak_topics"]}
    assert topics == {"卤代烃消去机理": "open", "羰基亲核加成": "open"}
    assert "SN2 与 E2 竞争" in m["taught_topics"]
    assert m["plan_stage"] == "第二轮·第3天"
    assert "E2" in m["summary"]
    # 摘要合并非覆盖
    _tool("save_memory", {"summary_patch": "2026-10-30 练了真题"}, token)
    m2 = json.loads(_tool("get_profile", {}, token))["memory"]
    assert "E2" in m2["summary"] and "真题" in m2["summary"]
    # digest 落对话明细
    dl = db.recent_dialogues(student, limit=3)
    assert any("消去" in d["digest"] for d in dl)


def test_weak_topic_solved(student, token):
    import json
    _tool("save_memory", {"weak_topic": "红外光谱"}, token)
    _tool("save_memory", {"weak_topic_solved": "红外光谱"}, token)
    topics = {w["topic"]: w["status"]
              for w in json.loads(_tool("get_profile", {}, token))["memory"]["weak_topics"]}
    assert topics["红外光谱"] == "solved"


def test_merge_weak_topics_pure():
    """合并纯函数：去重、顺序保持、solved 状态机"""
    cur = [{"topic": "A", "status": "open"}]
    m1 = _merge_weak_topics(cur, "B", None)
    assert [x["topic"] for x in m1] == ["A", "B"]
    m2 = _merge_weak_topics(m1, "A", None)  # 重复添加不产生新条目
    assert len([x for x in m2 if x["topic"] == "A"]) == 1
    m3 = _merge_weak_topics(m2, None, "A")
    assert next(x for x in m3 if x["topic"] == "A")["status"] == "solved"
    m4 = _merge_weak_topics(m3, None, "C")  # solved 一个不存在的 → 直接以 solved 入列
    assert next(x for x in m4 if x["topic"] == "C")["status"] == "solved"


# ---------------- record_attempt 幂等 + 自动 solved ----------------

def test_record_attempt_idempotent(student, token):
    import json
    idem = uuid.uuid4().hex
    args = {"qid": "test-q-1", "paper": "9701/22", "question_no": 3,
            "score": 2, "marks_total": 4, "error_types": ["概念混淆"],
            "topic_detail": "carbonyl", "idem_key": idem}
    r1 = json.loads(_tool("record_attempt", args, token))
    r2 = json.loads(_tool("record_attempt", args, token))
    assert r1["duplicate"] is False and r2["duplicate"] is True
    assert r1["attempt_id"] == r2["attempt_id"]
    rows = db.attempts_of(student)
    mine = [r for r in rows if r["qid"] == "test-q-1"]
    assert len(mine) == 1  # 只落一条


def test_record_attempt_no_idem_still_works(student, token):
    import json
    r = json.loads(_tool("record_attempt", {"qid": "test-q-2", "score": 1,
                                            "marks_total": 2}, token))
    assert r["ok"] and r["duplicate"] is False


def test_auto_solve_on_repeat_full_marks(student, token):
    import json
    # 先把卡点写进画像
    _tool("save_memory", {"weak_topic": "羰基还原"}, token)
    # 同 qid 两次满分（topic 一致）→ 自动 solved
    for _ in range(2):
        _tool("record_attempt", {"qid": "test-q-3", "score": 3, "marks_total": 3,
                                 "topic_detail": "羰基还原"}, token)
    topics = {w["topic"]: w["status"]
              for w in json.loads(_tool("get_profile", {}, token))["memory"]["weak_topics"]}
    assert topics.get("羰基还原") == "solved"


# ---------------- 错题本 / 掌握度 / 报告 ----------------

def test_list_mistakes_and_mastery(student, token):
    import json
    m = json.loads(_tool("list_mistakes", {}, token))
    qids = {i["qid"] for i in m["items"]}
    assert "test-q-1" in qids          # 2/4 未满分 → 在错题本
    assert "test-q-3" not in qids      # 满分 → 自动移出（chemask 口径）
    mas = json.loads(_tool("get_mastery", {}, token))
    assert "carbonyl" in mas["by_topic"]


def test_report_flow(student, token):
    import json
    assert "暂无报告" in _tool("get_report", {}, token)
    db.add_report(student, "six_section", "# 报告\n①诊断…全文内容")
    p = json.loads(_tool("get_profile", {}, token))
    assert p["latest_report"]["full_len"] >= 10
    assert len(p["latest_report"]["summary"]) <= 210  # 只回 200 字摘要
    full = _tool("get_report", {}, token)
    assert "全文内容" in full


def test_clear_memory(student, token):
    _tool("save_memory", {"weak_topic": "待清空卡点"}, token)
    n = db.clear_student_memory(student)
    assert n >= 1
    m = db.get_student_memory(student)
    assert m is None  # 画像行已删，可重新建档


# ---------------- 权限与限流 ----------------

def test_class_summary_requires_teacher(student, token):
    r = _call("tools/call", token, {"name": "class_summary", "arguments": {}})
    assert r.status_code == 403  # pytest Token 无 teacher 标记


def test_class_summary_teacher_ok(student):
    # v1.3：教师权限走正式 role 列（不再看 label）
    raw = "ck_teach_" + secrets.token_urlsafe(12)
    db.issue_api_key(student, _token_hash(raw), label="pytest", role="teacher")
    try:
        r = _call("tools/call", None, {"name": "class_summary", "arguments": {}},
                  raw_token=raw)
        assert r.status_code == 200
        assert "class_mastery" in r.json()["result"]["content"][0]["text"]
    finally:
        db.revoke_api_key(_token_hash(raw))


def test_rate_limit(student):
    """修复 A 后：只有 tools/call 计入配额，tools/list 等保活流量免计费"""
    raw = "ck_rate_" + secrets.token_urlsafe(12)
    key_id = db.issue_api_key(student, _token_hash(raw), label="pytest-rate",
                              rate_limit_h=3)
    try:
        # 保活类请求（tools/list）不限流：打 10 次都应 200
        for _ in range(10):
            assert _call("tools/list", None, raw_token=raw).status_code == 200
        # 业务调用 tools/call：原子预计数语义——限额 3 时第 1-3 次（计数 1/2/3≤3）放行，
        # 第 4 次起（计数 4>3）429。本次调用已计数，429 不需补插
        codes = []
        for i in range(5):
            r = _call("tools/call", None,
                      {"name": "get_profile", "arguments": {}}, raw_token=raw)
            codes.append(r.status_code)
        assert codes[:3] == [200, 200, 200]
        assert codes[3] == 429 and codes[4] == 429
    finally:
        db.revoke_api_key(_token_hash(raw))


def test_keepalive_traffic_not_counted(student):
    """initialize/notifications/SSE GET 不消耗业务配额（教训：保活计费=自我拒绝服务）"""
    raw = "ck_ka_" + secrets.token_urlsafe(12)
    key_id = db.issue_api_key(student, _token_hash(raw), label="pytest-ka",
                              rate_limit_h=2)
    try:
        # 混合打保活流量：initialize + 通知 + tools/list 各若干次
        for _ in range(5):
            assert _call("initialize", None, raw_token=raw).status_code == 200
            r = client.post("/mcp", json={"jsonrpc": "2.0",
                                          "method": "notifications/initialized"},
                            headers={"Authorization": f"Bearer {raw}"})
            assert r.status_code == 202
            assert _call("tools/list", None, raw_token=raw).status_code == 200
        # 业务配额应原封不动：仍可成功调用限额内 2 次（原子预计数：第1/2次计数1/2≤2 放行）
        assert _call("tools/call", None,
                     {"name": "get_profile", "arguments": {}},
                     raw_token=raw).status_code == 200
        assert _call("tools/call", None,
                     {"name": "get_profile", "arguments": {}},
                     raw_token=raw).status_code == 200
        # 第 3 次计数=3 > 限额2 → 429
        assert _call("tools/call", None,
                     {"name": "get_profile", "arguments": {}},
                     raw_token=raw).status_code == 429
    finally:
        db.revoke_api_key(_token_hash(raw))


def test_malformed_body_400():
    r = client.post("/mcp", content=b"not-json",
                    headers={"Authorization": "Bearer x",
                             "Content-Type": "application/json"})
    assert r.status_code == 400


# ---------------- 迁移幂等 ----------------

def test_migration_idempotent():
    from ops import mcp_migrate
    mcp_migrate.run(apply=True)  # 重复执行不应报错


# ---------------- v1.3 评审修复（2026-10-30）----------------
# 针对外部评审的 P0 修复：原子幂等 / JSON 合法裁剪 / 乐观锁 / RBAC role

import threading

from gateway.mcp_gateway import _trim_to_limit, tool_record_attempt


def test_atomic_idempotency_concurrent(student):
    """并发同 idem_key：UNIQUE 约束保证只落一行（check→insert 有竞态窗口）"""
    results, barrier = [], threading.Barrier(6)
    idem = "v13-concurrent-" + uuid.uuid4().hex[:8]

    def worker():
        barrier.wait()
        aid, dup = db.add_attempt_idempotent(
            student, idem, "v13_q1", score=2, marks_total=4, source="mcp")
        results.append((aid, dup))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len({aid for aid, _ in results}) == 1, f"出现多行: {results}"
    assert sum(dup for _, dup in results) == 5  # 恰好 1 新 + 5 重
    # 清理
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM attempts WHERE student_id=%s AND idem_key=%s",
                        (student, idem))


def test_idem_key_in_dedicated_column(student):
    """idem_key 落独立列（不再塞 weak_points 混业务数据与控制字段）"""
    idem = "v13-col-" + uuid.uuid4().hex[:8]
    aid, dup = db.add_attempt_idempotent(student, idem, "v13_q2", score=1,
                                         marks_total=2, weak_points=["概念混淆"],
                                         source="mcp")
    assert not dup
    row = db.attempts_of(student, only_qbank=False)
    mine = [r for r in row if r["id"] == aid][0]
    assert "idem:" not in (mine["weak_points"] or "[]")  # weak_points 干净
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT idem_key FROM attempts WHERE id=%s", (aid,))
            assert cur.fetchone()["idem_key"] == idem
            cur.execute("DELETE FROM attempts WHERE id=%s", (aid,))


def test_get_profile_always_valid_json(student):
    """超长画像也不产生非法 JSON：结构化裁剪替代字符串截断（评审 P0 bug）"""
    huge = "长" * 5000
    db.upsert_student_memory(student, summary=huge,
                             weak_topics=[{"topic": huge[:500], "status": "open"}])
    out = _tool("get_profile", {}, None) if False else None
    # 直接走网关（带 token fixture 的学生）——本函数自建 token 以隔离
    raw = "ck_v13_" + secrets.token_urlsafe(12)
    db.issue_api_key(student, _token_hash(raw), label="v13")
    try:
        text = _tool("get_profile", {}, raw)
        import json as _json
        p = _json.loads(text)  # 不抛 = 合法 JSON
        assert len(text) <= 3000 + 200  # 允许少量开销
        assert p["student_id"] == student
    finally:
        db.revoke_api_key(_token_hash(raw))
        db.clear_student_memory(student)


def test_trim_to_limit_never_breaks_json():
    """纯函数级验证：任何输入都返回可解析 JSON；身份与 stale 必须保留"""
    import json as _json
    payload = {"student_id": 1,
               "memory": {"stale": True, "summary": "x" * 8000,
                          "taught_topics": [f"t{i}" for i in range(100)],
                          "weak_topics": [{"topic": "w" * 300, "status": "open"}]},
               "recent_dialogues": [{"role": "student", "digest": "d" * 400}] * 10,
               "latest_report": {"summary": "r" * 500}}
    text = _trim_to_limit(payload, 1500)
    p = _json.loads(text)
    # 身份与 stale 是最高优先级字段，任何裁剪计划都不应触及
    assert p["student_id"] == 1
    assert p["memory"]["stale"] is True


def test_optimistic_lock_on_memory(student):
    """版本冲突被拒绝：旧 version 写入抛 MemoryConflict，重读后成功"""
    db.upsert_student_memory(student, summary="base")
    ver1 = db.get_memory_version(student)
    # 模拟并发：另一写者先把版本推进
    db.upsert_student_memory(student, summary="other-writer")
    ver2 = db.get_memory_version(student)
    assert ver2 == ver1 + 1
    # 用过期版本写入 → 冲突
    with pytest.raises(db.MemoryConflict):
        db.upsert_student_memory(student, expected_version=ver1, summary="stale-write")
    # 重读新版本写入 → 成功
    ver3 = db.get_memory_version(student)
    db.upsert_student_memory(student, expected_version=ver3, summary="fresh-write")
    assert db.get_student_memory(student)["summary"] == "fresh-write"
    db.clear_student_memory(student)


def test_save_memory_survives_conflict_via_retry(student):
    """网关层：save_memory 遇冲突自动重试收敛（不向调用方抛 409）"""
    raw = "ck_v13m_" + secrets.token_urlsafe(12)
    db.issue_api_key(student, _token_hash(raw), label="v13m")
    try:
        db.upsert_student_memory(student, weak_topics=[{"topic": "A", "status": "open"}])
        # 在工具执行中途插队推进版本——模拟并发写者
        orig_upsert = db.upsert_student_memory
        calls = {"n": 0}

        def racing_upsert(student_id, expected_version=None, **fields):
            calls["n"] += 1
            if expected_version is not None and calls["n"] == 1:
                # 第一次尝试用旧版本，抢先把版本推进制造冲突
                db.upsert_student_memory(student_id, summary="racer")
            return orig_upsert(student_id, expected_version=expected_version, **fields)

        from gateway import mcp_gateway
        mcp_gateway.db.upsert_student_memory = racing_upsert
        try:
            out = _tool("save_memory",
                        {"weak_topic": "B", "session_id": "s", "digest": "d"}, raw)
            assert '"ok": true' in out.lower()
            assert calls["n"] >= 2  # 确实发生过重试
        finally:
            mcp_gateway.db.upsert_student_memory = orig_upsert
        # 两个 weak topic 都在（无 Lost Update）
        weak = db.get_student_memory(student)["weak_topics"]
        assert "A" in weak and "B" in weak
    finally:
        db.revoke_api_key(_token_hash(raw))
        db.clear_student_memory(student)


def test_class_summary_rbac_by_role(student, token):
    """RBAC：role 列为准，label 不再参与权限判断"""
    # 学生 token（role=student）→ 403
    r = _call("tools/call", token, {"name": "class_summary", "arguments": {}})
    assert r.status_code == 403
    # 教师 token（role=teacher，label 不含 teacher 字样）→ 200
    raw = "ck_v13t_" + secrets.token_urlsafe(12)
    db.issue_api_key(student, _token_hash(raw), label="principal-key", role="teacher")
    try:
        r2 = _call("tools/call", raw, {"name": "class_summary", "arguments": {}})
        assert r2.status_code == 200
    finally:
        db.revoke_api_key(_token_hash(raw))
    # label 含 teacher 但 role=student → 仍 403（证明 label 不再生效）
    raw2 = "ck_v13f_" + secrets.token_urlsafe(12)
    db.issue_api_key(student, _token_hash(raw2), label="teacher-assistant")
    try:
        r3 = _call("tools/call", raw2, {"name": "class_summary", "arguments": {}})
        assert r3.status_code == 403
    finally:
        db.revoke_api_key(_token_hash(raw2))


# ---------------- v1.4（ChatGPT 二轮评审修复）----------------

def test_save_report_idempotent(student, token):
    """报告持久化闭环：save_report 原子幂等，get_report 可读回"""
    key = "sess-1030-r1"
    out1 = _tool("save_report", {"report_type": "six_section",
                                 "content_md": "# 六段式报告\n正文", "report_key": key}, token)
    assert '"duplicate": false' in out1.lower()
    out2 = _tool("save_report", {"report_type": "six_section",
                                 "content_md": "# 六段式报告\n正文", "report_key": key}, token)
    assert '"duplicate": true' in out2.lower()
    # get_report 读回
    got = _tool("get_report", {}, token)
    assert "# 六段式报告" in got
    # 清理
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reports WHERE student_id=%s AND report_key=%s",
                        (student, key))


def test_save_report_rejects_missing_key(student, token):
    """缺 report_key → 400（参数校验，不让 Agent 重试产生重复报告）"""
    r = _call("tools/call", token,
              {"name": "save_report",
               "arguments": {"report_type": "six_section", "content_md": "x"}})
    assert r.status_code == 400


def test_rate_count_is_atomic_precount(student):
    """限流原子计数：insert-before-count，配额判定含本次调用"""
    # 直接验证 db 层语义：连续调用计数严格递增
    raw = "ck_v14_" + secrets.token_urlsafe(12)
    key_id = db.issue_api_key(student, _token_hash(raw), label="v14", rate_limit_h=3)
    try:
        c1 = db.rate_count_last_hour(key_id)
        c2 = db.rate_count_last_hour(key_id)
        c3 = db.rate_count_last_hour(key_id)
        assert c1 == 1 and c2 == 2 and c3 == 3
    finally:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM api_key_calls WHERE key_id=%s", (key_id,))
                cur.execute("DELETE FROM api_keys WHERE id=%s", (key_id,))


# ---------------- v1.5（三轮评审修复）----------------

def test_rate_limit_concurrent_serialized(student):
    """并发限流串行化（三轮评审要求）：限额 1 时，N 个并发请求只放行 1 个。
    行锁 FOR UPDATE 使同一 Token 的配额判定串行执行，
    REPEATABLE READ 下未提交计数不可见的问题被锁排队消除。"""
    raw = "ck_v15_" + secrets.token_urlsafe(12)
    key_id = db.issue_api_key(student, _token_hash(raw), label="v15",
                              rate_limit_h=1)
    try:
        results, barrier = [], threading.Barrier(6)

        def worker():
            barrier.wait()
            r = _call("tools/call", None,
                      {"name": "get_profile", "arguments": {}}, raw_token=raw)
            results.append(r.status_code)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert results.count(200) == 1, f"放行了 {results.count(200)} 次: {results}"
        assert results.count(429) == 5
    finally:
        db.revoke_api_key(_token_hash(raw))
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM api_key_calls WHERE key_id=%s", (key_id,))


def test_first_profile_with_weak_topics_not_lost(student, token):
    """新学生首次 update_profile 带 weak_topics：不能因无 version 走无锁路径而丢失"""
    raw = "ck_v15f_" + secrets.token_urlsafe(12)
    db.issue_api_key(student, _token_hash(raw), label="v15f")
    try:
        db.clear_student_memory(student)
        _tool("update_profile",
              {"exam_board": "9701/22", "target_grade": "a",
               "weak_topics": ["有机化学", "物理化学"]}, raw)
        mem = db.get_student_memory(student)
        weak = db._load_json_helper(mem["weak_topics"]) if hasattr(
            db, "_load_json_helper") else __import__("json").loads(mem["weak_topics"])
        topics = {w["topic"] for w in weak}
        assert {"有机化学", "物理化学"} <= topics, f"首次建档丢卡点: {weak}"
    finally:
        db.revoke_api_key(_token_hash(raw))
        db.clear_student_memory(student)


def test_memory_version_single_read_consistency(student):
    """Memory 内容与 version 同源：mem['version'] 即条件更新依据（三轮评审时序问题）"""
    db.upsert_student_memory(student, summary="s1")
    mem = db.get_student_memory(student)
    # SELECT * 必须包含 version 列（同源读取的前提）
    assert "version" in mem
    ver = mem["version"]
    db.upsert_student_memory(student, expected_version=ver, summary="s2")
    assert db.get_student_memory(student)["version"] == ver + 1
    db.clear_student_memory(student)


def test_trim_never_exceeds_hard_limit():
    """任意恶意输入（巨型 preferences/超长字段）：输出 ≤3000 且 JSON 合法，
    student_id 与 stale 保留（三轮评审：JSON 合法 ≠ 长度受控）"""
    import json as _json
    payload = {"student_id": 42,
               "memory": {"stale": False,
                          "preferences": {"huge": "P" * 9000, "x": list(range(500))},
                          "summary": "s" * 9000,
                          "exam_board": "9" * 500,
                          "plan_stage": "p" * 500,
                          "weak_topics": [{"topic": "w" * 2000, "status": "open"}] * 50,
                          "taught_topics": ["t" * 1000] * 100},
               "recent_dialogues": [{"role": "student", "digest": "d" * 900}] * 20,
               "latest_report": {"summary": "r" * 9000}}
    text = _trim_to_limit(payload, 3000)
    p = _json.loads(text)
    assert len(text) <= 3000, f"超出硬限制: {len(text)}"
    assert p["student_id"] == 42
    assert "stale" in p["memory"]


def test_first_creation_concurrent_no_overwrite(student):
    """并发首建档：两个请求同时对无 Memory 行的学生 save_memory，
    走原子建行+条件更新，两个 weak_topic 都必须保留（五轮评审：
    ver=None 无版本路径在并发首写下互相覆盖）"""
    import threading as _th
    raws = []
    for i in range(2):
        raw = "ck_v17c_" + secrets.token_urlsafe(12)
        db.issue_api_key(student, _token_hash(raw), label=f"v17c{i}")
        raws.append(raw)
    barrier = _th.Barrier(2)
    errors = []

    def worker(raw, topic):
        try:
            barrier.wait(timeout=10)
            out = _tool("save_memory",
                        {"weak_topic": topic, "session_id": "s", "digest": "d"}, raw)
            if '"ok": true' not in out.lower():
                errors.append(f"{topic}: {out[:200]}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{topic}: {e!r}")

    t1 = _th.Thread(target=worker, args=(raws[0], "并发卡点甲"))
    t2 = _th.Thread(target=worker, args=(raws[1], "并发卡点乙"))
    t1.start(); t2.start(); t1.join(15); t2.join(15)
    try:
        assert not errors, f"并发首建档报错: {errors}"
        mem = db.get_student_memory(student)
        import json as _json
        weak = _json.loads(mem["weak_topics"]) if mem and mem.get("weak_topics") else []
        topics = {w["topic"] for w in weak}
        assert {"并发卡点甲", "并发卡点乙"} <= topics, f"并发首写覆盖: {topics}"
    finally:
        for raw in raws:
            db.revoke_api_key(_token_hash(raw))
        db.clear_student_memory(student)
