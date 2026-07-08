"""
Agent 工具循环（design.md §7.2、§7.1 混合架构阶段一）。

标准 function-calling 循环：LLM 或回复文本，或调用工具；工具结果回填后继续，
直到调用 submit_query_spec（触发阶段二报告流水线）或输出纯文本。

以生成器形式 yield 事件 dict，供 API 层转成 SSE：
  {"type": "status",  "text": ...}
  {"type": "message", "text": ...}
  {"type": "report",  "download_url"?, "filename", "summary"}
  {"type": "error",   "text": ...}
"""
from __future__ import annotations

import json
from typing import Any, Iterator

from openai import OpenAI

from autores.common.logging import get_logger
from autores.config import LLMConfig
from autores.server.agent import tools as agent_tools
from autores.server.agent.prompts import SYSTEM_PROMPT
from autores.server.report.pipeline import generate_report

log = get_logger("agent")


class Agent:
    def __init__(self, llm_cfg: LLMConfig, db, report_output_dir: str,
                 report_registry, spec_retry_limit: int = 2):
        self.cfg = llm_cfg
        self.db = db
        self.report_output_dir = report_output_dir
        # report_registry: 可调用，(file_path) -> download_url（token 注册，见 api 层）
        self.report_registry = report_registry
        self.spec_retry_limit = spec_retry_limit
        self.client = OpenAI(base_url=llm_cfg.base_url, api_key=llm_cfg.api_key,
                             timeout=llm_cfg.timeout_seconds)

    def system_message(self) -> dict:
        return {"role": "system", "content": SYSTEM_PROMPT}

    def _call_llm(self, messages: list[dict]):
        return self.client.chat.completions.create(
            model=self.cfg.model,
            messages=messages,
            tools=agent_tools.TOOL_DEFINITIONS,
            temperature=self.cfg.temperature,
        )

    def _dispatch_tool(self, name: str, args: dict) -> tuple[dict, dict | None]:
        """
        执行一个工具。返回 (给 LLM 的结果 dict, 报告事件 或 None)。
        submit_query_spec 成功时会真正生成报告并返回报告事件。
        """
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
        """
        跑一轮对话（history 已含 system + 历史消息 + 本轮 user）。
        yield 事件；同时把新增的 assistant/tool 消息 append 回 history（就地更新，供下轮复用）。
        """
        spec_retries = 0
        for _round in range(self.cfg.max_tool_rounds):
            try:
                resp = self._call_llm(history)
            except Exception as e:  # noqa: BLE001
                log.error("LLM 调用失败", extra={"fields": {"error": str(e)}})
                yield {"type": "error", "text": "模型服务暂不可用，请稍后重试。"}
                return

            choice = resp.choices[0]
            msg = choice.message

            # 无工具调用 → 纯文本回复，结束本轮
            if not msg.tool_calls:
                text = msg.content or ""
                history.append({"role": "assistant", "content": text})
                if text:
                    yield {"type": "message", "text": text}
                return

            # 有工具调用：先把 assistant 的 tool_calls 消息入历史
            history.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            })

            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                yield {"type": "status", "text": _status_text(name, args)}

                result, report_event = self._dispatch_tool(name, args)

                # QuerySpec 校验失败：给 LLM 自修正机会，超限则提示用户
                if name == "submit_query_spec" and not result.get("ok"):
                    if "validation_error" in result:
                        spec_retries += 1
                        if spec_retries > self.spec_retry_limit:
                            yield {"type": "message",
                                   "text": "抱歉，我没能正确构造查询，请换种说法再试。"}
                            history.append(_tool_msg(tc.id, result))
                            return

                history.append(_tool_msg(tc.id, result))

                if report_event is not None:
                    yield report_event
                    return  # 报告已生成，结束本轮

        # 超过最大轮次
        yield {"type": "message", "text": "处理超过最大步数，请缩小范围或换种描述再试。"}


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
