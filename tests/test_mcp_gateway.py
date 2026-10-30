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

from app import db
from app.mcp_gateway import app, _merge_weak_topics, _token_hash

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
    raw = "ck_teach_" + secrets.token_urlsafe(12)
    db.issue_api_key(student, _token_hash(raw), label="teacher-pytest")
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
        # 业务调用 tools/call：前 3 次内 200，第 4 次起 429
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
        # 业务配额应原封不动：仍可成功调用 2 次 tools/call
        assert _call("tools/call", None,
                     {"name": "get_profile", "arguments": {}},
                     raw_token=raw).status_code == 200
        assert _call("tools/call", None,
                     {"name": "get_profile", "arguments": {}},
                     raw_token=raw).status_code == 200
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
    from prep import mcp_migrate
    mcp_migrate.run(apply=True)  # 重复执行不应报错
