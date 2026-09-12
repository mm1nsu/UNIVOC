"""
로그인한 학생의 대화 기록 저장 — 학번별로 여러 개의 "대화(탭)"를 파일로 관리한다.

data/chat_logs/{학번}/{대화ID}.json 하나가 탭 하나에 해당하며, 사람이 보는 메시지 목록뿐
아니라 다시 이어서 채팅칠 수 있도록 세션 상태(session_store.serialize_session 결과)까지
그대로 저장한다 — 서버가 재시작되거나 다른 탭을 보다가 돌아와도 그 대화의 진행 상황
(장학금 슬롯/후보, 복수전공 판정 단계 등)을 그대로 복원할 수 있음.

로그인 안 하고 쓰는 경우(게스트)는 이 모듈을 아예 호출하지 않음 — 지금까지와 동일하게
아무것도 파일로 안 남는 일회성 대화.

※ 이전 버전(학번당 파일 하나에 메시지만 append)과 저장 위치가 다름(파일 -> 폴더). 예전
data/chat_logs/{학번}.json 파일이 남아있어도 새 코드는 안 건드리고 그냥 무시함(사용 안 함).
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
CHAT_LOG_DIR = BASE_DIR / "data" / "chat_logs"

PREVIEW_MAX_LEN = 30


def new_conversation_id() -> str:
    return uuid.uuid4().hex[:12]


def _student_dir(student_id: str) -> Path:
    # student_id는 auth.validate_student_id로 이미 영문/숫자 4~20자만 검증된 값만 여기 들어옴
    # (app.py에서 로그인 성공한 사용자만 이 모듈을 호출함) — 경로 조작 위험 없음.
    return CHAT_LOG_DIR / student_id


def _conversation_path(student_id: str, conversation_id: str) -> Path:
    return _student_dir(student_id) / f"{conversation_id}.json"


def load_conversation(student_id: str, conversation_id: str) -> Optional[dict]:
    path = _conversation_path(student_id, conversation_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_conversations(student_id: str) -> list[dict]:
    """탭 목록(미리보기/최근수정순)에 쓸 메타데이터만 모아서 반환 — 메시지 본문/세션상태는
    여기 안 담음(탭 여러 개를 한 번에 내려줄 때 응답이 무거워지지 않게)."""
    d = _student_dir(student_id)
    if not d.exists():
        return []
    out = []
    for f in d.glob("*.json"):
        try:
            record = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append(
            {
                "id": record.get("id", f.stem),
                "preview": record.get("preview") or "(새 대화)",
                "mode": record.get("mode"),
                "message_count": len(record.get("messages") or []),
                "created_at": record.get("created_at"),
                "updated_at": record.get("updated_at"),
            }
        )
    out.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
    return out


def append_turn(
    student_id: str,
    conversation_id: str,
    *,
    student_msg: str,
    ai_msg: str,
    mode: Optional[str],
    state: dict,
) -> None:
    """학생 발화 + AI 응답 한 턴을 대화 파일에 이어붙이고, 다시 이어서 채팅칠 수 있도록
    현재 세션 상태(state = session_store.serialize_session(sess) 결과)로 덮어써서 저장한다."""
    path = _conversation_path(student_id, conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()

    record = load_conversation(student_id, conversation_id)
    if record is None:
        record = {
            "id": conversation_id,
            "created_at": now,
            "preview": student_msg[:PREVIEW_MAX_LEN],
            "messages": [],
        }

    record["messages"].append({"role": "student", "text": student_msg, "at": now})
    record["messages"].append({"role": "ai", "text": ai_msg, "at": now})
    record["mode"] = mode
    record["updated_at"] = now
    record["state"] = state
    if not record.get("preview"):
        record["preview"] = student_msg[:PREVIEW_MAX_LEN]

    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_conversation(student_id: str, conversation_id: str) -> None:
    path = _conversation_path(student_id, conversation_id)
    if path.exists():
        path.unlink()
