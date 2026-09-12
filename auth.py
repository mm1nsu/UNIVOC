"""
로그인/회원가입 — 학번 + 비밀번호 기반 간단 인증.

비밀번호는 원문으로 저장하지 않고 PBKDF2-HMAC-SHA256(솔트 + 다중 라운드)로 해시해서
data/users.json에 저장한다. 외부 라이브러리 추가 없이 표준 라이브러리(hashlib/secrets)만 사용.

세션 토큰은 로그인 시 발급해서 메모리(SESSION_TOKENS)에만 들고 있음 — 이 프로젝트의 다른
세션 상태(app.py의 UNIFIED_SESSIONS)와 동일하게, 데모 단계에선 서버를 재시작하면 로그인도
풀림. 하지만 계정 정보와 대화 기록(chat_log.py)은 파일로 남아서 재시작해도 유지됨 —
"로그인이 풀리는 것"과 "기록이 사라지는 것"은 다른 문제라 안전하게 분리해둠. 다시 로그인만
하면 지난 기록은 그대로 다시 보임.
"""
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
USERS_PATH = BASE_DIR / "data" / "users.json"

# 학번은 보통 숫자지만 학교/과정에 따라 영문이 섞이는 경우도 있어서 영문+숫자 4~20자로 허용.
STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9]{4,20}$")
PBKDF2_ROUNDS = 260_000

# 로그인 세션 토큰: {token: student_id}. 데모 규모라 메모리로 충분함.
SESSION_TOKENS: dict[str, str] = {}


def _read_users() -> dict:
    if USERS_PATH.exists():
        try:
            return json.loads(USERS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write_users(data: dict) -> None:
    USERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    USERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _hash_password(password: str, salt: Optional[bytes] = None) -> tuple[str, str]:
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return digest.hex(), salt.hex()


def validate_student_id(student_id: str) -> Optional[str]:
    """형식이 이상하면 학생에게 보여줄 에러 메시지를, 문제없으면 None을 반환."""
    if not STUDENT_ID_RE.match(student_id or ""):
        return "학번은 영문/숫자 4~20자로 입력해줘."
    return None


def validate_password(password: str) -> Optional[str]:
    if not password or len(password) < 4:
        return "비밀번호는 4자 이상으로 설정해줘."
    return None


def register_user(student_id: str, password: str, name: Optional[str] = None) -> None:
    err = validate_student_id(student_id) or validate_password(password)
    if err:
        raise ValueError(err)
    users = _read_users()
    if student_id in users:
        raise ValueError("이미 등록된 학번이야. 로그인해줘.")
    pw_hash, salt = _hash_password(password)
    users[student_id] = {
        "student_id": student_id,
        "name": (name or "").strip() or None,
        "password_hash": pw_hash,
        "salt": salt,
    }
    _write_users(users)


def verify_user(student_id: str, password: str) -> Optional[dict]:
    """학번+비밀번호가 맞으면 {student_id, name}을, 아니면 None을 반환.
    학번이 없거나 비밀번호가 틀린 경우를 구분해서 알려주지 않음(계정 존재 여부 노출 방지)."""
    users = _read_users()
    user = users.get(student_id)
    if not user:
        return None
    expected, _ = _hash_password(password, bytes.fromhex(user["salt"]))
    if not secrets.compare_digest(expected, user["password_hash"]):
        return None
    return {"student_id": user["student_id"], "name": user.get("name")}


def create_session_token(student_id: str) -> str:
    token = secrets.token_urlsafe(32)
    SESSION_TOKENS[token] = student_id
    return token


def get_student_id_from_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    return SESSION_TOKENS.get(token)


def delete_session_token(token: Optional[str]) -> None:
    if token:
        SESSION_TOKENS.pop(token, None)


def get_user_name(student_id: str) -> Optional[str]:
    users = _read_users()
    user = users.get(student_id)
    return user.get("name") if user else None
