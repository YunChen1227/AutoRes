"""API 路由（design.md §8）：/api/chat (SSE)、/api/download、/api/health、/。"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from autores.common.logging import get_logger
from autores.server.agent.loop import Agent

log = get_logger("api")
router = APIRouter()

_FRONTEND = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                         "frontend", "index.html")


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


@router.get("/")
def index():
    return FileResponse(_FRONTEND, media_type="text/html")


@router.get("/api/health")
def health(request: Request):
    app = request.app.state
    status = {"status": "ok"}
    # DB 连通性
    try:
        app.db.command("ping")
        status["db"] = "ok"
    except Exception as e:  # noqa: BLE001
        status["db"] = f"error: {e}"
        status["status"] = "degraded"
    return JSONResponse(status)


@router.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    session_id = body.get("session_id", "")
    message = (body.get("message") or "").strip()

    st = request.app.state
    if not session_id or not message:
        return JSONResponse({"error": "session_id 与 message 必填"}, status_code=400)

    agent: Agent = st.agent
    sess = st.sessions.get_or_create(session_id, agent.system_message())
    sess.messages.append({"role": "user", "content": message})

    def event_stream():
        try:
            for event in agent.run_turn(sess.messages):
                yield _sse(event)
        except Exception as e:  # noqa: BLE001
            log.error("chat 处理异常", extra={"fields": {"error": str(e)}})
            yield _sse({"type": "error", "text": "服务器内部错误，请稍后重试。"})
        finally:
            st.sessions.trim(sess)
            yield _sse({"type": "done"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/api/download/{token}")
def download(token: str, request: Request):
    st = request.app.state
    path = st.reports.resolve(token)
    if path is None or not os.path.exists(path):
        return JSONResponse({"error": "报告已过期或不存在，请重新生成。"}, status_code=410)
    filename = os.path.basename(path)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
