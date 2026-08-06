"""LLM 客户端工厂：OpenAI 兼容端点 或 智谱官方 SDK（zai-sdk）。"""
from __future__ import annotations

from typing import Any

from autores.config import LLMConfig


def create_chat_client(cfg: LLMConfig) -> Any:
    """
    按 llm.provider 创建聊天客户端。

    - openai_compat：任意 OpenAI 兼容端点（本地 vLLM/SGLang 等）
    - zhipu：智谱开放平台，使用官方 ZhipuAiClient
      文档：https://docs.bigmodel.cn/cn/guide/develop/python/introduction
    """
    if cfg.provider == "zhipu":
        from zai import ZhipuAiClient

        base_url = cfg.base_url.rstrip("/")
        if not base_url.endswith("/v4"):
            # 允许用户写 .../v4/ 或 .../v4/chat/completions 的父路径
            if base_url.endswith("/v4/chat/completions"):
                base_url = base_url[: -len("/chat/completions")]
        return ZhipuAiClient(
            api_key=cfg.api_key,
            base_url=f"{base_url}/",
            timeout=cfg.timeout_seconds,
            max_retries=2,
        )

    from openai import OpenAI

    return OpenAI(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        timeout=cfg.timeout_seconds,
    )
