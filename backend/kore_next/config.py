from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else default.resolve()


@dataclass(frozen=True)
class Settings:
    """集中读取环境配置；数据目录与工作目录可分别覆盖。"""

    provider: str = os.getenv("KORENEXT_PROVIDER", "openai")
    model: str = os.getenv("KORENEXT_MODEL", "gpt-4.1-mini")
    api_key: str = os.getenv("KORENEXT_API_KEY", "")
    base_url: str = os.getenv("KORENEXT_BASE_URL", "https://api.openai.com/v1")
    search_provider: str = os.getenv("KORENEXT_SEARCH_PROVIDER", "auto")
    tavily_api_key: str = os.getenv("KORENEXT_TAVILY_API_KEY", "")
    http_allow_private_network: bool = os.getenv("KORENEXT_HTTP_ALLOW_PRIVATE_NETWORK", "false").lower() == "true"
    host: str = os.getenv("KORENEXT_HOST", "127.0.0.1")
    port: int = int(os.getenv("KORENEXT_PORT", "8765"))
    data_dir: Path = _path("KORENEXT_DATA_DIR", ROOT / ".koreagent-next")
    user_skills_dir: Path = _path("KORENEXT_USER_SKILLS_DIR", Path.home() / ".koreagent-next" / "skills")
    workspace_dir: Path = _path("KORENEXT_WORKSPACE_DIR", ROOT / "workspace")
    computer_enabled: bool = os.getenv("KORENEXT_COMPUTER_ENABLED", "false").lower() == "true"
    max_steps: int = int(os.getenv("KORENEXT_MAX_STEPS", "12"))
    run_warning_tokens: int = int(os.getenv("KORENEXT_RUN_WARNING_TOKENS", "80000"))
    run_finalization_tokens: int = int(os.getenv("KORENEXT_RUN_FINALIZATION_TOKENS", "100000"))
    run_hard_tokens: int = int(os.getenv("KORENEXT_RUN_HARD_TOKENS", "120000"))
    run_warning_model_calls: int = int(os.getenv("KORENEXT_RUN_WARNING_MODEL_CALLS", "16"))
    run_finalization_model_calls: int = int(os.getenv("KORENEXT_RUN_FINALIZATION_MODEL_CALLS", "20"))
    run_hard_model_calls: int = int(os.getenv("KORENEXT_RUN_HARD_MODEL_CALLS", "24"))

    @property
    def database(self) -> Path:
        return self.data_dir / "koreagent-next.db"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.workspace_dir,
            self.data_dir / "artifacts",
            self.data_dir / "memory",
            self.data_dir / "skills",
            self.data_dir / "screenshots",
            self.data_dir / "settings",
        ):
            path.mkdir(parents=True, exist_ok=True)
