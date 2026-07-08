# AutoRes — 性能测试结果管理与报告 Agent

自动采集 sglang / vllm 的性能测试结果入库（SQLite），并通过 Web chatbot 用自然语言按需生成 Excel 对比报告。

完整设计见 [docs/design.md](docs/design.md)。

## 组成

- **落盘脚本** `to_csv.py`（也在 `tools/`）：测试人员本机运行，把 bench 输出整理为 `result.csv` + `metadata.json`，写入 NAS 时间戳目录。
- **Scanner**（`autores/scanner/`）：定时扫描 NAS，解析入库 SQLite。
- **API + 前端**（`autores/server/` + `frontend/index.html`）：LLM Agent 理解需求、确定性流水线生成 Excel、SSE 推送、前端下载。

## 快速开始

### 1. 测试人员落盘

```bash
python to_csv.py \
  --framework sglang --framework-version 0.4.6 \
  --input-dir ./bench_logs \
  --nas-dir /mnt/nas/benchmark_root \
  --gpu-type H20-141G \
  --model GLM-4.5 --model-version distributed2 \
  --launch-cmd "python -m sglang.launch_server --tp-size 8 --enable-hierarchical-cache"
```

vllm 场景需额外传 `--bench-cmd`（用于补 `--random-input-len`），且 bench 时须加
`--percentile-metrics ttft,tpot,itl,e2el` 才有 E2E 指标。详见 design.md §5.2。

### 2. 部署服务

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

前端访问 `http://<服务器>:8080/`。

## 依赖

`pip install -r requirements.txt`（Python ≥ 3.11）。
