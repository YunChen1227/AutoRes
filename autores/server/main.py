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
from autores.server.reports_store import ReportStore
from autores.server.session import SessionStore

log = get_logger("server")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # 启动：起报告清理后台任务
    task = asyncio.create_task(_cleanup_loop(app))
    log.info("API 启动完成")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    setup_logging()
    cfg = get_config()

    app = FastAPI(title="AutoRes 性能测试报告 Agent", lifespan=_lifespan)

    db = dbc.connect(cfg.database, ensure_indexes=True)
    reports = ReportStore(cfg.report.ttl_minutes)
    sessions = SessionStore(cfg.session.ttl_minutes, cfg.session.max_messages)
    agent = Agent(
        llm_cfg=cfg.llm,
        db=db,
        report_output_dir=cfg.report.output_dir,
        report_registry=reports.register,
    )

    app.state.db = db
    app.state.reports = reports
    app.state.sessions = sessions
    app.state.agent = agent
    app.state.config = cfg

    app.include_router(router)
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
