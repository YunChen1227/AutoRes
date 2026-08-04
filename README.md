# AutoRes — 性能测试结果管理与报告 Agent

自动采集 **sglang / vllm** 的性能测试结果入库（SQLite），并通过 Web chatbot 用自然语言按需生成 Excel 对比报告。

完整设计见 [docs/design.md](docs/design.md)。

## 能做什么

| 能力 | 说明 |
|------|------|
| 自动入库 | Scanner 定时扫描 NAS 时间戳目录，解析 `result.csv` + `metadata.json` |
| 手工上传 | 数据不在 NAS 时，在 `/upload` 提交 CSV + 启动命令 txt + 元信息，规则与落盘脚本一致 |
| 自然语言对比 | Chatbot 多轮澄清需求 → 确定性流水线查库对齐 → 下载 Excel |
| 跨框架参数对齐 | `tools/param_map.py` 维护 vLLM ↔ SGLang 启动参数配对（含量纲/类型差异说明） |
| Excel 对比报告 | 双层表头矩阵宽表；两方对比时带 `A vs B` 相对差异列与按 `Input_Length` 的块汇总 |

## 组成

```
数据入口                          服务
────────                          ────
tools/to_csv.py  ──► NAS 目录 ──► Scanner ──┐
frontend/upload.html ──────────► API 上传 ──┤──► SQLite
                                            │
浏览器 chatbot / 下载 ◄── API + Agent + 报告流水线 ◄┘
```

- **落盘脚本** `tools/to_csv.py`：测试人员本机运行，把 bench 输出整理为 `result.csv` + `metadata.json`，写入 NAS 时间戳目录。
- **Scanner**（`autores/scanner/`）：定时扫描 NAS，解析入库。
- **API + 前端**（`autores/server/` + `frontend/`）：
  - `/` — chatbot（SSE）
  - `/upload` — 手工上传入库
  - `/api/chat`、`/api/upload`、`/api/download/{token}`、`/api/health`
- **参数工具**（`tools/`）：
  - `param_map.py` — vLLM/SGLang 启动参数配对表（与 `autores/db/schema.py` 的 `PARAM_DIMENSIONS` 一一对应）
  - `verify_param_map.py` — 对照上游源码校验 flag 是否仍存在
  - `gpu_memory_presets.py` — SGLang 按显存档位推导的参考中间量（DERIVED 参数辅助）

## 快速开始

### 1. 测试人员落盘

```bash
python tools/to_csv.py \
  --framework sglang --framework-version 0.4.6 \
  --input-dir ./bench_logs \
  --nas-dir /mnt/nas/benchmark_root \
  --gpu-type H20-141G \
  --model GLM-4.5 --model-version distributed2 \
  --launch-cmd "python -m sglang.launch_server --tp-size 8 --enable-hierarchical-cache"
```

vllm 场景需额外传 `--bench-cmd`（用于补 `--random-input-len`），且 bench 时须加
`--percentile-metrics ttft,tpot,itl,e2el` 才有 E2E 指标。详见 design.md §5.2。

### 2. 手工上传（可选）

服务启动后打开 `http://<服务器>:8080/upload`：

1. 上传符合固定 schema 的结果 CSV（须含 `Input_Length`、`Concurrency`）
2. 上传启动命令 txt（支持 `#` 注释、空行、反斜杠续行）
3. 填写 `framework` / `framework_version` / `model` / `model_version` / `gpu_type`

启动参数提取复用 `tools/to_csv.py` 的同一套规则，入库记录与 Scanner 路径在查询/报告环节无差别。详见 design.md §5.6。

#### 样例：`result.csv`

表头与 `tools/to_csv.py` 产出一致；**必填列**只有 `Input_Length`、`Concurrency`，其余指标列可按实际填写，缺测填 `N/A`。下面两行是示意数值：

```csv
Input_Length,Concurrency,Request_Throughput,Input_Throughput,Output_Throughput,Total_Throughput,TTFT_Mean(ms),TTFT_Median(ms),TTFT_P95(ms),TTFT_P99(ms),TPOT_Mean(ms),TPOT_Median(ms),TPOT_P95(ms),TPOT_P99(ms),ITL_Mean(ms),ITL_Median(ms),ITL_P95(ms),ITL_P99(ms),E2E_Mean(ms),E2E_Median(ms),E2E_P95(ms),E2E_P99(ms),Completed,Total_Input_Tokens,Total_Output_Tokens
1024,1,2.15,2200.5,210.3,2410.8,45.2,42.1,68.0,82.5,12.3,11.8,15.1,18.0,12.1,11.6,14.8,17.5,1280.0,1250.0,1420.0,1560.0,50,51200,10240
1024,8,12.8,9800.0,980.5,10780.5,62.4,58.0,95.2,120.0,13.5,12.9,17.2,21.0,13.2,12.6,16.8,20.5,1450.0,1400.0,1680.0,1920.0,400,409600,81920
2048,1,1.82,3700.2,185.0,3885.2,78.5,72.0,110.0,135.0,12.8,12.2,16.0,19.5,12.6,12.0,15.6,19.0,2100.0,2050.0,2400.0,2680.0,50,102400,10240
```

> vllm 若无 input 侧吞吐，对应列写 `N/A` 即可（与落盘脚本行为一致）。

#### 样例：`launch.txt`（sglang）

```text
# 服务启动命令；以 # 开头的行与空行会被忽略
# 可用反斜杠续行，最终会拼成一条命令

python -m sglang.launch_server \
  --model-path /models/GLM-4.5 \
  --tp-size 8 \
  --mem-fraction-static 0.85 \
  --enable-hierarchical-cache \
  --attention-backend flashinfer
```

#### 样例：`launch.txt`（vllm）

```text
# vllm 启动命令示例

vllm serve /models/Qwen2.5-72B \
  -tp 8 \
  --enable-expert-parallel \
  --gpu-memory-utilization 0.9 \
  --enable-prefix-caching
```

上传时表单可填：`framework=sglang`、`framework_version=0.4.6`、`model=GLM-4.5`、`model_version=distributed2`、`gpu_type=H20-141G`（与 txt 中命令对应即可）。

### 3. 部署服务

**方式 A：裸机运行**（Debian 12 等，无需 Docker）

```bash
bash scripts/install.sh   # 装依赖到系统 Python（Debian 需 --break-system-packages，脚本已处理）
bash scripts/start.sh     # 生成 config.yaml（首次）→ 数据库就绪性检查 → 拉起 scanner + api → 健康检查
bash scripts/status.sh    # 查看运行状态
bash scripts/stop.sh      # 停止
```

首次运行会从 `config.example.yaml` 自动生成 `config.yaml`，需要你编辑其中的
`llm.base_url`（LLM 端点）与 `scanner.benchmark_root`（NAS 挂载路径）后重新
`bash scripts/start.sh`。日志在 `var/log/`，运行时数据库与报告文件在 `var/data/`。

> SQLite 不是常驻的数据库服务，没有独立进程要"启动"——它是一个磁盘文件，
> Python 进程直接打开读写。`start.sh` 的第一步是「数据库就绪性检查」（确认
> 目录可写、能正常建表/打开该文件），而非启动数据库；随后依次拉起
> Scanner 和 API 两个进程，并轮询 `/api/health` 确认服务真正就绪。

**方式 B：Docker Compose**

```bash
cp config.example.yaml config.yaml   # 填写 llm 端点、NAS 路径（SQLite 零配置）
docker compose up -d                 # 起 scanner + api，数据库为共享卷上的 SQLite 单文件
```

前端访问：

- Chatbot：`http://<服务器>:8080/`
- 手工上传：`http://<服务器>:8080/upload`

## Excel 报告版式

报告为纯数据对比表（无图表、无 LLM 结论）：

- **行**：`(Input_Length, Concurrency)` 测试条件，按输入长度分块
- **列**：每个指标一组；组内按对比轴取值展开
- **双层表头**：第 1 行指标名（跨列合并），第 2 行对比轴取值
- **差异列**：对比轴恰好两个取值时，追加 `A vs B` = `(A - B) / B`（百分比）；否则不生成
- **块汇总**：每个 `Input_Length` 块末尾一行，汇总该块各差异列均值（红色加粗）

## 结构化启动参数

入库后可直接筛选/对比的维度与 `tools/param_map.py` 对齐，包括并行度（tp/pp/dp/dcp、ep_enabled/ep_width）、显存与 KV、调度、量化、投机解码、hicache 等。部分参数跨框架**量纲或类型不同**（如 `mem_fraction`、`ep_enabled` vs `ep_width`），配对表中有 `comparable` / `note` 说明，对比前请留意。

新增或调整参数时：同步改 `param_map.py` 与 `autores/db/schema.py`，并运行：

```bash
python tools/verify_param_map.py
```

## 依赖

```bash
pip install -r requirements.txt          # 生产（Python ≥ 3.11）
pip install -r requirements-dev.txt      # 含测试依赖
```

主要运行时依赖：FastAPI、uvicorn、openpyxl、openai、PyYAML、python-multipart。数据库为标准库 `sqlite3`，无需额外驱动。
