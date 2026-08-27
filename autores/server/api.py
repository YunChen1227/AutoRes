"""API 路由（design.md §8）：/api/chat (SSE)、/api/download、/api/health、/。"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from autores.common.logging import get_logger
from autores.db.client import DuplicateRunError
from autores.server import gpu_types as gpu_types_mod
from autores.server import runs as runs_mod
from autores.server.agent.loop import Agent
from autores.server.gpu_types import GpuTypeError
from autores.server.ingest import launch_params, upload as upload_mod
from autores.server.ingest.upload import UploadError
from autores.server.runs import RunDeleteError

log = get_logger("api")
router = APIRouter()

_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                             "frontend")
_FRONTEND = os.path.join(_FRONTEND_DIR, "index.html")
_UPLOAD_PAGE = os.path.join(_FRONTEND_DIR, "upload.html")
_UPLOAD_VLM_PAGE = os.path.join(_FRONTEND_DIR, "upload_vlm.html")
_GPUS_PAGE = os.path.join(_FRONTEND_DIR, "gpus.html")
_RUNS_PAGE = os.path.join(_FRONTEND_DIR, "runs.html")

# 上传页 HTML 常改；禁止浏览器/CDN 缓存旧版（/upload 比 /upload/vlm 更早访问，容易 stale）。
_HTML_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


def _html_page(path: str) -> FileResponse:
    return FileResponse(path, media_type="text/html", headers=_HTML_NO_CACHE)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


@router.get("/")
def index():
    return _html_page(_FRONTEND)


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
    """手工上传子页面（文本压测 CSV + 启动命令文本）。"""
    return _html_page(_UPLOAD_PAGE)


@router.get("/upload/vlm")
def upload_vlm_page():
    """VLM 多模态压测手工上传子页面。"""
    return _html_page(_UPLOAD_VLM_PAGE)


@router.get("/gpus")
def gpus_page():
    """显卡型号管理子页面（增删改查 tools/gpu_types.json）。"""
    return _html_page(_GPUS_PAGE)


@router.get("/runs")
def runs_page():
    """测试记录管理子页面（仅可删页面上传的记录）。"""
    return _html_page(_RUNS_PAGE)


@router.get("/api/runs")
def runs_list(request: Request, kind: str = "text", limit: int = 200, q: str = ""):
    """列出测试记录摘要（供 /runs 管理页）。"""
    try:
        st = request.app.state
        items = runs_mod.list_briefs(
            st.db,
            kind=kind,
            limit=limit,
            keyword=q or None,
            benchmark_root=st.config.scanner.benchmark_root,
        )
        return JSONResponse({
            "runs": items,
            "kinds": ["text", "vlm"],
        })
    except (ValueError, RunDeleteError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        log.error("列出测试记录失败", extra={"fields": {"error": str(e)}})
        return JSONResponse({"error": "服务器内部错误，请稍后重试。"}, status_code=500)


@router.get("/api/runs/{run_id}/delete-preview")
def runs_delete_preview(run_id: str, request: Request, kind: str | None = None):
    """删除预览：跑完护栏但不删，供前端二次确认。"""
    st = request.app.state
    try:
        result = runs_mod.preview_delete(
            st.db,
            run_id,
            kind=kind,
            benchmark_root=st.config.scanner.benchmark_root,
            dir_pattern=st.config.scanner.dir_pattern,
        )
        return JSONResponse(result)
    except RunDeleteError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        log.error("删除预览失败", extra={"fields": {"run_id": run_id, "error": str(e)}})
        return JSONResponse({"error": "服务器内部错误，请稍后重试。"}, status_code=500)


@router.delete("/api/runs/{run_id}")
def runs_delete(run_id: str, request: Request, kind: str | None = None):
    """删除一条页面上传的测试记录（目录 + 表行 + ingest_log）。MCP 不提供。"""
    st = request.app.state
    try:
        result = runs_mod.delete_run(
            st.db,
            run_id,
            kind=kind,
            benchmark_root=st.config.scanner.benchmark_root,
            dir_pattern=st.config.scanner.dir_pattern,
        )
        log.info("删除测试记录", extra={"fields": {
            "run_id": result.get("run_id"),
            "source_dir": result.get("source_dir"),
            "kind": result.get("benchmark_kind"),
            "removed_dir": result.get("removed_dir"),
            "removed_row": result.get("removed_row"),
            "removed_log": result.get("removed_log"),
        }})
        return JSONResponse(result)
    except RunDeleteError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        log.error("删除测试记录失败", extra={"fields": {"run_id": run_id, "error": str(e)}})
        return JSONResponse({"error": "服务器内部错误，请稍后重试。"}, status_code=500)


@router.get("/api/gpu-types")
def gpu_types_list(request: Request):
    """列出全部显卡型号（含库内引用数 in_use）。"""
    try:
        items = gpu_types_mod.list_types(request.app.state.db)
        return JSONResponse({
            "gpu_types": items,
            "vendors": gpu_types_mod.vendor_presets(),
            "vendor_presets": gpu_types_mod.vendor_presets(),
            "used_vendors": gpu_types_mod.used_vendors(),
        })
    except Exception as e:  # noqa: BLE001
        log.error("列出显卡型号失败", extra={"fields": {"error": str(e)}})
        return JSONResponse({"error": "服务器内部错误，请稍后重试。"}, status_code=500)


@router.post("/api/gpu-types")
async def gpu_types_create(request: Request):
    """新增显卡型号。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "请求体须为 JSON"}, status_code=400)
    try:
        item = gpu_types_mod.create_type(
            request.app.state.db,
            name=body.get("name"),
            memory_gib=body.get("memory_gib"),
            cards_per_machine=body.get("cards_per_machine", 8),
            vendor=body.get("vendor"),
            released=body.get("released", True),
            note=body.get("note", ""),
        )
        log.info("新增显卡型号", extra={"fields": {"name": item["name"]}})
        return JSONResponse(item, status_code=201)
    except GpuTypeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        log.error("新增显卡型号失败", extra={"fields": {"error": str(e)}})
        return JSONResponse({"error": "服务器内部错误，请稍后重试。"}, status_code=500)


@router.patch("/api/gpu-types/{name}")
async def gpu_types_update(name: str, request: Request):
    """修改显卡型号（不允许改 name）。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "请求体须为 JSON"}, status_code=400)
    if "name" in body and body["name"] != name:
        return JSONResponse(
            {"error": "不允许修改型号名；要改名请新建后迁移历史记录"},
            status_code=400,
        )
    try:
        item = gpu_types_mod.update_type(
            request.app.state.db,
            name,
            memory_gib=body.get("memory_gib"),
            cards_per_machine=body.get("cards_per_machine"),
            vendor=body.get("vendor"),
            released=body.get("released"),
            note=body.get("note"),
        )
        log.info("更新显卡型号", extra={"fields": {"name": name}})
        return JSONResponse(item)
    except GpuTypeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        log.error("更新显卡型号失败", extra={"fields": {"error": str(e)}})
        return JSONResponse({"error": "服务器内部错误，请稍后重试。"}, status_code=500)


@router.delete("/api/gpu-types/{name}")
def gpu_types_delete(name: str, request: Request):
    """删除显卡型号（有库内引用时拒绝）。"""
    try:
        result = gpu_types_mod.delete_type(request.app.state.db, name)
        log.info("删除显卡型号", extra={"fields": {"name": name}})
        return JSONResponse(result)
    except GpuTypeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        log.error("删除显卡型号失败", extra={"fields": {"error": str(e)}})
        return JSONResponse({"error": "服务器内部错误，请稍后重试。"}, status_code=500)


@router.get("/api/upload/options")
def upload_options():
    """上传表单可选项（框架 / 显卡 / 路由策略由后端规则表提供，避免前后端各写一份）。"""
    return JSONResponse({
        "frameworks": launch_params.supported_frameworks(),
        "bench_frameworks": upload_mod.supported_bench_frameworks(),
        "gpu_types": upload_mod.supported_gpu_types(),
        "model_dtypes": upload_mod.supported_model_dtypes(),
        "router_policies": launch_params.router_policies(),
        "transfer_backends": launch_params.transfer_backends(),
        "benchmark_kinds": ["text", "vlm"],
    })


@router.post("/api/upload/detect-bench")
async def upload_detect_bench(csv_file: UploadFile = File(...)):
    """
    读一遍 CSV，按 spec decoding 列是否有值粗判 bench_framework，供前端预填。
    返回 {bench_framework, sglang_spec_present, vllm_spec_present}；
    bench_framework 为 null 时表示无法判断，需用户手选。
    """
    try:
        csv_bytes = await csv_file.read()
        return JSONResponse(upload_mod.detect_bench_framework(csv_bytes))
    except UploadError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/api/upload/inspect-config")
async def upload_inspect_config(config_file: UploadFile = File(...)):
    """
    选好 config.json 后立刻回显识别到的模型结构与元信息预填值，
    让用户当场确认传对了文件。这里只解析、不入库；非法文件返回 400 与具体原因。

    model_meta 里的三项就是留空时的入库值，前端把它们回显成占位提示而不是预填
    输入框——让用户核对而不是让用户抄一遍。
    """
    try:
        raw = await config_file.read()
        cfg = launch_params.load_model_config(raw)
        arch = launch_params.normalize_model_config(cfg)
        meta, notes = launch_params.model_meta(cfg, arch)
        return JSONResponse({"model_arch": arch, "model_meta": meta, "notes": notes})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/api/upload/detect")
async def upload_detect(
    framework: str = Form(""),
    command: str = Form(""),
):
    """
    判断一条启动命令是否为 PD 分离（供前端"填好即自动跳转"）。
    返回 {is_pd, role}；role 为 prefill/decode/both/null。
    """
    role = None
    if framework in launch_params.supported_frameworks():
        role = launch_params.detect_role(framework, command)
    is_pd = role is not None or launch_params.looks_like_pd(command)
    return JSONResponse({"is_pd": bool(is_pd), "role": role})


@router.post("/api/upload")
async def upload_run(
    request: Request,
    csv_file: UploadFile = File(...),
    config_file: UploadFile | None = File(None),
    framework: str = Form(...),
    framework_version: str = Form(...),
    model: str = Form(...),
    gpu_type: str = Form(...),
    model_params_b: str = Form(""),
    model_weight_gb: str = Form(""),
    model_dtype: str = Form(""),
    model_version: str = Form(""),
    bench_framework: str = Form(...),
    bench_flush_cache: str = Form(...),
    benchmark_kind: str = Form("text"),
    bench_image_count: str = Form(""),
    bench_image_resolution: str = Form(""),
    deployment_mode: str = Form("colocated"),
    launch_cmd: str = Form(""),
    prefill_cmd: str = Form(""),
    decode_cmd: str = Form(""),
    router_cmd: str = Form(""),
):
    """手工上传一次测试结果入库。

    单机/分布式：deployment_mode=colocated，命令走 launch_cmd。
    PD 分离    ：deployment_mode=pd_disagg，命令走 prefill_cmd / decode_cmd（+ router_cmd）。

    config_file 是模型目录下的 config.json（可选）。给了它才能推出 context_length /
    dtype / quantization / 批量调度默认值等启动命令里通常不写的参数，以及
    model_params_b / model_weight_gb / model_dtype 三个元信息列。

    这三列留空即按 config.json 推导值入库，填了以填的为准（不一致时在回显里告警）。
    没传 config 时 model_params_b 必填——它是分组对比的主轴，不能为空。
    """
    st = request.app.state
    meta = {
        "framework": framework,
        "framework_version": framework_version,
        "model": model,
        "model_version": model_version,
        "model_params_b": model_params_b,
        "model_weight_gb": model_weight_gb,
        "model_dtype": model_dtype,
        "gpu_type": gpu_type,
        "bench_framework": bench_framework,
        "bench_flush_cache": bench_flush_cache,
        "benchmark_kind": benchmark_kind,
        "bench_image_count": bench_image_count,
        "bench_image_resolution": bench_image_resolution,
    }
    try:
        csv_bytes = await csv_file.read()
        config_bytes = await config_file.read() if config_file is not None else None
        summary = upload_mod.ingest(
            st.db, meta, csv_bytes, launch_cmd,
            benchmark_root=st.config.scanner.benchmark_root,
            dir_pattern=st.config.scanner.dir_pattern,
            deployment_mode=deployment_mode,
            prefill_text=prefill_cmd,
            decode_text=decode_cmd,
            router_text=router_cmd,
            benchmark_kind=benchmark_kind,
            config_bytes=config_bytes,
        )
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
        "run_id": summary["run_id"], "metrics": summary["num_metrics"],
        "kind": summary.get("benchmark_kind"),
        "model_config_used": summary.get("model_config_used")}})
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
