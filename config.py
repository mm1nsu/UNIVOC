"""
API 키 / 모델 설정 관리
- 우선순위: 환경변수(GEMINI_API_KEY) > data/config.json 에 저장된 값
- 웹 UI(/admin)에서 키를 입력하면 data/config.json 에 저장됨 (매번 서버 재시작 안 해도 됨)
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "data" / "config.json"

DEFAULT_MODEL = "gemini-2.5-flash"


def _read_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_api_key() -> str | None:
    env_key = os.environ.get("GEMINI_API_KEY")
    if env_key:
        return env_key
    return _read_config().get("gemini_api_key")


def set_api_key(key: str) -> None:
    cfg = _read_config()
    cfg["gemini_api_key"] = key.strip()
    _write_config(cfg)


def get_model() -> str:
    env_model = os.environ.get("GEMINI_MODEL")
    if env_model:
        return env_model
    return _read_config().get("gemini_model", DEFAULT_MODEL)


def set_model(model: str) -> None:
    cfg = _read_config()
    cfg["gemini_model"] = model.strip()
    _write_config(cfg)


def has_api_key() -> bool:
    return bool(get_api_key())
