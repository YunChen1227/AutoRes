# AutoRes — 性能测试结果管理与报告 Agent

自动采集 sglang / vllm 的性能测试结果入库，并通过 Web chatbot 用自然语言按需生成 Excel 对比报告。

完整设计见 [docs/design.md](docs/design.md)。

## 组成

- **落盘脚本** `to_csv.py`（也在 `tools/`）：测试人员本机运行，把 bench 输出整理为 `result.csv` + `metadata.json`，写入 NAS 时间戳目录。
- **Scanner**（`autores/scanner/`）：定时扫描 NAS，解析入库 MongoDB。
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

```bash
cp config.example.yaml config.yaml   # 填写 llm 端点、mongo 连接串、NAS 路径
docker compose up -d                 # 起 scanner + api（MongoDB 用公司已有实例）
```

前端访问 `http://<服务器>:8080/`。

## 依赖

`pip install -r requirements.txt`（Python ≥ 3.11）。
