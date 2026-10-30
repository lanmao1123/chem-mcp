"""D1-0a 平台能力验证用：最小 echo MCP 服务器（零业务依赖）。

用途：验证 WorkBuddy 能否连接自定义 HTTP MCP 并调用工具（30 分钟生死验证）。
1) 本地运行：  python3 prep/mcp_echo_server.py --port 8901
2) 内网穿透（二选一，验证时可临时用）：
   - ssh -R 披露到 lobster，再 nginx 反代出公网
   - 或用 cloudflared/ngrok 临时隧道
3) WorkBuddy → 连接器 → 自定义连接器 → 添加 MCP：
   URL: https://<公网地址>/mcp   Header: Authorization: Bearer echo-test-2026
4) 对话里召唤任意专家让它调用 echo 工具，观察是否返回。

传输：POST JSON-RPC（application/json）+ GET SSE 通道（Streamable HTTP 规范）。
工具集故意做成 echo + now 两个，无任何数据库依赖。
"""
import argparse
import asyncio

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(docs_url=None, redoc_url=None)
AUTH_TOKEN = "echo-test-2026"

TOOLS = [
    {"name": "echo", "description": "原样返回 text 字段，用于验证连通性",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"], "additionalProperties": False}},
    {"name": "now", "description": "返回服务器当前时间（ISO）",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


@app.get("/health")
def health():
    return {"ok": True, "server": "echo-mcp"}


@app.get("/mcp")
async def mcp_get(request: Request):
    """Streamable HTTP GET：客户端建立 SSE 监听通道。
    服务器不主动推送服务器请求（无 sampling 等特性），按协议返回 200 + 立即关闭的
    text/event-stream（含 endpoint 事件），避免 405 被客户端当作传输失败。"""
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {AUTH_TOKEN}":
        return _err("unauthorized", 401)
    if "text/event-stream" not in request.headers.get("accept", ""):
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32000,
                                       "message": "SSE not accepted"}},
                            status_code=406)

    async def event_gen():
        # 立即结束的合法 SSE 流：发一个注释心跳再关，客户端视为无服务器推送
        yield ": stream open\n\n"
        # 保持短暂连接后正常结束；客户端会按需重连
        await asyncio.sleep(0.2)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "Connection": "keep-alive"})


@app.post("/mcp")
async def mcp(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return _err("bad json")
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {AUTH_TOKEN}":
        return _err("unauthorized", 401)

    method = body.get("method", "")
    rpc_id = body.get("id")

    # JSON-RPC 通知（无 id）：按规范返回 202 无响应体，绝不能 400
    if "id" not in body:
        return Response(status_code=202)

    if method == "initialize":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "echo-mcp", "version": "0.1.0"}}})
    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                             "result": {"tools": TOOLS}})
    if method == "tools/call":
        name = (body.get("params") or {}).get("name")
        args = (body.get("params") or {}).get("arguments") or {}
        if name == "echo":
            text = f"ECHO: {args.get('text', '')}"
        elif name == "now":
            import datetime
            text = datetime.datetime.now().isoformat()
        else:
            return _err(f"unknown tool {name}", 404)
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {
            "content": [{"type": "text", "text": text}]}})
    return _err(f"unsupported method {method}")


if __name__ == "__main__":
    import uvicorn
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8901)
    ns = p.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=ns.port, log_level="info")
