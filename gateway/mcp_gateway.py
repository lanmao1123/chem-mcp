"""MCP 网关：把 chemask 数据层暴露为 7 个白名单 MCP 工具（v1.2 方案 §8.4）。

链路：WorkBuddy 专家 → MCP 连接器（HTTPS + Bearer Token）→ 本网关 → app.db → MySQL
安全设计：
  - Token 只存 SHA-256 哈希（api_keys.token_hash），明文仅签发时展示一次
  - 每次调用：鉴权 → 吊销检查 → 限流（rate_limit_h 次/小时）→ 记 last_used_at
  - student_id 一律由 Token 反查注入，不信任工具参数中的任何身份字段
  - 只暴露白名单工具，不透传任意 SQL
  - MCP 超时/异常统一返回结构化错误，专家侧提示词有降级话术

运行（本地测试）：
    ./.venv/bin/python -m app.mcp_gateway --port 8765
生产（服务器）：
    systemd + nginx /chem-mcp/ 反代（部署需另行确认）
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import db

log = logging.getLogger("mcp_gateway")

# ---------------------------------------------------------------- 工具描述

TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "name": "get_profile",
        "description": ("会话开始必须首先调用：获取当前学生身份、画像摘要、最近对话与最近报告。"
                        "返回即真相；若 stale=true 表示记忆超过 7 天未更新，应主动向学生确认近况。"),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "update_profile",
        "description": "新学生首访建档或修正画像字段（仅传需要设置的字段）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "exam_board": {"type": "string", "description": "考季卷型，如 9701/22"},
                "target_grade": {"type": "string", "description": "目标等级 a/b/c"},
                "current_score": {"type": "string", "description": "当前分数段"},
                "weak_topics": {"type": "array", "items": {"type": "string"},
                                 "description": "卡点列表（合并语义见 save_memory）"},
                "preferences": {"type": "object", "description": "教学偏好"},
                "summary": {"type": "string", "description": "画像自由摘要（≤2000 字符）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "save_memory",
        "description": ("保存记忆增量：会话中出现新的长期有效信息即时调用；会话结束固化调用。"
                        "合并规则由网关处理：weak_topics 并集、plan_stage 覆盖、taught_topics 并集。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "weak_topic": {"type": "string", "description": "新增/发现的卡点"},
                "weak_topic_solved": {"type": "string", "description": "已解决的卡点（标记 solved）"},
                "taught_topic": {"type": "string", "description": "本次讲过的知识点（防重复讲解）"},
                "plan_stage": {"type": "string", "description": "当前计划阶段（覆盖）"},
                "session_id": {"type": "string", "description": "当前会话标识"},
                "digest": {"type": "string", "description": "本轮对话摘要句（≤500 字符，非原文）"},
                "summary_patch": {"type": "string", "description": "画像摘要增量（拼接合并）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "record_attempt",
        "description": ("学生作答后记录（题目/得分/错因/耗时）。幂等：同 idem_key 重复提交只记一次。"
                        "student_id 由网关从 Token 注入。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "qid": {"type": "string"},
                "paper": {"type": "string"},
                "question_no": {"type": "integer"},
                "score": {"type": "integer"},
                "marks_total": {"type": "integer"},
                "error_types": {"type": "array", "items": {"type": "string"}},
                "weak_points": {"type": "array", "items": {"type": "string"}},
                "topic_detail": {"type": "string"},
                "time_spent_s": {"type": "integer", "description": "耗时秒（存入 weak_points 元信息）"},
                "idem_key": {"type": "string", "description": "幂等键：session_id+轮次号"},
            },
            "required": ["qid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_mistakes",
        "description": "查当前学生错题本（复用 chemask 口径：重练满分自动移出）。",
        "inputSchema": {
            "type": "object",
            "properties": {"topic": {"type": "string", "description": "可选主题过滤"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_mastery",
        "description": "查当前学生按主题掌握度与错因分布。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "class_summary",
        "description": "（仅教师 Token）班级掌握度汇总。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

_PROFILE_LIMIT = 3000          # get_profile 总返回量上限（字符）
_REPORT_SUMMARY_CHARS = 200
_STALE_DAYS = 7
TEACHER_LABEL = "teacher"


# ---------------------------------------------------------------- 错误响应

class GatewayError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status


def _err(code: str, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


# ---------------------------------------------------------------- 鉴权/限流

def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise GatewayError("unauthorized", "缺少 Bearer Token", 401)
    raw = auth[7:].strip()
    if not raw or len(raw) > 256 or any(c in raw for c in "\r\n\x00"):
        raise GatewayError("unauthorized", "Token 格式非法", 401)
    return raw


def authenticate(request: Request) -> dict:
    """Token 鉴权（不限流）。限流只在业务动作 tools/call 时计——
    教训：连接器保活/轮询流量若计入配额，会挤掉真正的业务调用（自我拒绝服务）。"""
    raw = _extract_bearer(request)
    row = db.lookup_api_key(_token_hash(raw))
    if not row:
        raise GatewayError("unauthorized", "Token 无效或已吊销", 401)
    db.touch_api_key(row["id"])
    return row


def enforce_rate_limit(key_row: dict):
    """业务动作限流（仅 tools/call 调用）。429 时附带 Retry-After 语义。"""
    if db.rate_count_last_hour(key_row["id"]) >= key_row["rate_limit_h"]:
        raise GatewayError("rate_limited", "业务调用超过每小时限额，请稍后再试", 429)


# ---------------------------------------------------------------- 合并逻辑

def _load_json(v) -> list:
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return v
    try:
        parsed = json.loads(v)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _merge_weak_topics(current: list, new_topic: str | None,
                       solved_topic: str | None) -> list:
    """weak_topics 合并：新卡点追加 open；已解决的按名匹配置 solved（并集，不重复）。
    返回全新对象（不共享输入 dict 引用），保证调用方可用 == 比较变化。"""
    items = {it["topic"]: dict(it) for it in current
             if isinstance(it, dict) and "topic" in it}
    if new_topic:
        items.setdefault(new_topic.strip(), {"topic": new_topic.strip(), "status": "open"})
    if solved_topic:
        t = solved_topic.strip()
        if t in items:
            items[t]["status"] = "solved"
        else:
            items[t] = {"topic": t, "status": "solved"}
    order = [it["topic"] for it in current if isinstance(it, dict) and "topic" in it]
    for t in [new_topic, solved_topic]:
        if t and t.strip() not in order:
            order.append(t.strip())
    return [items[t] for t in order if t in items]


# ---------------------------------------------------------------- 工具实现

def tool_get_profile(student_id: int) -> dict:
    mem = db.get_student_memory(student_id) or {}
    tries = db.recent_dialogues(student_id, limit=3)
    rpt = db.latest_report_meta(student_id, summary_chars=_REPORT_SUMMARY_CHARS)
    # 无画像行 → 视为陈旧（提示词据此主动向学生确认近况/触发建档）
    updated = mem.get("updated_at") if mem else None
    if updated is None:
        stale = True
    else:
        stale = (time.time() - _to_epoch(updated)) > _STALE_DAYS * 86400
    payload = {
        "student_id": student_id,
        "memory": {
            "exam_board": mem.get("exam_board"),
            "target_grade": mem.get("target_grade"),
            "current_score": mem.get("current_score"),
            "weak_topics": _load_json(mem.get("weak_topics")),
            "taught_topics": _load_json(mem.get("taught_topics")),
            "plan_stage": mem.get("plan_stage"),
            "preferences": mem.get("preferences"),
            "summary": mem.get("summary"),
            "stale": stale,
        },
        "recent_dialogues": [
            {"role": d["role"], "digest": d["digest"]} for d in tries
        ],
        "latest_report": ({
            "id": rpt["id"], "report_type": rpt["report_type"],
            "created_at": str(rpt["created_at"]),
            "summary": rpt["summary"], "full_len": rpt["full_len"],
            "hint": "全文用 get_report 拉取",
        } if rpt else None),
    }
    blob = json.dumps(payload, ensure_ascii=False)
    if len(blob) > _PROFILE_LIMIT:  # 防御性截断：摘要字段优先保全
        payload["memory"]["summary"] = (payload["memory"]["summary"] or "")[:1000]
        blob = json.dumps(payload, ensure_ascii=False)
    return {"content": [{"type": "text", "text": blob[:_PROFILE_LIMIT]}]}


def _to_epoch(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    import datetime
    if isinstance(v, datetime.datetime):
        return v.timestamp()
    return 0.0


def tool_update_profile(student_id: int, args: dict) -> dict:
    fields = {k: args[k] for k in
              ("exam_board", "target_grade", "current_score", "summary", "preferences")
              if k in args}
    weak = _load_json((db.get_student_memory(student_id) or {}).get("weak_topics"))
    for t in args.get("weak_topics") or []:
        weak = _merge_weak_topics(weak, t, None)
    if args.get("weak_topics") is not None:
        fields["weak_topics"] = weak
    db.upsert_student_memory(student_id, **fields)
    return {"content": [{"type": "text", "text": json.dumps(
        {"ok": True, "student_id": student_id}, ensure_ascii=False)}]}


def tool_save_memory(student_id: int, args: dict) -> dict:
    mem = db.get_student_memory(student_id) or {}
    weak = _load_json(mem.get("weak_topics"))
    taught = _load_json(mem.get("taught_topics"))
    patch: dict = {}
    if args.get("weak_topic"):
        weak = _merge_weak_topics(weak, args["weak_topic"], None)
        patch["weak_topics"] = weak
    if args.get("weak_topic_solved"):
        weak = _merge_weak_topics(weak, None, args["weak_topic_solved"])
        patch["weak_topics"] = weak
    if args.get("taught_topic"):
        t = args["taught_topic"].strip()
        if t not in taught:
            taught.append(t)
        patch["taught_topics"] = taught
    if args.get("plan_stage"):
        patch["plan_stage"] = args["plan_stage"]
    if args.get("summary_patch"):
        base = (mem.get("summary") or "").strip()
        merged = (base + "\n" + args["summary_patch"].strip()).strip()
        patch["summary"] = merged[-2000:]  # 超限保留尾部（近因优先）
    if patch:
        db.upsert_student_memory(student_id, **patch)
    if args.get("session_id") and args.get("digest"):
        db.add_dialogue(student_id, args["session_id"], "student", args["digest"])
    return {"content": [{"type": "text", "text": json.dumps(
        {"ok": True, "merged_fields": list(patch.keys())}, ensure_ascii=False)}]}


def tool_record_attempt(student_id: int, args: dict) -> dict:
    idem = args.get("idem_key") or ""
    qid = args["qid"]
    if idem:
        dup = db.find_attempt_by_idem(student_id, qid, idem)
        if dup:
            return {"content": [{"type": "text", "text": json.dumps(
                {"ok": True, "duplicate": True, "attempt_id": dup}, ensure_ascii=False)}]}
    weak_points = args.get("weak_points") or []
    if args.get("time_spent_s"):
        weak_points = list(weak_points) + [f"time:{args['time_spent_s']}s"]
    # 幂等落库：idem_key 编码进 weak_points 元信息（schema 不动，语义扩展）
    if idem:
        weak_points = list(weak_points) + [f"idem:{idem}"]
    attempt_id = db.add_attempt(
        student_id, qid, paper=args.get("paper"),
        question_no=args.get("question_no"), score=args.get("score"),
        marks_total=args.get("marks_total"),
        error_types=args.get("error_types"), weak_points=weak_points,
        topic_detail=args.get("topic_detail"), source="mcp")
    # 连续两次同 qid 满分 → weak_topics 标记 solved（§8.5 规则）
    _auto_solve_on_repeat_success(student_id, qid)
    return {"content": [{"type": "text", "text": json.dumps(
        {"ok": True, "attempt_id": attempt_id, "duplicate": False}, ensure_ascii=False)}]}


def _auto_solve_on_repeat_success(student_id: int, qid: str):
    rows = db.attempts_of(student_id)
    same = [r for r in rows if r["qid"] == qid and r["score"] is not None
            and r["marks_total"] and r["score"] >= r["marks_total"]]
    if len(same) < 2:
        return
    topic = next((r.get("topic_detail") for r in reversed(rows)
                  if r["qid"] == qid and r.get("topic_detail")), None)
    if not topic:
        return
    mem = db.get_student_memory(student_id) or {}
    weak = _load_json(mem.get("weak_topics"))
    merged = _merge_weak_topics(weak, None, topic)
    if merged != weak:
        db.upsert_student_memory(student_id, weak_topics=merged)


def tool_list_mistakes(student_id: int, args: dict) -> dict:
    groups = db.mistake_groups(student_id)
    topic = args.get("topic")
    if topic:
        groups = [g for g in groups if g.get("topic_detail") == topic]
    slim = [{"qid": g["qid"], "paper": g["paper"], "no": g["question_no"],
             "topic": g["topic_detail"], "attempts": g["attempts_count"],
             "last": f"{g['last_score']}/{g['last_marks']}"}
            for g in groups[:20]]
    return {"content": [{"type": "text", "text": json.dumps(
        {"count": len(groups), "items": slim}, ensure_ascii=False)}]}


def tool_get_mastery(student_id: int) -> dict:
    by_topic, err_dist = db.mastery_by_topic(student_id)
    return {"content": [{"type": "text", "text": json.dumps(
        {"by_topic": by_topic, "error_distribution": err_dist}, ensure_ascii=False)}]}


def tool_class_summary(key_row: dict) -> dict:
    label = (key_row.get("label") or "")
    if TEACHER_LABEL not in label.lower():
        raise GatewayError("forbidden", "class_summary 仅教师 Token 可用", 403)
    rows = db.class_mastery()
    return {"content": [{"type": "text", "text": json.dumps(
        {"class_mastery": rows}, ensure_ascii=False)}]}


# ---------------------------------------------------------------- 路由

app = FastAPI(title="chemask MCP gateway", docs_url=None, redoc_url=None)

# get_report 是第 8 个辅助工具（方案定稿 7 个；get_report 属 get_profile 全文按需拉取通道）
TOOLS_SPEC.append({
    "name": "get_report",
    "description": "按需拉取最近一份学情报告全文（Markdown）。",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
})


def tool_get_report(student_id: int) -> dict:
    mem = db.get_student_memory  # noqa: F841  (占位避免误删)
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT report_type, content_md, created_at FROM reports"
                " WHERE student_id=%s ORDER BY id DESC LIMIT 1", (student_id,))
            r = cur.fetchone()
    if not r:
        return {"content": [{"type": "text", "text": "暂无报告"}]}
    return {"content": [{"type": "text", "text": r["content_md"]}]}


_TOOL_IMPL = {
    "get_profile": lambda sid, args, key: tool_get_profile(sid),
    "update_profile": lambda sid, args, key: tool_update_profile(sid, args),
    "save_memory": lambda sid, args, key: tool_save_memory(sid, args),
    "record_attempt": lambda sid, args, key: tool_record_attempt(sid, args),
    "list_mistakes": lambda sid, args, key: tool_list_mistakes(sid, args),
    "get_mastery": lambda sid, args, key: tool_get_mastery(sid),
    "class_summary": lambda sid, args, key: tool_class_summary(key),
    "get_report": lambda sid, args, key: tool_get_report(sid),
}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/mcp")
async def mcp_get_endpoint(request: Request):
    """Streamable HTTP GET：客户端 SSE 监听通道（修复 B：长流 + 心跳）。
    保持连接打开并每 10s 发注释心跳，避免客户端 3s 重连风暴打满日志与配额；
    客户端断开时生成器被取消，连接自动回收。"""
    try:
        authenticate(request)
    except GatewayError as e:
        return _err(e.code, e.message, e.status)
    if "text/event-stream" not in request.headers.get("accept", ""):
        return _err("bad_request", "SSE not accepted", 406)

    from fastapi.responses import StreamingResponse

    async def event_gen():
        yield ": stream open\n\n"
        # 长连接心跳；nginx proxy_read_timeout 120s < 10s 间隔不会断
        while True:
            await asyncio.sleep(10)
            yield ": ka\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """极简 MCP-over-HTTP：JSON-RPC 风格（initialize / tools/list / tools/call）"""
    try:
        body = await request.json()
    except ValueError:
        return _err("bad_request", "body 不是合法 JSON", 400)

    method = body.get("method", "")
    rpc_id = body.get("id")

    # JSON-RPC 通知（如 notifications/initialized，无 id 字段）：按规范回 202 空体
    if "id" not in body:
        from fastapi.responses import Response
        return Response(status_code=202)

    if method == "initialize":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "chemask-mcp", "version": "1.2.0"},
        }})

    # 其余方法需要鉴权
    try:
        key_row = authenticate(request)
    except GatewayError as e:
        return _err(e.code, e.message, e.status)

    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                             "result": {"tools": TOOLS_SPEC}})

    if method == "tools/call":
        params = body.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        impl = _TOOL_IMPL.get(name)
        if not impl:
            return _err("unknown_tool", f"工具 {name} 不在白名单", 404)
        try:
            enforce_rate_limit(key_row)  # 只对业务动作计费（修复 A）
            result = impl(key_row["student_id"], args, key_row)
            return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})
        except GatewayError as e:
            return _err(e.code, e.message, e.status)
        except Exception:  # noqa: BLE001 不向调用方泄漏内部堆栈
            log.exception("tool %s failed", name)
            return _err("internal", "数据服务暂时不可用，本次可按新对话继续", 500)

    return _err("bad_request", f"不支持的方法 {method}", 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    ns = parser.parse_args()
    import uvicorn
    uvicorn.run(app, host=ns.host, port=ns.port, log_level="info")


if __name__ == "__main__":
    main()
