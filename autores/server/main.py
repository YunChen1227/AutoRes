"""FastAPI 入口（进程 B）。装配 DB / Agent / 会话 / 报告注册表，挂载路由。"""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI

from autores.common.logging import setup_logging, get_logger
from autores.config import get_config
from autores.db import client as dbc
from autores.server.agent.loop import Agent
from autores.server.api import router
from autores.server.mcp_server import build_mcp_server, build_mcp_transport_security
from autores.server.reports_store import ReportStore
from autores.server.session import SessionStore

log = get_logger("server")


def create_app() -> FastAPI:
    setup_logging()
    cfg = get_config()

    db = dbc.connect(cfg.database)
    reports = ReportStore(cfg.report.ttl_minutes)
    sessions = SessionStore(cfg.session.ttl_minutes, cfg.session.max_messages)
    agent = Agent(
        llm_cfg=cfg.llm,
        db=db,
        report_output_dir=cfg.report.output_dir,
        report_registry=reports.register,
    )

    # MCP server：把 chatbot 能力封装为标准 MCP 工具，挂在 /mcp（Streamable HTTP）。
    # 用 stateless_http 免去会话保持；把内部路由设为 "/" 后挂到 /mcp，最终端点即 /mcp。
    mcp = build_mcp_server(db, cfg, reports)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        host=cfg.server.host,
        transport_security=build_mcp_transport_security(cfg),
    )

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # 启动：报告清理后台任务 + MCP 会话管理器（Streamable HTTP 必须在其上下文内运行）
        task = asyncio.create_task(_cleanup_loop(app))
        async with mcp.session_manager.run():
            log.info("API 启动完成（含 MCP /mcp）")
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="AutoRes 性能测试报告 Agent", lifespan=_lifespan)

    app.state.db = db
    app.state.reports = reports
    app.state.sessions = sessions
    app.state.agent = agent
    app.state.config = cfg

    app.include_router(router)
    app.mount("/mcp", mcp_app)
    return app


async def _cleanup_loop(app: FastAPI):
    """每 10 分钟清理过期报告（design.md §7.6）。"""
    while True:
        await asyncio.sleep(600)
        try:
            removed = app.state.reports.cleanup()
            if removed:
                log.info("清理过期报告", extra={"fields": {"removed": removed}})
        except Exception as e:  # noqa: BLE001
            log.error("报告清理异常", extra={"fields": {"error": str(e)}})


app = create_app()
