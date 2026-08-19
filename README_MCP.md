# AutoRes MCP Server

把 AutoRes chatbot 的现有能力（盘点、维度查询、命中预检、生成 Excel 对比报告）
封装为标准 **MCP（Model Context Protocol）** 工具，通过 **Streamable HTTP** 传输
挂载在主服务的 **`/mcp`** 路径下。

这样任意 MCP 客户端（Cursor、Claude Desktop、自研 Agent、SDK 脚本等）都能直接调用
这些工具，**不经过内置 LLM**——由外部模型/客户端来编排对话，AutoRes 只提供确定性的数据与报告工具。

> 工具实现与 chatbot 后端（`autores/server/agent/tools.py`、`report/pipeline.py`）
> 复用同一套函数与语义，两处逻辑不会漂移。

---

## 1. 端点

| 项 | 值 |
|----|----|
| 传输协议 | MCP Streamable HTTP（无状态 / stateless） |
| URL | `http://<host>:<port>/mcp` （默认 `http://127.0.0.1:8080/mcp`） |
| 与 chatbot 关系 | 同一个 FastAPI 进程，共用数据库、报告注册表与下载端点 |

MCP 随主服务一起启动，**无需单独进程**。启动方式与原来完全一致：

```bash
# 裸机
bash scripts/start.sh
# 或直接
python -m uvicorn autores.server.main:app --host 0.0.0.0 --port 8080

# Docker Compose
docker compose up -d
```

启动日志出现 `API 启动完成（含 MCP /mcp）` 即表示 MCP 已就绪。

依赖已加入 `requirements.txt`（`mcp>=1.9`），首次部署请重新安装依赖：

```bash
pip install -r requirements.txt
```

---

## 2. 提供的工具

| 工具 | 作用 | 主要参数 |
|------|------|----------|
| `list_dimensions` | 列出所有可用于筛选/对比的维度名（其他工具的 `dimension` / `filters` 键必须取自此列表） | 无 |
| `summarize_reports` | 按 **显卡 × 模型** 盘点库内测试记录数量 | `filters?` |
| `list_dimension_values` | 列出某维度在库内的**真实取值**及各值计数（把口语如 `4090` 对齐到真实值） | `dimension`，`filters?` |
| `count_matching_runs` | 生成报告前**预检**一组条件命中多少条记录（1~20 条会附带明细） | `filters`，`exclude?` |
| `generate_comparison_report` | 生成 **Excel 对比报告**，返回下载链接与摘要 | `compare_on`，`filters?`，`compare_values?`，`exclude?`，`metrics?`，`metric_filters?`，`normalize_gpu_scale?` |
| `health` | 健康检查（数据库连通性） | 无 |

### 推荐调用流程

1. `list_dimension_values` —— 把用户口语对齐到库内真实值（如 `4090` → `NVIDIA RTX 4090`）；
2. `count_matching_runs` —— 预检命中数量：0 条则提示无数据，过多则加约束或排除取值；
3. `generate_comparison_report` —— 生成报告并把返回的 `download_url` 交给用户下载。

### `generate_comparison_report` 返回示例

```json
{
  "ok": true,
  "download_url": "http://127.0.0.1:8080/api/download/05ff9278fb264fb28057c04b63e9d702",
  "filename": "对比报告_framework_20260819_153948.xlsx",
  "summary": {
    "num_runs": 4,
    "num_metric_rows": 795,
    "columns": ["vllm"],
    "notes": { "multi_framework": false, "multi_version": true }
  }
}
```

- `download_url` 是**绝对地址**，可直接下载。链接由现有 `/api/download/{token}` 端点提供，
  带 TTL（`config.yaml` 的 `report.ttl_minutes`，默认 120 分钟），过期自动清理。
- 命中 0 条时返回 `{"ok": false, "reason": "命中 0 条记录，未生成报告"}`；
  QuerySpec 非法时返回 `{"ok": false, "error": "..."}`。

---

## 3. 客户端接入

### 3.1 Cursor

编辑 `~/.cursor/mcp.json`（或项目内 `.cursor/mcp.json`），加入：

```json
{
  "mcpServers": {
    "autores": {
      "url": "http://127.0.0.1:8080/mcp"
    }
  }
}
```

保存后在 Cursor 的 MCP 设置里应能看到 `autores` 及其 6 个工具。

### 3.2 Claude Desktop / 其他仅支持 stdio 的客户端

部分客户端只支持 stdio，需用 `mcp-remote` 把 HTTP 端点桥接为 stdio：

```json
{
  "mcpServers": {
    "autores": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8080/mcp"]
    }
  }
}
```

### 3.3 Python SDK

```python
import asyncio
from mcp import Client   # pip install "mcp>=1.9"

async def main():
    async with Client("http://127.0.0.1:8080/mcp") as client:
        tools = await client.list_tools()
        print([t.name for t in tools.tools])

        # 先看有哪些显卡取值
        r = await client.call_tool("list_dimension_values", {"dimension": "gpu_type"})
        print(r.content[0].text)

        # 预检
        r = await client.call_tool("count_matching_runs",
                                   {"filters": {"gpu_type": "H20-141G"}})
        print(r.content[0].text)

        # 生成报告
        r = await client.call_tool("generate_comparison_report",
                                   {"compare_on": "framework",
                                    "filters": {"gpu_type": "H20-141G"}})
        print(r.content[0].text)

asyncio.run(main())
```

> 结果同时在 `r.content[0].text`（JSON 文本）与 `r.structured_content`（结构化，取决于客户端版本）中可读。

### 3.4 curl 快速自检

Streamable HTTP 是 JSON-RPC，客户端须声明可接受两种响应类型。列出工具：

```bash
curl -s http://127.0.0.1:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

> 手写 JSON-RPC 需自行处理 initialize 握手，日常调试建议直接用上面的 Python SDK 或
> `npx @modelcontextprotocol/inspector` 图形化调试器连 `http://127.0.0.1:8080/mcp`。

---

## 4. 配置说明

MCP 复用主服务的 `config.yaml`，无需额外配置。与下载链接相关的新增项：

```yaml
server:
  host: "0.0.0.0"
  port: 8080
  # MCP 工具返回报告下载链接的前缀（绝对地址）。
  # 留空 = 按 host:port 自动推导（0.0.0.0 → 127.0.0.1）。
  # 反代 / 容器 / 公网访问场景请填对外可达地址：
  # public_base_url: "https://autores.example.com"
  public_base_url: ""
  # 经代理域名访问 MCP 时的 Host 白名单（配 public_base_url 时会自动加入其域名）
  # mcp_allowed_hosts:
  #   - "model-download.example.com:*"
  # mcp_disable_host_check: false   # 内网调试可设 true 完全关闭 Host 校验
```

也可用环境变量覆盖：`AUTORES_SERVER_PUBLIC_BASE_URL=https://autores.example.com`。

> 若客户端与服务不在同一台机器，务必设置 `public_base_url`（或确保 host 对外可达），
> 否则返回的下载链接会指向 `127.0.0.1` 而无法访问。

**代理域名访问 MCP：** 服务 `host` 为 `0.0.0.0` 时默认已关闭 Host 校验，一般无需额外配置。
若仍见 `421 Misdirected Request` / `Invalid Host header`，在 `config.yaml` 中配置：

```yaml
server:
  public_base_url: "http://你的代理域名"   # 推荐：同时修正报告下载链接
  # 或显式白名单：
  mcp_allowed_hosts:
    - "你的代理域名:*"
```

---

## 5. 常见问题

| 现象 | 排查 |
|------|------|
| 客户端连不上 `/mcp` | 确认服务已启动且日志含 `含 MCP /mcp`；确认端口、防火墙；URL 结尾是 `/mcp` |
| `421 Misdirected Request` / `Invalid Host header` | MCP SDK 拒绝了代理域名的 Host 头。`host=0.0.0.0` 部署后重启服务通常已修复；否则在 `config.yaml` 配 `public_base_url` 或 `mcp_allowed_hosts`（见 §4） |
| `406 Not Acceptable` | 请求头缺 `Accept: application/json, text/event-stream`（用 SDK 则无需手动加） |
| 下载链接打不开 | 链接指向 `127.0.0.1` 但客户端在别的机器 → 配置 `server.public_base_url`；或报告已过期（超过 `report.ttl_minutes`）重新生成 |
| `ModuleNotFoundError: mcp` | 重新 `pip install -r requirements.txt` |
| 报告生成返回 `ok:false, reason: 命中 0 条` | 先用 `list_dimension_values` + `count_matching_runs` 确认条件能命中记录 |

---

## 6. 实现位置（便于维护）

| 文件 | 说明 |
|------|------|
| `autores/server/mcp_server.py` | MCP 工具定义与装配（`build_mcp_server`），闭包复用后端函数 |
| `autores/server/main.py` | 构建 MCP app，`app.mount("/mcp", ...)`，并在 lifespan 内运行 MCP 会话管理器 |
| `autores/config.py` | 新增 `ServerConfig.public_base_url` |
| `requirements.txt` | 新增 `mcp>=1.9` |
