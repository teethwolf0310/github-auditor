"""加载配置：优先 config.local.yaml，回退 config.example.yaml；env 覆盖最关键字段。"""
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    github_token: str = ""
    proxy: str = ""
    max_requests_per_eval: int = 19
    max_repo_pages: int = 3
    max_event_pages: int = 3
    top_own_repos_sample: int = 3
    data_dir: str = ""
    raw_cache_dir: str = ""
    listen_host: str = "127.0.0.1"
    listen_port: int = 8010
    weights: dict = field(default_factory=lambda: {
        'authenticity': 35, 'activity': 20, 'capability': 20,
        'breadth': 10, 'longevity': 8, 'identity': 7,
    })

    def sqlite_path(self) -> Path:
        return Path(self.data_dir).expanduser() / "auditor.db"

    def raw_dir(self) -> Path:
        return Path(self.raw_cache_dir).expanduser()


def _load_yaml() -> dict:
    for name in ("config.local.yaml", "config.example.yaml"):
        p = ROOT / name
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    return {}


def _resolve_dir(p: str) -> str:
    """相对路径相对项目根解析；绝对 / ~ 展开。"""
    if not p:
        p = "./data"
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = str(ROOT / p)
    return p


def load_config(require_token: bool = True) -> Config:
    raw = _load_yaml()
    cfg = Config(
        github_token=os.getenv("GITHUB_TOKEN", raw.get("github_token", "")),
        proxy=os.getenv("GITHUB_PROXY", raw.get("proxy", "")),
        max_requests_per_eval=int(raw.get("max_requests_per_eval", 19)),
        max_repo_pages=int(raw.get("max_repo_pages", 3)),
        max_event_pages=int(raw.get("max_event_pages", 3)),
        top_own_repos_sample=int(raw.get("top_own_repos_sample", 3)),
        data_dir=_resolve_dir(raw.get("data_dir", "./data")),
        raw_cache_dir=_resolve_dir(raw.get("raw_cache_dir", "./data/raw")),
        listen_host=raw.get("listen_host", "127.0.0.1"),
        listen_port=int(raw.get("listen_port", 8010)),
        weights=raw.get("weights") or Config().weights,
    )
    # 校验权重和
    total = sum(cfg.weights.values())
    if total != 100:
        print(f"[warn] weights sum = {total}, 期望 100；总分仍会按当前权重计算。", file=sys.stderr)

    if require_token and not cfg.github_token:
        print(
            "[error] 缺少 github_token。请复制 config.example.yaml 为 config.local.yaml "
            "并填入 token，或 set GITHUB_TOKEN=...",
            file=sys.stderr,
        )
        sys.exit(2)
    return cfg
