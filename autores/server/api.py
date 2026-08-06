"""API 路由（design.md §8）：/api/chat (SSE)、/api/download、/api/health、/。"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from autores.common.logging import get_logger
from autores.db.client import DuplicateRunError
from autores.server.agent.loop import Agent
from autores.server.ingest import launch_params, upload as upload_mod
from autores.server.ingest.upload import UploadError

log = get_logger("api")
router = APIRouter()

_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                             "frontend")
_FRONTEND = os.path.join(_FRONTEND_DIR, "index.html")
_UPLOAD_PAGE = os.path.join(_FRONTEND_DIR, "upload.html")


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
        app.db.ping()
        status["db"] = "ok"
    except Exception as e:  # noqa: BLE001
        status["db"] = f"error: {e}"
        status["status"] = "degraded"
    return JSONResponse(status)


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


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
        yield ": connected\n\n"
        try:
            for event in agent.run_turn(sess.messages):
                yield _sse(event)
        except Exception as e:  # noqa: BLE001
            log.error("chat 处理异常", extra={"fields": {"error": str(e)}})
            yield _sse({"type": "error", "text": "服务器内部错误，请稍后重试。"})
        finally:
            st.sessions.trim(sess)
            yield _sse({"type": "done"})

    return StreamingResponse(
        event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS,
    )


@router.get("/upload")
def upload_page():
    """手工上传子页面（CSV + 启动命令文本）。"""
    return FileResponse(_UPLOAD_PAGE, media_type="text/html")


@router.get("/api/upload/options")
def upload_options():
    """上传表单可选项（框架 / 显卡由后端规则表提供，避免前后端各写一份）。"""
    return JSONResponse({
        "frameworks": launch_params.supported_frameworks(),
        "gpu_types": upload_mod.supported_gpu_types(),
    })


@router.post("/api/upload")
async def upload_run(
    request: Request,
    csv_file: UploadFile = File(...),
    launch_cmd: str = Form(...),
    framework: str = Form(...),
    framework_version: str = Form(...),
    model: str = Form(...),
    gpu_type: str = Form(...),
    model_version: str = Form(""),
):
    """手工上传一次测试结果入库。启动命令通过 launch_cmd 文本字段提交。"""
    st = request.app.state
    meta = {
        "framework": framework,
        "framework_version": framework_version,
        "model": model,
        "model_version": model_version,
        "gpu_type": gpu_type,
    }
    try:
        csv_bytes = await csv_file.read()
        summary = upload_mod.ingest(st.db, meta, csv_bytes, launch_cmd)
    except UploadError as e:
        log.info("上传校验失败", extra={"fields": {"error": str(e)}})
        return JSONResponse({"error": str(e)}, status_code=400)
    except DuplicateRunError:
        # run_id 由服务器时间生成并已查重，正常不会走到；并发同秒提交时兜底
        log.warning("上传 run_id 冲突")
        return JSONResponse({"error": "记录已存在，请稍后重试。"}, status_code=409)
    except Exception as e:  # noqa: BLE001
        log.error("上传入库异常", extra={"fields": {"error": str(e)}})
        return JSONResponse({"error": "服务器内部错误，请稍后重试。"}, status_code=500)

    log.info("上传入库成功", extra={"fields": {
        "run_id": summary["run_id"], "metrics": summary["num_metrics"]}})
    return JSONResponse(summary)


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
