"""
MCP server（挂载在主应用 /mcp 路径下）。

把 chatbot 现有的确定性能力封装为标准 MCP 工具，供任意 MCP 客户端
（Cursor / Claude Desktop / 自研 Agent 等）通过 Streamable HTTP 直接调用，
无需经过内置 LLM。工具实现完全复用 chatbot 后端：

  - list_dimensions        : 列出所有可用于筛选/对比的维度名（枚举参考）
  - summarize_reports      : 按显卡×模型盘点库内测试记录数量
  - list_dimension_values  : 列出某维度在库内的真实取值及计数（口语→真实值对齐）
  - count_matching_runs    : 提交前预检一组条件命中多少条记录
  - analyze_saturation     : 性能饱和点 / hardware wall 分析（JSON + Markdown）
  - generate_comparison_report : 生成 Excel 对比报告，返回下载链接
  - health                 : 健康检查（DB 连通性）

设计上刻意与 Agent function-calling 工具（agent/tools.py）保持同一套语义与
实现函数，避免两处逻辑漂移。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from autores.common.logging import get_logger
from autores.config import Config
from autores.db import schema
from autores.server.agent import tools as agent_tools
from autores.server.report.pipeline import generate_report

log = get_logger("mcp")

_INSTRUCTIONS = (
    "AutoRes 性能测试工具集。用于查询 sglang / vllm / vllm-ascend 的压测记录、"
    "分析性能饱和点，并生成 Excel 对比报告。典型流程：\n"
    "1) 用 list_dimension_values 把用户口语（如 '4090'）对齐到库内真实值；\n"
    "2) 用 count_matching_runs 预检命中数量，0 条则提示无数据、过多则加约束；\n"
    "3a) 查饱和点 / 推荐并发 / 性能墙 → 调用 analyze_saturation（无需生成 Excel）；\n"
    "3b) 横向对比 → 用 generate_comparison_report 生成报告并返回下载链接。"
)


def _base_url(cfg: Config) -> str:
    """报告下载链接前缀（绝对地址）。"""
    if cfg.server.public_base_url:
        return cfg.server.public_base_url.rstrip("/")
    host = cfg.server.host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{cfg.server.port}"


def _host_pattern(host: str) -> str:
    """Host 白名单项：无端口则补 :*（任意端口）。"""
    host = host.strip()
    if not host:
        return host
    if host.endswith(":*") or (host.startswith("[") and "]:" in host):
        return host
    if host.count(":") == 1 and not host.startswith("["):
        return host  # 已带端口，如 example.com:8080
    return f"{host}:*"


def build_mcp_transport_security(cfg: Config) -> TransportSecuritySettings | None:
    """MCP Streamable HTTP 的 Host/Origin 校验策略。

    MCP SDK 在 host=127.0.0.1 时会默认只放行 localhost，经代理域名访问会 421。
    策略：
      - mcp_disable_host_check=true → 完全关闭
      - 配置了 mcp_allowed_hosts 或 public_base_url → 启用白名单（含 localhost）
      - server.host 为 0.0.0.0/:: 且无上述配置 → 关闭（K8s/反代常见场景）
      - 其余 → None，走 SDK 默认 localhost 保护
    """
    srv = cfg.server
    if srv.mcp_disable_host_check:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    hosts: list[str] = list(srv.mcp_allowed_hosts)
    if srv.public_base_url:
        hostname = urlparse(srv.public_base_url).hostname
        if hostname and hostname not in hosts:
            hosts.append(hostname)

    if hosts:
        patterns = sorted({_host_pattern(h) for h in hosts if h.strip()})
        patterns.extend(["127.0.0.1:*", "localhost:*", "[::1]:*"])
        allowed = sorted(set(patterns))
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed,
        )

    if srv.host in ("0.0.0.0", "::"):
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    return None


def build_mcp_server(db, cfg: Config, reports) -> MCPServer:
    """装配 MCPServer 实例，工具以闭包捕获 db / 配置 / 报告注册表。"""
    mcp = MCPServer(name="autores", instructions=_INSTRUCTIONS)
    base_url = _base_url(cfg)

    @mcp.tool()
    def list_dimensions() -> dict:
        """列出所有可用于筛选（filters/exclude）与对比（compare_on）的维度名。

        其他工具的 dimension / filters 键必须取自本列表。"""
        return {"dimensions": list(schema.ALL_DIMENSIONS)}

    @mcp.tool()
    def summarize_reports(filters: dict[str, Any] | None = None) -> dict:
        """按显卡×模型盘点库内测试记录（报告）数量。

        用于回答"现在有多少报告""每张卡每个模型各有多少"这类盘点问题；
        忽略框架版本、启动参数等细节，只按 gpu_type + model 归并计数。

        filters: 可选，额外维度等值约束以缩小盘点范围（键取自 list_dimensions）。"""
        return agent_tools.summarize_reports(db, filters)

    @mcp.tool()
    def list_dimension_values(
        dimension: str,
        filters: dict[str, Any] | None = None,
    ) -> dict:
        """列出数据库中某个维度的所有真实取值及各值的记录数。

        在把用户口语（如 '4090'）对齐到库内真实值（如 'NVIDIA RTX 4090'）之前，
        必须先用本工具确认库内实际有哪些值；也用于向用户列出候选做澄清。

        dimension: 维度名（取自 list_dimensions）。
        filters: 可选，其他维度的等值约束以缩小统计范围。"""
        return agent_tools.list_dimension_values(db, dimension, filters)

    @mcp.tool()
    def count_matching_runs(
        filters: dict[str, Any],
        exclude: dict[str, Any] | None = None,
    ) -> dict:
        """统计满足一组维度条件的测试记录数量（生成报告前应先预检）。

        命中 0 条→告知用户没有该数据；数量过多→提示加约束或排除某些取值。
        命中 1~20 条时会附带各条记录的关键信息。

        filters: 维度等值条件，值可为数组（表示多选）。
        exclude: 可选，排除项，键为维度名、值为要排除的取值数组。"""
        return agent_tools.count_matching_runs(db, filters, exclude)

    @mcp.tool()
    def analyze_saturation(
        filters: dict[str, Any] | None = None,
        exclude: dict[str, Any] | None = None,
        run_id: str | None = None,
        slo_ttft_p99: float | None = None,
        slo_tpot_mean: float | None = None,
        slo_itl_p95: float | None = None,
        slo_e2e_p99: float | None = None,
        plateau_gain: float | None = None,
        latency_factor: float | None = None,
        headroom: float | None = None,
        include_points: bool = False,
        max_runs: int | None = None,
    ) -> dict:
        """分析性能饱和点（hardware wall）：按 input_length 给出墙并发、推荐运行点、
        瓶颈归因与置信度。查饱和点 / 推荐并发时用本工具，无需生成 Excel。

        filters: 维度等值条件（键取自 list_dimensions），值可为数组。
        exclude: 可选，排除项。
        run_id: 可选，精确指定一条 run。
        slo_*: 可选，延迟 SLO 上限（ms）。
        plateau_gain / latency_factor / headroom: 可选，检测器阈值。
        include_points: 默认 false；true 时附带逐并发点明细（上下文更大）。
        max_runs: 最多分析几条，默认 5；超出则返回需加约束的提示。

        返回：{ok, n_runs, settings, runs, markdown, caveats} 或 {ok:false, ...}。"""
        args: dict[str, Any] = {
            "filters": filters,
            "exclude": exclude,
            "run_id": run_id,
            "slo_ttft_p99": slo_ttft_p99,
            "slo_tpot_mean": slo_tpot_mean,
            "slo_itl_p95": slo_itl_p95,
            "slo_e2e_p99": slo_e2e_p99,
            "include_points": include_points,
        }
        if plateau_gain is not None:
            args["plateau_gain"] = plateau_gain
        if latency_factor is not None:
            args["latency_factor"] = latency_factor
        if headroom is not None:
            args["headroom"] = headroom
        if max_runs is not None:
            args["max_runs"] = max_runs
        return agent_tools.analyze_saturation(db, args)

    @mcp.tool()
    def generate_comparison_report(
        compare_on: str,
        filters: dict[str, Any] | None = None,
        compare_values: list[Any] | None = None,
        exclude: dict[str, Any] | None = None,
        metrics: list[str] | None = None,
        metric_filters: dict[str, Any] | None = None,
        normalize_gpu_scale: bool = True,
    ) -> dict:
        """生成 Excel 对比报告，返回下载链接与结果摘要。

        提交前建议先用 count_matching_runs 预检、并消除维度歧义。

        compare_on: 对比轴——在哪个维度上横向比较（取自 list_dimensions）。
        filters: 约束项——其余维度保持一致的等值条件（值可为数组）。
        compare_values: 可选，对比轴上的目标取值；缺省=该轴下所有匹配值。
        exclude: 可选，排除项，键为维度、值为要剔除的取值数组。
        metrics: 可选，要对比的指标列名；缺省=全部指标。
        metric_filters: 可选，按 input_length / concurrency 进一步筛选，值为数组。
        normalize_gpu_scale: 可选，默认 true。各配置卡数不同时按卡数做弱扩展归一
            （吞吐×卡数比、并发同比对齐、延迟不变）以做"对齐卡数"的公平对比；
            卡数相同则自动无操作；需看原始未换算数值时设 false。

        返回：{ok, download_url, filename, summary} 或 {ok: false, error/reason}。"""
        spec_dict = {
            "compare_on": compare_on,
            "filters": filters or {},
            "compare_values": compare_values,
            "exclude": exclude or {},
            "metrics": metrics,
            "metric_filters": metric_filters or {},
            "normalize_gpu_scale": normalize_gpu_scale,
        }
        spec, err = agent_tools.validate_query_spec(spec_dict)
        if err:
            return {"ok": False, "error": f"QuerySpec 非法: {err}"}

        result = generate_report(db, spec, cfg.report.output_dir)
        if result.empty:
            return {"ok": False, "reason": "命中 0 条记录，未生成报告"}

        download_path, filename = reports.register(result.file_path)
        log.info("MCP 生成报告", extra={"fields": {
            "filename": filename, "num_runs": result.num_runs}})
        return {
            "ok": True,
            "download_url": f"{base_url}{download_path}",
            "filename": filename,
            "summary": {
                "num_runs": result.num_runs,
                "num_metric_rows": result.num_metric_rows,
                "columns": result.column_labels,
                "notes": result.notes,
            },
        }

    @mcp.tool()
    def health() -> dict:
        """健康检查：返回服务状态与数据库连通性。"""
        try:
            db.ping()
            return {"status": "ok", "db": "ok"}
        except Exception as e:  # noqa: BLE001
            return {"status": "degraded", "db": f"error: {e}"}

    return mcp
