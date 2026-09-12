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


# 실사용자 요청: "반려되는 순간 바로 알림, 컴퓨터 꺼도 받게" — 이메일 발송(Resend) 설정.
# Gemini 키와 완전히 같은 패턴: 환경변수(RESEND_API_KEY)가 최우선이고, 없으면
# data/config.json에 저장된 값을 씀. RESEND_FROM_EMAIL을 따로 안 정해주면 Resend가
# 도메인 인증 없이도 바로 쓸 수 있게 제공하는 기본 발신주소(onboarding@resend.dev)로 감 —
# 데모 단계에서 별도 도메인 인증 없이 바로 발송 테스트가 가능하게 하기 위함.
_DEFAULT_FROM_EMAIL = "Uni-VOC <onboarding@resend.dev>"


def get_resend_api_key() -> str | None:
    env_key = os.environ.get("RESEND_API_KEY")
    if env_key:
        return env_key
    return _read_config().get("resend_api_key")


def set_resend_api_key(key: str) -> None:
    cfg = _read_config()
    cfg["resend_api_key"] = key.strip()
    _write_config(cfg)


def get_notify_from_email() -> str:
    env_from = os.environ.get("RESEND_FROM_EMAIL")
    if env_from:
        return env_from
    return _read_config().get("resend_from_email", _DEFAULT_FROM_EMAIL)
