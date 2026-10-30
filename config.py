"""Configuration loaded from .env (next to this file) with environment overrides."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent

KEYS = (
    "API_BASE_URL", "API_KEY", "MODEL",
    "TELEGRAM_BOT_TOKEN", "ALLOWED_USER_ID",
    "WORKDIR", "GOAL_INTERVAL", "MAX_STEPS_PER_TASK", "DAILY_TOKEN_BUDGET",
    "PRICE_INPUT_PER_M", "PRICE_OUTPUT_PER_M",
    "APPROVAL_TIMEOUT", "SHELL_TIMEOUT", "LLM_TIMEOUT", "DB_PATH", "LOG_DIR",
    "DEFAULT_VERBOSE", "WEB_ENABLED", "WEB_HOST", "WEB_PORT", "WEB_TOKEN", "ACCESS", "APPROVALS",
)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    api_base_url: str
    api_key: str = field(repr=False)
    model: str
    workdir: Path
    telegram_bot_token: str = field(default="", repr=False)
    allowed_user_id: int = 0
    goal_interval_min: float = 30
    max_steps_per_task: int = 40
    daily_token_budget: int = 2_000_000
    price_input_per_m: float = 0.0
    price_output_per_m: float = 0.0
    approval_timeout: int = 900
    shell_timeout: int = 120
    llm_timeout: int = 180
    db_path: Path = PROJECT_DIR / "data" / "agent.db"
    log_dir: Path = PROJECT_DIR / "logs"
    default_verbose: str = "steps"
    web_enabled: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    web_token: str = field(default="", repr=False)   # empty -> generated and stored in the DB
    full_access: bool = False   # ACCESS=full: tools may touch any path on the machine
    approvals: bool = True      # APPROVALS=off: never ask, run every command

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.allowed_user_id)

    @property
    def secrets(self) -> list[str]:
        out = [s for s in (self.api_key, self.telegram_bot_token, self.web_token) if s]
        if ":" in self.telegram_bot_token:  # the secret half of "<bot id>:<secret>"
            out.append(self.telegram_bot_token.split(":", 1)[1])
        return out


def _raw(env_file: Path | None) -> dict[str, str]:
    path = env_file or PROJECT_DIR / ".env"
    values = {k: v for k, v in dotenv_values(path).items() if v is not None} if path.exists() else {}
    for k in KEYS:
        if k in os.environ:
            values[k] = os.environ[k]
    return values


def load_config(env_file: Path | None = None) -> Config:
    v = _raw(env_file)
    missing = [k for k in ("API_BASE_URL", "API_KEY", "MODEL", "WORKDIR") if not v.get(k)]
    if missing:
        raise ConfigError(f"missing required settings in .env: {', '.join(missing)}")
    token = (v.get("TELEGRAM_BOT_TOKEN") or "").strip()
    allowed = 0
    if token:  # Telegram is optional; if a token is set, the user id is required
        try:
            allowed = int(v.get("ALLOWED_USER_ID") or "")
        except ValueError as e:
            raise ConfigError("ALLOWED_USER_ID must be your numeric Telegram user id "
                              "(or leave TELEGRAM_BOT_TOKEN empty to disable Telegram)") from e

    workdir = Path(os.path.expanduser(v["WORKDIR"])).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    def p(key: str, default: Path) -> Path:
        raw = v.get(key)
        if not raw:
            return default
        path = Path(os.path.expanduser(raw))
        return path if path.is_absolute() else (PROJECT_DIR / path).resolve()

    verbose = v.get("DEFAULT_VERBOSE", "steps").strip().lower()
    if verbose not in ("off", "steps", "full"):
        verbose = "steps"

    return Config(
        api_base_url=v["API_BASE_URL"].rstrip("/"),
        api_key=v["API_KEY"],
        model=v["MODEL"],
        telegram_bot_token=token,
        allowed_user_id=allowed,
        workdir=workdir,
        goal_interval_min=float(v.get("GOAL_INTERVAL") or 30),
        max_steps_per_task=int(v.get("MAX_STEPS_PER_TASK") or 40),
        daily_token_budget=int(v.get("DAILY_TOKEN_BUDGET") or 2_000_000),
        price_input_per_m=float(v.get("PRICE_INPUT_PER_M") or 0),
        price_output_per_m=float(v.get("PRICE_OUTPUT_PER_M") or 0),
        approval_timeout=int(v.get("APPROVAL_TIMEOUT") or 900),
        shell_timeout=int(v.get("SHELL_TIMEOUT") or 120),
        llm_timeout=int(v.get("LLM_TIMEOUT") or 180),
        db_path=p("DB_PATH", PROJECT_DIR / "data" / "agent.db"),
        log_dir=p("LOG_DIR", PROJECT_DIR / "logs"),
        default_verbose=verbose,
        web_enabled=(v.get("WEB_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")),
        web_host=v.get("WEB_HOST") or "127.0.0.1",
        web_port=int(v.get("WEB_PORT") or 8765),
        web_token=(v.get("WEB_TOKEN") or "").strip(),
        full_access=(v.get("ACCESS", "workdir").strip().lower() == "full"),
        approvals=(v.get("APPROVALS", "on").strip().lower() not in ("0", "false", "no", "off")),
    )


def redact(text: str, secrets: list[str]) -> str:
    """Replace every secret value in text. Used on all logs and outgoing messages."""
    if not text:
        return text
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "***REDACTED***")
    return text
