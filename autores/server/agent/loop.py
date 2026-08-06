"""
Agent 工具循环（design.md §7.2、§7.1 混合架构阶段一）。

标准 function-calling 循环：LLM 或回复文本，或调用工具；工具结果回填后继续，
直到调用 submit_query_spec（触发阶段二报告流水线）或输出纯文本。

以生成器形式 yield 事件 dict，供 API 层转成 SSE：
  {"type": "status",  "text": ...}
  {"type": "thinking", "text": ...}   # 推理过程增量（智谱 thinking 模式）
  {"type": "thinking_done"}           # 本轮推理结束，正文即将/正在输出
  {"type": "message", "text": ...}
  {"type": "report",  "download_url"?, "filename", "summary"}
  {"type": "error",   "text": ...}
"""
from __future__ import annotations

import json
from typing import Any, Iterator

from autores.common.logging import get_logger
from autores.config import LLMConfig
from autores.server.agent import tools as agent_tools
from autores.server.agent.llm_client import create_chat_client
from autores.server.agent.prompts import SYSTEM_PROMPT
from autores.server.report.pipeline import generate_report

log = get_logger("agent")


class Agent:
    def __init__(self, llm_cfg: LLMConfig, db, report_output_dir: str,
                 report_registry, spec_retry_limit: int = 2):
        self.cfg = llm_cfg
        self.db = db
        self.report_output_dir = report_output_dir
        self.report_registry = report_registry
        self.spec_retry_limit = spec_retry_limit
        self.client = create_chat_client(llm_cfg)

    def system_message(self) -> dict:
        return {"role": "system", "content": SYSTEM_PROMPT}

    def _llm_kwargs(self, messages: list[dict]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "tools": agent_tools.TOOL_DEFINITIONS,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        thinking = (
            {"type": "enabled"} if self.cfg.thinking_enabled else {"type": "disabled"}
        )
        if self.cfg.provider == "zhipu":
            kwargs["thinking"] = thinking
        elif self.cfg.thinking_enabled:
            # ModelVerse 等 OpenAI 兼容网关通过 extra_body 传 thinking
            kwargs["extra_body"] = {"thinking": thinking}
        return kwargs

    def _iter_llm(self, messages: list[dict]) -> Iterator[tuple[str, Any]]:
        """
        调用 LLM 并 yield 内部事件：
          ("thinking", str)  推理增量
          ("content", str)   正文增量
          ("complete", dict) 完整结果 {content, reasoning_content, tool_calls}
        """
        use_stream = self.cfg.thinking_enabled
        if not use_stream:
            resp = self.client.chat.completions.create(**self._llm_kwargs(messages))
            msg = resp.choices[0].message
            reasoning = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None) or ""
            if reasoning:
                yield ("thinking", reasoning)
                yield ("thinking_done", None)
            tool_calls = _normalize_tool_calls(msg.tool_calls)
            yield ("complete", {
                "content": msg.content or "",
                "reasoning_content": reasoning,
                "tool_calls": tool_calls,
            })
            return

        stream = self.client.chat.completions.create(
            **self._llm_kwargs(messages), stream=True,
        )
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_acc: dict[int, dict[str, str]] = {}
        saw_reasoning = False

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            reasoning_delta = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if reasoning_delta:
                saw_reasoning = True
                reasoning_parts.append(reasoning_delta)
                yield ("thinking", reasoning_delta)
            content_delta = getattr(delta, "content", None)
            if content_delta:
                if saw_reasoning and reasoning_parts and not content_parts:
                    yield ("thinking_done", None)
                    saw_reasoning = False  # 只发一次
                content_parts.append(content_delta)
                yield ("content", content_delta)
            tool_calls_delta = getattr(delta, "tool_calls", None)
            if tool_calls_delta:
                for tc in tool_calls_delta:
                    slot = tool_calls_acc.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""},
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments

        if reasoning_parts and not content_parts and not tool_calls_acc:
            yield ("thinking_done", None)

        tool_calls = [
            {
                "id": tool_calls_acc[i]["id"],
                "type": "function",
                "function": {
                    "name": tool_calls_acc[i]["name"],
                    "arguments": tool_calls_acc[i]["arguments"],
                },
            }
            for i in sorted(tool_calls_acc)
            if tool_calls_acc[i]["name"]
        ]
        yield ("complete", {
            "content": "".join(content_parts),
            "reasoning_content": "".join(reasoning_parts),
            "tool_calls": tool_calls,
        })

    def _dispatch_tool(self, name: str, args: dict) -> tuple[dict, dict | None]:
        if name == "list_dimension_values":
            return agent_tools.list_dimension_values(
                self.db, args.get("dimension"), args.get("filters")), None

        if name == "count_matching_runs":
            return agent_tools.count_matching_runs(
                self.db, args.get("filters", {}), args.get("exclude")), None

        if name == "submit_query_spec":
            spec, err = agent_tools.validate_query_spec(args)
            if err:
                return {"ok": False, "validation_error": err}, None
            result = generate_report(self.db, spec, self.report_output_dir)
            if result.empty:
                return {"ok": False, "reason": "命中 0 条记录，未生成报告"}, None
            download_url, filename = self.report_registry(result.file_path)
            summary = {
                "num_runs": result.num_runs,
                "num_metric_rows": result.num_metric_rows,
                "columns": result.column_labels,
                "notes": result.notes,
            }
            report_event = {
                "type": "report",
                "download_url": download_url,
                "filename": filename,
                "summary": summary,
            }
            return {"ok": True, "summary": summary}, report_event

        return {"error": f"未知工具: {name}"}, None

    def run_turn(self, history: list[dict]) -> Iterator[dict]:
        spec_retries = 0
        for _round in range(self.cfg.max_tool_rounds):
            yield {"type": "status", "text": "正在连接模型…"}
            try:
                complete: dict[str, Any] | None = None
                streamed_content = False
                for kind, payload in self._iter_llm(history):
                    if kind == "thinking":
                        yield {"type": "thinking", "text": payload}
                    elif kind == "thinking_done":
                        yield {"type": "thinking_done"}
                    elif kind == "content":
                        streamed_content = True
                        if payload:
                            yield {"type": "message", "text": payload}
                    elif kind == "complete":
                        complete = payload
            except Exception as e:  # noqa: BLE001
                log.error("LLM 调用失败", extra={"fields": {"error": str(e)}})
                yield {"type": "error", "text": _llm_error_text(e)}
                return

            if complete is None:
                yield {"type": "error", "text": "模型未返回有效响应。"}
                return

            text = complete["content"]
            tool_calls = complete["tool_calls"]

            if not tool_calls:
                history.append({"role": "assistant", "content": text})
                if text and not streamed_content:
                    yield {"type": "message", "text": text}
                return

            history.append({
                "role": "assistant",
                "content": text,
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}

                yield {"type": "status", "text": _status_text(name, args)}

                result, report_event = self._dispatch_tool(name, args)

                if name == "submit_query_spec" and not result.get("ok"):
                    if "validation_error" in result:
                        spec_retries += 1
                        if spec_retries > self.spec_retry_limit:
                            yield {"type": "message",
                                   "text": "抱歉，我没能正确构造查询，请换种说法再试。"}
                            history.append(_tool_msg(tc["id"], result))
                            return

                history.append(_tool_msg(tc["id"], result))

                if report_event is not None:
                    yield report_event
                    return

        yield {"type": "message", "text": "处理超过最大步数，请缩小范围或换种描述再试。"}


def _llm_error_text(err: Exception) -> str:
    s = str(err)
    if "429" in s or "1302" in s or "1305" in s or "ReachLimit" in s or "并发" in s:
        return "模型请求过于频繁（智谱 API 限流），请等待 10～30 秒后重试。"
    return "模型服务暂不可用，请稍后重试。"


def _normalize_tool_calls(raw) -> list[dict]:
    if not raw:
        return []
    out = []
    for tc in raw:
        out.append({
            "id": tc.id,
            "type": "function",
            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
        })
    return out


def _tool_msg(tool_call_id: str, result: dict) -> dict:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(result, ensure_ascii=False, default=str),
    }


def _status_text(name: str, args: dict) -> str:
    if name == "list_dimension_values":
        return f"正在查询库内「{args.get('dimension', '')}」的可选值…"
    if name == "count_matching_runs":
        return "正在预检匹配的测试记录数量…"
    if name == "submit_query_spec":
        return "正在生成对比报告…"
    return f"正在执行 {name}…"
