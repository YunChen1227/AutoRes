"""配置加载：读取 config.yaml，并允许环境变量 AUTORES_<段>_<键> 覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

import yaml


@dataclass
class LLMConfig:
    # openai_compat：本地/自建 OpenAI 兼容端点；zhipu：智谱开放平台（zai-sdk）
    provider: str = "openai_compat"
    base_url: str = "http://localhost:8000/v1"
    api_key: str = "placeholder"
    model: str = "default"
    temperature: float = 0.1
    timeout_seconds: int = 60
    max_tool_rounds: int = 8
    max_tokens: int = 8192
    thinking_enabled: bool = False


@dataclass
class DatabaseConfig:
    path: str = "var/data/autores.db"   # SQLite 数据库文件路径（本地磁盘，勿放 NAS）


@dataclass
class ScannerConfig:
    benchmark_root: str = "/mnt/nas/benchmark_root"
    interval_seconds: int = 300
    dir_pattern: str = r"^\d{8}_\d{6}$"


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    # MCP 工具生成报告后返回的下载链接前缀（绝对地址）。
    # 留空时按 host:port 自动推导（0.0.0.0 → 127.0.0.1）。
    # 反向代理 / 容器场景可显式配置为对外可访问地址，如 https://autores.example.com
    public_base_url: str = ""
    # MCP Host 头白名单（代理/反代域名）。留空时：host=0.0.0.0 关闭校验；
    # 若配置了 public_base_url 则自动把其域名加入白名单。
    # 示例：["model-download.example.com:*", "127.0.0.1:*"]
    mcp_allowed_hosts: list[str] = field(default_factory=list)
    # 设为 true 可完全关闭 MCP 的 Host/Origin 校验（仅建议内网调试）
    mcp_disable_host_check: bool = False


@dataclass
class SessionConfig:
    ttl_minutes: int = 60
    max_messages: int = 40


@dataclass
class ReportConfig:
    output_dir: str = "var/data/reports"
    ttl_minutes: int = 120


_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "database": ("path",),
    "scanner": ("benchmark_root",),
    "report": ("output_dir",),
}


def _resolve_path(config_path: str, value: str) -> str:
    """相对路径相对于 config 文件所在目录解析，避免受进程 cwd 影响。"""
    if os.path.isabs(value):
        return os.path.normpath(value)
    base = os.path.dirname(os.path.abspath(config_path))
    return os.path.normpath(os.path.join(base, value))


def _resolve_paths(config_path: str, cfg: Config) -> None:
    for section_name, keys in _PATH_FIELDS.items():
        section = getattr(cfg, section_name)
        for key in keys:
            setattr(section, key, _resolve_path(config_path, getattr(section, key)))


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    config_path: str = "config.yaml"


_SECTION_TYPES = {
    "llm": LLMConfig,
    "database": DatabaseConfig,
    "scanner": ScannerConfig,
    "server": ServerConfig,
    "session": SessionConfig,
    "report": ReportConfig,
}


def _coerce(value: str, target_type: type) -> Any:
    """把环境变量字符串转换为目标字段类型。"""
    if target_type is bool:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    return value


def _apply_env_overrides(section_name: str, section_obj: Any) -> None:
    """用 AUTORES_<SECTION>_<KEY> 环境变量覆盖某个 section。"""
    for f in fields(section_obj):
        env_key = f"AUTORES_{section_name.upper()}_{f.name.upper()}"
        if env_key in os.environ:
            setattr(section_obj, f.name, _coerce(os.environ[env_key], f.type if isinstance(f.type, type) else type(getattr(section_obj, f.name))))


def load_config(path: str | None = None) -> Config:
    """
    加载配置。优先级：环境变量 > config.yaml > 默认值。
    path 缺省时按 AUTORES_CONFIG 环境变量或 ./config.yaml 查找。
    """
    path = path or os.environ.get("AUTORES_CONFIG", "config.yaml")
    config_path = os.path.abspath(path)

    raw: dict[str, Any] = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    cfg = Config()
    cfg.config_path = config_path
    for section_name, section_type in _SECTION_TYPES.items():
        section_raw = raw.get(section_name, {}) or {}
        # 只接受 dataclass 已声明的键，忽略多余键
        known = {f.name for f in fields(section_type)}
        filtered = {k: v for k, v in section_raw.items() if k in known}
        section_obj = section_type(**filtered)
        _apply_env_overrides(section_name, section_obj)
        setattr(cfg, section_name, section_obj)

    _resolve_paths(config_path, cfg)
    return cfg


# 便捷单例（首次调用时加载）
_cached: Config | None = None


def get_config(path: str | None = None) -> Config:
    global _cached
    if _cached is None or path is not None:
        _cached = load_config(path)
    return _cached
