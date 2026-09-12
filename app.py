"""
웹 UI(브라우저 채팅창)로 실행하는 서버.
실행: python3 app.py  →  http://localhost:8000
"""
import json
import re
import threading
import urllib.parse
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import bot_core
import config
import curriculum
import notify
import recognition_doc
from matching import match_scholarships, major_clearly_matches
from rules import check_dual_major_eligibility
from schemas import (
    ChatLogEntry,
    CompletedCourseItem,
    DualMajorState,
    IncidentReport,
    IncidentUpdateIn,
    INCIDENT_CATEGORIES,
    RecognitionApplication,
    Scholarship,
    SlotState,
    StaffAccessLog,
    dual_major_missing_slots,
    missing_slots,
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "scholarship_db.json"
RECOGNITION_PATH = BASE_DIR / "data" / "recognition_applications.json"
INCIDENT_REPORTS_PATH = BASE_DIR / "data" / "incident_reports.json"
STAFF_ACCESS_LOGS_PATH = BASE_DIR / "data" / "staff_access_logs.json"
CHAT_LOGS_PATH = BASE_DIR / "data" / "chat_logs.json"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Uni-VOC 챗봇")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 세션별 대화 상태 (데모용 — 메모리 저장, 서버 재시작하면 초기화됨)
#
# 통합 챗봇 구조: 하나의 세션(하나의 대화 스레드) 안에서 장학금/복수전공 두 시나리오를
# 자연스럽게 오갈 수 있게 함 — 학생이 URL이나 탭을 안 골라도 됨. mode가 어떤 시나리오인지
# 가리키고, 각 시나리오의 세부 상태(state/stage/등)는 그대로 분리 보관해서 서로 안 섞임
# (예: 장학금 얘기하다 복수전공으로 넘어갔다가 다시 돌아와도 장학금 진행상황이 안 날아감).
UNIFIED_SESSIONS: dict[str, dict] = {}

# 동시요청 방지용 락 — 프론트에서 버튼을 안 잠가도(더블클릭/느린 응답 중 재전송 등) 같은
# 세션의 요청 두 개가 동시에 처리되면서 신청서가 2개 만들어지거나, 승인/반려 파일 저장이
# 서로 덮어쓰기 되는 문제를 막기 위함. 세션별 락(같은 학생의 중복 클릭)과, 신청서 파일
# 자체에 대한 전역 락(여러 세션/직원승인화면이 동시에 같은 JSON 파일을 읽고-고치고-쓰는
# 것 자체를 막음)을 따로 둔다. 락은 요청 처리 시간(대부분 수 ms~수백 ms) 동안만 잡혀
# 있으니 정상 사용에는 영향 없음 — 오직 "거의 동시에 들어온 중복 요청"만 순서대로 처리됨.
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()
RECOGNITION_APPS_LOCK = threading.Lock()
INCIDENT_REPORTS_LOCK = threading.Lock()
STAFF_ACCESS_LOGS_LOCK = threading.Lock()
CHAT_LOGS_LOCK = threading.Lock()


def _get_session_lock(session_id: str) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _SESSION_LOCKS[session_id] = lock
        return lock


# ---------------- DB 로드/저장 ----------------

def load_db() -> list[Scholarship]:
    if not DB_PATH.exists():
        return []
    raw = json.loads(DB_PATH.read_text(encoding="utf-8"))
    return [Scholarship.model_validate(r) for r in raw]


def save_db(db: list[Scholarship]) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.write_text(
        json.dumps([s.model_dump() for s in db], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_recognition_apps() -> list[RecognitionApplication]:
    if not RECOGNITION_PATH.exists():
        return []
    raw = json.loads(RECOGNITION_PATH.read_text(encoding="utf-8"))
    return [RecognitionApplication.model_validate(r) for r in raw]


def load_incident_reports() -> list[IncidentReport]:
    if not INCIDENT_REPORTS_PATH.exists():
        return []
    raw = json.loads(INCIDENT_REPORTS_PATH.read_text(encoding="utf-8"))
    return [IncidentReport.model_validate(r) for r in raw]


def save_incident_reports(reports: list[IncidentReport]) -> None:
    INCIDENT_REPORTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    INCIDENT_REPORTS_PATH.write_text(
        json.dumps([r.model_dump() for r in reports], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_recognition_apps(apps: list[RecognitionApplication]) -> None:
    RECOGNITION_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECOGNITION_PATH.write_text(
        json.dumps([a.model_dump() for a in apps], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_staff_access_logs() -> list[StaffAccessLog]:
    if not STAFF_ACCESS_LOGS_PATH.exists():
        return []
    raw = json.loads(STAFF_ACCESS_LOGS_PATH.read_text(encoding="utf-8"))
    return [StaffAccessLog.model_validate(r) for r in raw]


def save_staff_access_logs(logs: list[StaffAccessLog]) -> None:
    STAFF_ACCESS_LOGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STAFF_ACCESS_LOGS_PATH.write_text(
        json.dumps([r.model_dump() for r in logs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_chat_logs() -> list[ChatLogEntry]:
    if not CHAT_LOGS_PATH.exists():
        return []
    raw = json.loads(CHAT_LOGS_PATH.read_text(encoding="utf-8"))
    return [ChatLogEntry.model_validate(r) for r in raw]


def save_chat_logs(logs: list[ChatLogEntry]) -> None:
    CHAT_LOGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHAT_LOGS_PATH.write_text(
        json.dumps([r.model_dump() for r in logs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------- 세션 ----------------

def _new_scholarship_state() -> dict:
    return {
        "state": SlotState(),
        "stage": "slot_filling",  # slot_filling -> condition_check(조건있을때만) -> matched -> consult
        "matches": [],
        "selected": None,
        "needs_income_check": False,
        "pending_conditions": [],  # condition_check 단계에서 학생한테 직접 확인받을 특례조건 원문들
    }


def _new_dual_major_state() -> dict:
    return {
        "state": DualMajorState(),
        # slot_filling -> done -> awaiting_home_major -> course_collect
        # -> course_overlap_check(홈학과/목표학과 교육과정표에 겹치는데 학생이 말 안 한 과목이
        #    있으면 먼저 확인받는 단계 — 없으면 건너뜀, 실사용자 요청: "겹치는 과목 이미
        #    들은거같으면 미리 물어보고 신청서에 기재해줄 수 있도록")
        # -> course_semester_check(매칭된 과목 중 실제 이수 연도/학기를 모르는 게 있으면
        #    먼저 물어보는 단계 — 없으면 건너뜀, 실사용자 요청: "이수학기도 채워주면 좋겠는데")
        # -> course_draft
        # -> student_info(성명/학번/학년/소속대학 확인 — 신청서에 실제로 필요한 정보라 빠지면
        #    신청서로서 의미가 없음, 실사용자 피드백으로 추가됨) -> student_info_confirm(최종
        #    확인 — 잘못 입력했으면 다시 고칠 기회를 준다, 이것도 실사용자 요청으로 추가됨) -> done
        # done 단계에서는 매 턴마다, 방금 제출한 신청서가 (a) 어딘가에서 반려됐는데 아직 학생한테
        # 안 알려줬으면 rejection_followup으로 전환해서 반려 사유를 알려주고 수정 의향을 묻고,
        # (b) 학생이 "파일/다운로드/양식"을 다시 달라고 하면 다운로드 탭을 다시 띄워준다
        # (실사용자 요청: "반려되면 학생한테 알려줘서 일부수정해서 다시 제출... 수정제출할때
        # 학생한테 수정파일이 안 보이는데... 파일 보여달라고하면 다운로드하는 탭을 보여주면").
        # rejection_followup에서 "응"으로 답하면 course_collect로 돌아가되, 예전에 매칭됐던
        # 과목들을 그대로 들고 가서(완전히 처음부터 다시 말 안 해도 되게) 이어서 빼거나
        # 더할 수 있게 한다.
        "stage": "slot_filling",
        "result": None,
        "completed_courses": [],  # list[CompletedCourseItem] — 2단계(이수과목 인정) 수집용
        "matched": [],
        "unmatched": [],
        "overlap_candidates": [],  # course_overlap_check 단계에서 제시한 후보(list[dict])
        "semester_pending": [],  # course_semester_check 단계에서 물어본, 아직 이수시점 모르는 과목명
        "last_application_id": None,
        "rejection_notified": False,  # last_application_id가 반려됐다는 걸 학생한테 이미 알렸는지
        "student_identity": {"name": None, "student_id": None, "grade": None, "college": None, "email": None},
    }


def _new_unified_session() -> dict:
    return {
        "mode": None,  # None(아직 파악 안 됨) | "scholarship" | "dual_major"
        "history": [],  # 두 시나리오가 공유하는 대화 이력 (모드 전환해도 맥락 유지)
        "scholarship": _new_scholarship_state(),
        "dual_major": _new_dual_major_state(),
        # 학번 기반 세션 복원("로그인" 없이 이전 신청 내역 이어받기) 대기 플래그 — 아래
        # LOOKUP_TRIGGER_RE 참고.
        "awaiting_lookup_id": False,
        # 캠퍼스 안전신고 1차 감지 직후, 112 지령실처럼 위치/인원수/이름·연락처를 몇 차례
        # 더 캐묻는 동안 잠깐 들고 있는 상태. None이면 대기 중 아님. 아래
        # `_try_handle_incident_report`/`_handle_incident_followup_message` 참고 —
        # sess["mode"]와는 완전히 독립적이라 진행 중이던 장학금/복수전공 상담을 절대
        # 건드리지 않는다.
        "pending_incident": None,
    }


def get_unified_session(session_id: str) -> dict:
    if session_id not in UNIFIED_SESSIONS:
        UNIFIED_SESSIONS[session_id] = _new_unified_session()
    return UNIFIED_SESSIONS[session_id]


# ---------------- 페이지 ----------------

@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/admin")
def admin():
    return FileResponse(str(STATIC_DIR / "admin.html"))


@app.get("/dualmajor")
def dualmajor_page():
    # 예전엔 복수전공 전용 페이지가 따로 있었는데, 이제 메인 챗봇 하나로 통합됨.
    # 예전 링크(북마크 등)가 죽지 않게 메인으로 보내줌.
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/")


@app.get("/staff")
def staff_page():
    return FileResponse(str(STATIC_DIR / "staff.html"))


@app.get("/report")
def incident_report_page():
    # 실사용자 요청: "독립된 간단 신고 폼" — 챗봇 흐름과 완전히 분리된 별도 페이지.
    return FileResponse(str(STATIC_DIR / "report.html"))


@app.get("/incidents")
def incidents_dashboard_page():
    # 실사용자 요청: "사이트 하나를 만들어서 탭 별로 보안/시설/기타 누르면 볼 수 있게" —
    # 보안팀/행정팀이 보는 화면이라 staff.html과 마찬가지로 전부 존댓말(합쇼체)로 작성됨.
    return FileResponse(str(STATIC_DIR / "incidents.html"))


# ---------------- 버전(마지막 업데이트 시각) ----------------
# 실사용자 요청: "몇시몇분에 업데이트한 버전인지 웹에서 볼 수 있게 해줘" — 로컬에서 테스트
# 중인 코드랑 Render에 배포된 코드가 서로 다른 시점일 수 있어서, 지금 화면에 뜬 게 정확히
# 언제 반영된 코드인지 데모 중에도 바로 확인할 수 있게 함. 서버가 돌아가는 컴퓨터의 시스템
# 시간대가 뭐든(로컬 PC는 KST, Render는 보통 UTC) 항상 한국시간(KST)으로 통일해서 보여줌 —
# 안 그러면 로컬이랑 Render 화면에 서로 다른 시간이 찍혀서 더 헷갈릴 수 있음.
_VERSION_FILES = [
    "app.py",
    "bot_core.py",
    "matching.py",
    "schemas.py",
    "rules.py",
    "curriculum.py",
    "static/index.html",
    "static/staff.html",
    "static/report.html",
    "static/incidents.html",
]


# UptimeRobot 같은 외부 감시 서비스가 Render 무료 플랜의 "슬립" 방지용으로 주기적으로
# 두드리는 헬스체크 엔드포인트. 감시 서비스마다 GET/HEAD/POST 등 실제로 보내는 HTTP
# 메서드가 다를 수 있어서(예: "405 Method Not Allowed" — 그 메서드를 허용 안 해서 남),
# "/"(GET만 허용)처럼 특정 메서드만 받지 않고 흔히 쓰이는 세 가지를 전부 허용해둠.
@app.api_route("/healthz", methods=["GET", "HEAD", "POST"])
def healthz():
    return {"status": "ok"}


@app.get("/api/version")
def get_version():
    mtimes = []
    for rel in _VERSION_FILES:
        p = BASE_DIR / rel
        if p.exists():
            mtimes.append(p.stat().st_mtime)
    if not mtimes:
        return {"last_updated": "확인 불가"}
    latest = datetime.fromtimestamp(max(mtimes), tz=ZoneInfo("Asia/Seoul"))
    return {"last_updated": latest.strftime("%Y-%m-%d %H:%M")}


# ---------------- 설정(API 키) ----------------

@app.get("/api/settings")
def get_settings():
    return {"has_api_key": config.has_api_key(), "model": config.get_model()}


class SettingsIn(BaseModel):
    api_key: Optional[str] = None
    model: Optional[str] = None


@app.post("/api/settings")
def set_settings(body: SettingsIn):
    if body.api_key:
        config.set_api_key(body.api_key)
    if body.model:
        config.set_model(body.model)
    return {"ok": True, "has_api_key": config.has_api_key(), "model": config.get_model()}


# ---------------- 장학금 DB 조회/삭제 (관리자 페이지용) ----------------

@app.get("/api/scholarships")
def list_scholarships():
    db = load_db()
    return [s.model_dump() for s in db]


@app.delete("/api/scholarships/{sid}")
def delete_scholarship(sid: str):
    db = load_db()
    new_db = [s for s in db if s.id != sid]
    save_db(new_db)
    return {"ok": True, "count": len(new_db)}


# ---------------- RAG 자료 추가 (공지 원문 → 구조화 → 저장) ----------------

class IngestParseIn(BaseModel):
    raw_text: str


@app.post("/api/ingest/parse")
def ingest_parse(body: IngestParseIn):
    try:
        parsed = bot_core.extract_scholarship(body.raw_text)
    except Exception as e:  # noqa: BLE001 — 데모용으로 에러 메시지 그대로 노출
        return JSONResponse(status_code=400, content={"error": str(e)})
    return parsed.model_dump()


class IngestSaveIn(BaseModel):
    record: dict


@app.post("/api/ingest/save")
def ingest_save(body: IngestSaveIn):
    try:
        record = Scholarship.model_validate(body.record)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": f"형식이 올바르지 않아요: {e}"})
    record.id = record.id or uuid.uuid4().hex[:12]
    db = load_db()
    db.append(record)
    save_db(db)
    return {"ok": True, "id": record.id, "count": len(db)}


# ---------------- 채팅 (통합) ----------------
#
# 장학금/복수전공 두 시나리오를 한 채팅창에서 자연스럽게 오갈 수 있게 하는 라우터.
# 1) 모드가 아직 안 정해졌으면: 키워드로 먼저 판단 -> 애매하면 LLM으로 분류 -> 그래도
#    애매하면(unclear) 절대 추측하지 않고 학생에게 직접 물어봄.
# 2) 모드가 이미 정해졌어도, 메시지에 다른 시나리오 키워드가 명확히 나오면 그쪽으로
#    전환함 (예: 장학금 얘기하다가 "복수전공도 되나?" -> 자동 전환). 각 시나리오의
#    진행상황은 따로 보관되니 전환했다 돌아와도 안 날아감.
# 3) 실제 판단(장학금 매칭/자격요건 판정/이수과목 인정)은 예전과 동일하게 전부
#    결정론적 규칙(matching.py/rules.py/curriculum.py)이 담당 — 라우팅이 늘었다고
#    핵심 설계 원칙(LLM은 판단 안 함)이 바뀌는 건 아님.

class ChatIn(BaseModel):
    session_id: Optional[str] = None
    message: str


RESET_WORDS = {"처음부터", "다시검색", "리셋", "reset"}
LIST_WORDS = {"목록", "리스트", "list"}
EXIT_WORDS = {"종료", "exit", "quit"}

# 실사용자 요청: "내가 여태 구현해놓은 기능들을 도움말?형태로 해서 볼 수 있도록 하면
# 좋겠어. 사용자가 어떤 기능이 있는지 볼 수 있도록." — 학생이 지금 챗봇으로 뭘 할 수
# 있는지 스스로 발견할 방법이 없었음(퀵바 버튼도 모드 진입 전엔 대부분 숨겨져 있음).
# RESET/EXIT처럼 LLM 호출 없이 고정 문구로 즉답하되, RESET과 달리 지금 진행 중인
# 대화(mode/scholarship/dual_major/pending_incident 등 상태)는 전혀 건드리지 않음 —
# 상담이나 신고 접수 중간에 "도움말"이라고 물어봐도 답만 해주고 하던 흐름은 그대로
# 이어갈 수 있게 하기 위함. 버튼(quickbar의 "도움말", data-cmd="도움말")으로 누르면
# 정확히 매칭되고, 자유 타이핑으로도 대표적인 표현은 잡히도록 키워드도 같이 둔다.
HELP_EXACT_WORDS = {"도움말", "도움", "help", "사용법", "명령어"}
HELP_KEYWORD_PHRASES = (
    "뭐 할 수 있어", "뭘 할 수 있어", "뭐할수있어", "뭘할수있어",
    "무슨 기능", "어떤 기능", "무슨기능", "어떤기능", "기능이 뭐", "기능이뭐",
    "뭐가 가능", "뭐가가능", "사용법",
)


def _looks_like_help_request(msg: str) -> bool:
    stripped = msg.strip()
    if stripped in HELP_EXACT_WORDS:
        return True
    return any(p in stripped for p in HELP_KEYWORD_PHRASES)


HELP_MESSAGE = (
    "내가 할 수 있는 거 정리해줄게!\n\n"
    "1. 장학금 상담 — 학년, 성적, 소득분위 같은 조건 물어보면서 너한테 맞는 장학금 찾아줘. "
    "\"장학금\"이라고 말하면 시작돼.\n\n"
    "2. 복수전공/이수과목 인정신청 — 복수전공·부전공·다전공 관련 자격 확인부터, 이미 들은 "
    "과목을 인정받는 신청서 작성까지 도와줘. 신청서 내면 직원 승인 거쳐서 결과 나오고, "
    "학번만 알려주면 저번에 낸 신청 상태(반려됐는지 등)도 바로 조회해줄 수 있어. "
    "\"복수전공\" 또는 \"이수과목\"이라고 말하면 시작돼.\n\n"
    "3. 캠퍼스 안전신고 — 위험한 상황이나 시설 고장(화재, 사고, 수상한 사람, 에어컨/난방/"
    "와이파이 고장 등) 그냥 편하게 톡하듯 말해주면 바로 접수돼. 필요하면 장소나 인원수 같은 "
    "걸 한두 번 더 물어볼 수 있는데, \"그만\"이라고 하면 언제든 그 시점까지 내용으로 바로 "
    "접수 끝낼 수 있어. 이름·연락처도 \"익명으로 할래\"라고 하면 안 남겨.\n\n"
    "그 외에 \"처음부터\"라고 하면 지금 하던 거 리셋하고 새로 시작하고, \"종료\"라고 하면 "
    "대화 끝낼 수 있어. 편하게 아무거나 말 걸어봐!"
)

# 실사용자 요청: "로그인해서 대화내용 기억하고, 이수과목 인정신청서 반려되면 먼저 알려주면
# 좋겠다". 진짜 회원가입/로그인(비번 저장·보안 처리 등)은 데모 코앞에 손대기엔 작업량·리스크가
# 크고, 이 앱 세션은 애초에 브라우저 탭 하나에 sessionStorage로 묶여있어서(새로고침엔
# 살아남지만 탭을 닫으면 사라짐 — static/index.html 참고) 반려 여부가 나오는 데 며칠 걸리는
# 이 기능 특성상 "같은 세션으로 돌아와야만 알림이 뜨는" 지금 방식(app.py의 (0) 자동체크)은
# 사실상 거의 발동을 못 함. 그렇다고 정식 로그인 시스템 없이도, 신청서 제출 때 이미 받아둔
# 학번(student_id, RecognitionApplication에 이미 저장돼 있음)으로 "이어하기"만 붙이면 같은
# 효과를 훨씬 가볍게 낼 수 있음 — 그래서 비번 없는 "학번 기반 세션 복원"으로 구현함.
_LOOKUP_TRIGGER_RE = re.compile(r"(신청\s*(확인|조회|결과)|반려\s*(됐|되)|승인\s*(됐|됐나|여부))")
_LOOKUP_STUDENT_ID_RE = re.compile(r"\d{6,}")

SCHOLARSHIP_KEYWORDS = ("장학금", "장학", "학자금")
DUAL_MAJOR_KEYWORDS = ("복수전공", "복전", "다전공", "부전공", "이수과목")


def detect_mode_keywords(msg: str) -> Optional[str]:
    if any(k in msg for k in DUAL_MAJOR_KEYWORDS):
        return "dual_major"
    if any(k in msg for k in SCHOLARSHIP_KEYWORDS):
        return "scholarship"
    return None


# 실사용자 요청: "안전신고 탭을 안 들어가고 메인 챗봇에서 신고할 수 있도록" — 처음엔
# 완전히 독립된 /report 폼만 만들었는데, "각 잡고 폼을 만들면 오히려 신고율이 떨어지지
# 않냐, 카톡하듯이 편하게 던지는 게 낫지 않냐"는 재지적을 받고 추가함. 매 메시지마다
# LLM을 불러서 "이거 신고인가?"를 판단하면 모든 상담 턴의 응답속도·비용이 늘어나므로,
# 이 키워드 목록으로 값싸게 1차로 거른 메시지에 대해서만 LLM 최종확인(extract_incident_report)
# 을 호출함. 완전히 무관한 단어로만 이루어진, 목록에 없는 표현의 암묵적 신고는(예: 아래
# 어떤 단어도 안 들어간 창의적인 문장) 놓칠 수 있음 — 이건 "모든 턴마다 LLM 호출" 비용과
# 맞바꾼 의도적인 트레이드오프. 대신 대화 첫 메시지(모드 미정 상태)는 classify_intent가
# 이미 LLM을 호출하는 지점이라 거기서도 "incident_report"로 분류되면 한 번 더 커버함.
# 실사용자 질문: "어떤건 건의사항으로 보내고 어떤건 안 보내는거야?" — "에어컨 언제트냐
# 민원좀 넣어줘" 메시지가 신고로 안 잡힌 게 이 목록에 에어컨/난방 관련 단어가 아예
# 없었기 때문이었음(장소/사건류 단어 위주였음). 흔한 시설 장비 이름 몇 개를 추가해서
# 커버리지를 넓힘 — 그래도 최종 판단은 항상 LLM(extract_incident_report)이 한 번 더
# 하니까 "에어컨이 좋다/나쁘다" 같은 무관한 잡담까지 신고로 잘못 접수될 걱정은 없음.
INCIDENT_TRIGGER_KEYWORDS = (
    "신고", "제보",
    "위험해", "위험한", "위험함", "수상한", "수상해",
    "사고", "화재", "불났", "불나서", "다쳤", "다친", "쓰러져",
    "고장", "누수", "정전", "파손", "깨졌", "부서졌",
    "도둑", "절도", "폭행", "치한", "성희롱",
    "신천지", "포교", "전도",
    "에어컨", "히터", "난방", "냉방", "와이파이", "인터넷", "엘리베이터", "엘베", "정수기",
)

# 실사용자 요청: "112신고하면 어디에 칼부림났어요하면 계속 물어보잖아 그런걸 모델로해서
# 뭔가 계속 물어보면 좋겠어" — 위치/인원수를 못 들었으면 이 횟수만큼만 더 캐묻고
# (opening 질문 포함), 그래도 안 채워지면 넘어감. 무한정 캐물으면 오히려 짜증나므로
# 상한선을 둠 — "언제든 그만 말하면 바로 접수"라는 안내와 함께라 부담은 적음.
INCIDENT_FOLLOWUP_MAX_DETAIL_ROUNDS = 2


def _looks_like_incident(msg: str) -> bool:
    return any(k in msg for k in INCIDENT_TRIGGER_KEYWORDS)


def _try_handle_incident_report(sess: dict, user_msg: str) -> Optional[dict]:
    """user_msg가 실제로 캠퍼스 안전/시설 신고인지 LLM(bot_core.extract_incident_report)로
    최종 확인한다. 신고가 아니라고 판단되거나(오탐) LLM 호출 자체가 실패하면 None을
    반환해서 호출부가 원래 하던 대로(장학금/복수전공 상담 등) 계속 처리하게 한다 —
    신고 오판정이 진행 중이던 상담을 끊어버리면 안 되니까 항상 "확실할 때만 가로채기" 원칙.

    진짜 신고로 확인되면 위치/인원수/이름·연락처가 다 안 갖춰졌어도 지금까지 아는
    정보만으로 즉시 신고를 저장부터 해서 직원 화면에 바로 뜨게 하고, 그 다음 112
    지령실처럼 부족한 정보를 몇 차례 더 캐물으면서 답이 들어올 때마다 이미 떠 있는
    그 신고를 실시간으로 갱신한다(실사용자 요청: "112신고하면 어디에 칼부림났어요하면
    계속 물어보잖아 그런걸 모델로해서 뭔가 계속 물어보면 좋겠어. 대신 언제든 신고를
    끝낼 수는 잇도록 안내하고, 개인정보 익명처리하고 싶다하면 그렇게 할 수 있도록" +
    이후 "'신고좀'이라고 했을 때 직원용페이지에는 일단 올려놓고 그뒤로 업데이트되는
    정보들을 직원용페이지에 업데이트하는건 어때?" — 응급 상황일수록 접수 자체를 뒤로
    미루면 안 되고, 후속 답변은 이미 접수된 건을 보강하는 것이어야 한다는 취지).
    다음 단계 진행/질문 문구는 `_incident_next_action`이 만들고, 학생의 답변을 해석해서
    저장된 신고를 갱신하는 건 이어지는 메시지마다 `_handle_incident_followup_message`가
    처리한다."""
    try:
        extraction = bot_core.extract_incident_report(user_msg)
    except Exception:  # noqa: BLE001 — LLM 실패 시 신고 아님으로 안전하게 처리, 원래 흐름 계속
        return None
    if not extraction.is_incident_report or not extraction.description.strip():
        return None

    category = extraction.category if extraction.category in INCIDENT_CATEGORIES else "기타"
    location = (extraction.location or "").strip() or None
    people_count = (extraction.people_count or "").strip() or None
    description = extraction.description.strip()

    # 여기서 바로 저장 — 후속 질문에 학생이 답을 안 하거나 도중에 나가버려도 최소한
    # 지금까지 확보한 내용은 이미 직원 화면에 올라가 있게 된다.
    report = _save_new_incident_report(
        category=category,
        description=description,
        location=location,
        people_count=people_count,
        reporter_name=None,
        reporter_contact=None,
    )
    pending = {
        "report_id": report.id,
        "category": category,
        "description": description,
        "location": location,
        "people_count": people_count,
        "reporter_name": None,
        "reporter_contact": None,
        "anonymous": False,
        "stage": "details",  # "details"(위치/인원수 캐묻는 중) -> "contact"(이름/연락처 물어보는 중)
        "detail_rounds": 0,
        "contact_asked": False,
    }
    sess["pending_incident"] = pending

    question = _incident_next_action(pending)
    if question is None:
        # 이론상 도달 안 함(방금 만든 pending은 contact_asked=False라 항상 물어볼 게 있음) —
        # 그래도 혹시 모를 상황을 대비해 안전하게 즉시 마무리.
        return _finalize_incident(sess, early=False)
    return {"reply": question, "stage": "incident_pending_followup", "options": [], "incident_report_id": report.id}


def _incident_next_action(pending: dict) -> Optional[str]:
    """pending(sess["pending_incident"]) 상태를 보고 다음에 물어볼 질문 문구를 만든다.
    더 물어볼 게 있으면 그 질문을 반환하면서 pending의 stage/detail_rounds/contact_asked를
    그만큼 전진시키고, 더 물을 게 없으면(바로 접수해야 하면) None을 반환한다 — 호출부는
    None을 받으면 `_finalize_incident`를 불러야 한다.

    단계는 "details"(위치/인원수, INCIDENT_FOLLOWUP_MAX_DETAIL_ROUNDS번까지만 캐묻음)
    → "contact"(이름/연락처, 딱 한 번만 물어봄, 익명 요청 시 건너뜀) 순서 — 112 지령실이
    상황부터 확인하고 신원은 나중에 묻는 순서를 그대로 따름.

    실사용자 리포트: "공유기 고장낫어 의과대학 2층 와이파이"라고 신고했는데 "몇 명 정도
    있어?"라고 물어봐서 뜬금없었음 — 인원수는 보안 신고(수상한 사람이 몇 명인지, 다친
    사람이 몇 명인지 등)에서나 의미가 있지, 와이파이/에어컨/엘리베이터 같은 시설 고장
    신고에는 "관련 인원"이라는 개념 자체가 안 맞음. 그래서 카테고리별로 물어볼 가치가
    있는 필드 자체를 다르게 둠 — 시설/기타 신고는 위치만 캐묻고, 보안 신고에서만
    인원수까지 캐묻는다(학생이 먼저 알아서 인원수를 언급했다면 category와 무관하게
    그대로 저장은 됨 — 여기서 막는 건 "먼저 나서서 캐묻는 질문"뿐)."""
    if pending["anonymous"]:
        # 실사용자 요청: "개인정보 익명처리하고 싶다하면 그렇게 할 수 있도록" — 익명 요청은
        # 대화 어느 시점에 나오든 그 뒤로 이름/연락처는 다시는 안 물어봄.
        pending["contact_asked"] = True

    if pending["stage"] == "details":
        relevant_fields = ("location", "people_count") if pending["category"] == "보안" else ("location",)
        missing = [f for f in relevant_fields if not pending.get(f)]
        if missing and pending["detail_rounds"] < INCIDENT_FOLLOWUP_MAX_DETAIL_ROUNDS:
            pending["detail_rounds"] += 1
            bits = []
            if "location" in missing:
                bits.append("정확히 어디서 그런 거야?")
            if "people_count" in missing:
                bits.append("몇 명 정도 있어?")
            # 실사용자 요청: "언제든 신고를 끝낼 수는 잇도록 안내" — 캐물을 때마다 매번 상기시켜줌.
            # "신고 접수됐어"로 시작 — 지금 이 순간 이미 직원 화면에 올라가 있는 상태이지,
            # 후속 질문이 끝나야 비로소 접수되는 게 아니라는 걸 분명히 함.
            return "신고 접수됐어! 담당팀 화면에 바로 올라갔어. " + " ".join(bits) + " (언제든 '그만'이라고 하면 지금까지 내용으로 마무리할게!)"
        pending["stage"] = "contact"

    # stage == "contact"
    if not pending["contact_asked"]:
        pending["contact_asked"] = True
        return "마지막으로 이름이나 연락처 남겨줄 수 있어? 상황 파악하는 데 도움 돼! 말하기 싫으면 '익명으로 할래'라고 하거나 그냥 넘어가도 접수는 돼."
    return None


def _handle_incident_followup_message(sess: dict, user_msg: str) -> dict:
    """`_try_handle_incident_report`/`_incident_next_action`이 던진 후속 질문에 대한
    학생의 답변(user_msg)을 해석해서 sess["pending_incident"]에 반영하고, 새로 알게 된
    정보는 그 즉시 `_sync_incident_fields`로 이미 직원 화면에 떠 있는 신고 건에도
    반영한다(실사용자 요청: "'신고좀'이라고 했을 때 직원용페이지에는 일단 올려놓고
    그뒤로 업데이트되는 정보들을 직원용페이지에 업데이트하는건 어때?"). 그 다음 계속
    캐물을지(`_incident_next_action`이 다음 질문을 만듦) 아니면 여기서 마무리할지
    (`_finalize_incident`) 판단한다. LLM 호출(extract_incident_followup)이 실패해도
    그냥 다음 단계로 넘어갈 뿐 신고 자체가 무산되진 않음 — 후속 질문은 어디까지나
    "있으면 좋은" 보강 정보라 실패했다고 이미 확보한 신고를 날려버리면 안 된다."""
    pending = sess["pending_incident"]
    wants_to_finish = False
    updates: dict = {}
    try:
        fu = bot_core.extract_incident_followup(user_msg)
        if fu.location:
            pending["location"] = fu.location.strip()
            updates["location"] = pending["location"]
        if fu.people_count:
            pending["people_count"] = fu.people_count.strip()
            updates["people_count"] = pending["people_count"]
        if fu.extra_detail and fu.extra_detail.strip():
            pending["description"] = (pending["description"] + " " + fu.extra_detail.strip()).strip()
            updates["description"] = pending["description"]
        if fu.reporter_name:
            pending["reporter_name"] = fu.reporter_name.strip()
            updates["reporter_name"] = pending["reporter_name"]
        if fu.reporter_contact:
            pending["reporter_contact"] = fu.reporter_contact.strip()
            updates["reporter_contact"] = pending["reporter_contact"]
        if fu.wants_anonymous:
            # 실사용자 요청대로 익명 요청은 그 순간부터 확실히 반영 — 혹시 이전에 이름/
            # 연락처를 흘렸어도(예: 먼저 이름 말했다가 마음이 바뀐 경우), 이미 직원 화면에
            # 올라간 값까지 포함해서 지워버림.
            pending["anonymous"] = True
            pending["reporter_name"] = None
            pending["reporter_contact"] = None
            updates["reporter_name"] = None
            updates["reporter_contact"] = None
        wants_to_finish = fu.wants_to_finish
    except Exception:  # noqa: BLE001 — 해석 실패해도 진행은 계속(질문을 다시 던지거나 다음 단계로)
        pass

    if updates:
        _sync_incident_fields(pending["report_id"], **updates)

    if wants_to_finish:
        # 실사용자 요청: "언제든 신고를 끝낼 수는 잇도록" — 지금까지 모은 정보로 바로 마무리.
        return _finalize_incident(sess, early=True)

    question = _incident_next_action(pending)
    if question is None:
        return _finalize_incident(sess, early=False)
    return {
        "reply": question,
        "stage": "incident_pending_followup",
        "options": [],
        "incident_report_id": pending["report_id"],
    }


def _finalize_incident(sess: dict, early: bool) -> dict:
    """신고는 `_try_handle_incident_report`에서 이미 저장되고 이후 답변마다
    `_sync_incident_fields`로 계속 갱신돼 있으므로, 여기서는 새로 저장할 게 없고
    sess["pending_incident"]만 정리하고 마무리 안내만 돌려주면 된다. early=True는
    학생이 "그만"으로 후속 질문을 중간에 끊은 경우(안내 문구를 조금 다르게 함),
    False는 정상적으로 details→contact 단계를 다 거친 경우."""
    pending = sess["pending_incident"]
    sess["pending_incident"] = None
    # 학생용 챗봇이라 100% 반말 유지 — 존댓말 규칙은 이메일/직원화면(staff.html/incidents.html)에만
    # 적용된다는 기존 원칙 그대로.
    if early:
        reply = (
            f"알겠어, 지금까지 말해준 내용으로 마무리할게! ({pending['category']}) "
            "신고는 처음 말했을 때부터 이미 담당팀 화면에 올라가 있었어 — 알려줘서 고마워. "
            "하던 얘기 있으면 이어서 계속해도 돼!"
        )
    else:
        reply = (
            f"신고 접수 다 됐어! ({pending['category']}) 처음 말해줬을 때부터 담당팀 화면에 올라가 있었고, "
            "방금까지 알려준 내용도 다 반영해놨어 — 알려줘서 고마워. 하던 얘기 있으면 이어서 계속해도 돼!"
        )
    return {
        "reply": reply,
        "stage": "incident_reported",
        "options": [],
        "incident_report_id": pending["report_id"],
    }


def filter_by_soft_conditions(
    matches: list[tuple[Scholarship, bool]], state: SlotState, convo: str
) -> list[tuple[Scholarship, bool]]:
    """1차 규칙필터를 통과한 후보 중, special_conditions/major_restriction/학년조건처럼
    자유서술형이라 정확매칭이 불가능한 조건이 있는 것만 골라 LLM 보조판단(2차)을 한 번 거침.
    (matching.py 모듈 docstring 참고 — special_conditions는 실제 DB 41건 전부에 있어서
    이걸 예전처럼 정확매칭으로 걸렀더니 있는 장학금도 다 안 뜨는 버그가 있었음.)
    condition_check 단계(1.5차)에서 학생한테 실제 후보 조건 원문을 직접 짚어 물어본 경우,
    그 질문과 학생의 답은 이미 convo(대화 이력) 안에 그대로 들어있음 — soft_match_conditions
    프롬프트 자체가 "말 안 한 건 모르는 거지 아니라는 뜻이 아니다"를 이미 규칙으로 갖고 있어서,
    답변을 별도로 구조화해서 다시 넘길 필요 없이 convo만으로 충분히 반영됨(LLM 호출 한 번을
    아껴서 응답 속도도 빨라짐).
    LLM 호출이 실패해도(네트워크 등) 1차 필터 결과 자체는 그대로 살려서 반환함 —
    보조판단 실패가 검색 자체를 막으면 안 되니까."""
    candidates = []
    for s, _ in matches:
        e = s.eligibility
        conditions = []
        if e.special_conditions:
            conditions.extend(e.special_conditions)
        if e.major_restriction:
            conditions.append(f"학과제한: {e.major_restriction}")
        if e.grade_or_semester_note:
            conditions.append(f"학년/학기조건: {e.grade_or_semester_note}")
        if conditions:
            candidates.append({"scholarship_id": s.id or s.name, "conditions": conditions})

    if not candidates:
        return matches

    profile = (
        f"- 재학상태: {state.student_status or '미상'}\n"
        f"- 학과: {state.major or '미상'}\n"
        f"- 학년/학기: {state.grade_or_semester or '미상'}\n"
        f"- 평점: {state.gpa if state.gpa is not None else '미상'}\n"
        f"- 학자금지원구간: {state.income_bracket or '미상'}\n"
        f"- 거주지역: {state.region or '미상(학생이 말 안 함 — 지역조건은 이것만으론 배제하지 말 것)'}\n"
        f"- 특별조건 자기신고: {', '.join(state.special_conditions) if state.special_conditions else '없음/미상'} "
        f"(주의: 여기 없는 다른 카테고리는 '모름'이지 '해당 안 됨'이 아님)\n\n"
        f"[학생과의 대화 참고 — condition_check 단계에서 실제 후보 조건을 직접 짚어 물어본 질문과 "
        f"학생의 답변이 있으면 여기 포함돼 있으니 최우선으로 반영해라]\n{convo}"
    )

    # 실사용자 리포트로 확인된 문제: 이 LLM 호출이 실패하면(네트워크 순단, API 키/모델명
    # 오류 등) 조용히 1차 필터 결과를 그대로 다 보여주는 폴백이 있어서, 실패가 나도 화면상
    # 티가 안 나고 "왜 이상한 장학금이 계속 나오지"라는 증상으로만 보임 — 원인 진단이
    # 불가능했음. 그래서 (1) 순단성 오류를 대비해 한 번 재시도하고, (2) 그래도 실패하면
    # 최소한 서버 로그에는 남겨서 "지금 소프트매칭이 실제로 작동은 하는지"를 직접 확인할 수
    # 있게 함(터미널에서 이 문구가 계속 보이면 API 키/모델명을 점검해봐야 한다는 뜻).
    try:
        verdicts = bot_core.soft_match_conditions(profile, candidates)
    except Exception as e1:  # noqa: BLE001
        print(f"[filter_by_soft_conditions] 1차 호출 실패, 1회 재시도: {e1!r}")
        try:
            verdicts = bot_core.soft_match_conditions(profile, candidates)
        except Exception as e2:  # noqa: BLE001 — 재시도도 실패하면 1차 필터 결과는 그대로 유지
            print(
                f"[filter_by_soft_conditions] 재시도도 실패 — 소프트매칭 없이 1차 필터 결과만 "
                f"보여줌(학과/학년/특례조건 등은 검증되지 않은 상태): {e2!r}"
            )
            return matches

    # 실사용자 리포트: 시각디자인 전공 학생한테 "시각디자인 전공자 및 판화학과"가 학과제한인
    # 장학금이 2차 LLM 소프트매칭에서 걸러짐 — 학생 전공이 공지 학과제한 원문에 문자 그대로
    # 들어있는데도 제외된 거라 "확률적 판단이 가끔 틀릴 수 있다"로 넘어갈 문제가 아니라
    # 실제 버그임. 학과명이 이렇게 명확히 일치하는 경우는 지역 하드필터처럼 규칙만으로
    # 100% 확실하게 판단 가능한 영역이라, LLM이 뭐라고 판단하든(eligible=false를 줘도)
    # major_clearly_matches()가 True면 무조건 후보로 강제 유지시킴 — LLM 오판이 이 케이스를
    # 다시는 못 뚫게 규칙이 최종 결정권을 가짐. (반대방향은 안 씀: 학과가 안 맞아 보인다고
    # 규칙으로 자동배제하지는 않음 — 표현이 다양해서 오배제 위험이 크기 때문.)
    #
    # 실사용자 리포트: 조건 맞는 장학금이 2차 소프트매칭에서 조용히 사라지는 문제를 진단할
    # 방법이 없었음(왜 빠졌는지 서버 로그에 아무 흔적도 안 남음). LLM이 eligible=false를 준
    # 항목은 무조건 로그에 남겨서, 다음에 똑같은 문제가 재현되면 터미널에서 바로 "무슨 조건
    # 때문에 어떤 판단을 했는지" 확인 가능하게 함(동작 자체는 그대로, 진단용 로그만 추가).
    filtered = []
    for s, needs_income in matches:
        key = s.id or s.name
        eligible, reason = verdicts.get(key, (True, ""))
        if not eligible:
            if major_clearly_matches(state.major, s.eligibility.major_restriction):
                print(
                    f"[filter_by_soft_conditions] LLM은 제외(false)라고 했지만 학과가 "
                    f"명확히 일치해서 강제로 유지: {s.name} (id={key}) — LLM이 준 이유: {reason}"
                )
            else:
                print(f"[filter_by_soft_conditions] 제외됨: {s.name} (id={key}) — 이유: {reason}")
                continue
        filtered.append((s, needs_income))
    return filtered


def handle_scholarship_turn(sub: dict, convo: str, user_msg: str) -> dict:
    if user_msg in LIST_WORDS and sub["matches"]:
        reply = bot_core.generate_candidate_presentation(sub["matches"])
        sub["stage"] = "matched"
        return {
            "reply": reply,
            "stage": "matched",
            "options": [{"index": i + 1, "label": s.name} for i, (s, _) in enumerate(sub["matches"])],
        }

    stage = sub["stage"]

    if stage == "slot_filling":
        # 실사용자 리포트("답장 너무 오래 걸림") 대응: 원래 추출(extract_slots) + 다음질문
        # 생성(generate_followup_question)이 순서대로 호출 2번이라 응답이 거의 2배로
        # 느려지고 있었음. 이제 한 번에 합쳐서(extract_slots_with_followup) 대부분은 호출
        # 1번으로 끝냄 — 근데 "뭐가 비었는지" 최종 판단은 항상 여기서 missing_slots()로
        # 다시 결정론적으로 계산해서 검증하고, LLM이 준 질문이 그 판단과 안 맞으면(드물게
        # 헷갈렸을 때) 예전 방식(generate_followup_question, 호출 1번 더)으로 자동
        # 폴백함 — 최악의 경우에도 예전 속도 그대로, 맞아떨어지는 대부분은 훨씬 빠름.
        sub["state"], followup = bot_core.extract_slots_with_followup(convo, sub["state"])
        missing = missing_slots(sub["state"])
        if missing:
            reply = followup if followup and followup.strip() else bot_core.generate_followup_question(convo, missing)
            return {"reply": reply, "stage": "slot_filling", "options": []}

        db = load_db()
        matches = match_scholarships(db, sub["state"], today=date.today())
        if not matches:
            reply = (
                "지금 조건으로는 매칭되는 장학금을 못 찾았어. "
                "DB에 자료가 더 추가되면 다시 찾아볼 수 있어! "
                "'처음부터'라고 치면 조건 다시 입력할 수 있어."
            )
            return {"reply": reply, "stage": "slot_filling", "options": []}
        sub["matches"] = matches

        # region 슬롯 하나 물어보는 정도로는 부족하다는 사용자 피드백 반영 — 1차로 걸러진
        # 실제 후보들에 실제로 달려있는 special_conditions 원문 중, "한부모가정"/"다자녀"/
        # "차상위계층"처럼 학생이 스스로 말 안 하면 놓치기 쉬운 짧고 명확한 범주형 조건만 골라
        # "이 중에 해당되는 거 있어?"라고 직접 짚어 물어보는 단계로 감(1.5차).
        # 사용자 피드백("이렇게 한꺼번에 물으면 안되지, 의사가 진료할 때도 증상 다 던지는 게
        # 아니라 몇 개 추려서 초점 맞추는 거잖아")을 반영해서 두 가지로 제한함:
        # (a) 지역요건처럼 공고일·기간까지 딸린 긴 문단형 조건은 여기서 통째로 나열하지 않음 —
        #     그건 이미 물어본 region 슬롯 + 아래 2차 소프트매칭이 대화 맥락으로 알아서 판단할
        #     몫으로 남겨둠(final soft-match 후보 목록엔 이런 조건도 그대로 다 들어가서 판단됨,
        #     여기서 안 물어본다고 놓치는 게 아님).
        # (b) 후보 개수가 몇 개든 한 번에 물어볼 조건 개수 자체를 소수로 제한함.
        MAX_CONDITION_LEN = 16  # 이보다 길면 "짧은 범주 라벨"이 아니라 상세 조건문으로 간주
        MAX_CONDITIONS_TO_ASK = 4
        pending = []
        for s, _ in matches:
            for c in s.eligibility.special_conditions or []:
                if c and len(c) <= MAX_CONDITION_LEN and c not in pending:
                    pending.append(c)
        pending = pending[:MAX_CONDITIONS_TO_ASK]

        if pending:
            sub["pending_conditions"] = pending
            sub["stage"] = "condition_check"
            reply = bot_core.generate_condition_check_question(convo, pending)
            return {"reply": reply, "stage": "condition_check", "options": []}

        matches = filter_by_soft_conditions(matches, sub["state"], convo)
        sub["matches"] = matches
        if not matches:
            reply = (
                "지금 조건으로는 매칭되는 장학금을 못 찾았어. "
                "DB에 자료가 더 추가되면 다시 찾아볼 수 있어! "
                "'처음부터'라고 치면 조건 다시 입력할 수 있어."
            )
            return {"reply": reply, "stage": "slot_filling", "options": []}

        reply = bot_core.generate_candidate_presentation(matches)
        sub["stage"] = "matched"
        return {
            "reply": reply,
            "stage": "matched",
            "options": [{"index": i + 1, "label": s.name} for i, (s, _) in enumerate(matches)],
        }

    if stage == "condition_check":
        # 학생이 방금 pending_conditions(실제 후보들의 특례조건 원문)에 대해 답한 내용은
        # 이미 convo(대화 이력) 안에 그대로 들어있고, soft_match_conditions 프롬프트 자체가
        # "말 안 한 건 모르는 거지 아니라는 뜻이 아니다"라는 규칙을 이미 갖고 있어서, 별도로
        # 구조화 추출을 한 번 더 거칠 필요가 없음. (원래는 extract_condition_answers로 한 번
        # 더 구조화한 뒤 넘겼는데, 그러면 한 턴에 LLM을 3번 연달아 호출하게 돼서 체감 응답
        # 속도가 눈에 띄게 느려짐 — 사용자 피드백으로 확인. 정확도 손해 없이 2번으로 줄임.)
        matches = filter_by_soft_conditions(sub["matches"], sub["state"], convo)
        sub["matches"] = matches
        sub["pending_conditions"] = []
        if not matches:
            reply = (
                "확인해보니 지금 조건으로는 매칭되는 장학금이 없어. "
                "DB에 자료가 더 추가되면 다시 찾아볼 수 있어! "
                "'처음부터'라고 치면 조건 다시 입력할 수 있어."
            )
            sub["stage"] = "slot_filling"
            return {"reply": reply, "stage": "slot_filling", "options": []}

        reply = bot_core.generate_candidate_presentation(matches)
        sub["stage"] = "matched"
        return {
            "reply": reply,
            "stage": "matched",
            "options": [{"index": i + 1, "label": s.name} for i, (s, _) in enumerate(matches)],
        }

    if stage == "matched":
        m = re.search(r"\d+", user_msg)
        if not m:
            # 예전엔 숫자가 없으면 무조건 "번호로 골라줘"만 반복해서, 학생이 "이거 나
            # 해당 안 되는데?"처럼 후보를 반박해도 그 말을 완전히 무시하고 똑같은 문장을
            # 계속 뱉는 무한루프 버그가 있었음. 이제는 학생이 뭐라고 했는지 실제로 읽고
            # 반응한 다음에 다시 번호를 고를 수 있게 안내함.
            reply = bot_core.generate_matched_stage_reply(convo, sub["matches"])
            return {
                "reply": reply,
                "stage": "matched",
                "options": [{"index": i + 1, "label": s.name} for i, (s, _) in enumerate(sub["matches"])],
            }
        idx = int(m.group()) - 1
        if idx < 0 or idx >= len(sub["matches"]):
            reply = "그 번호는 없어, 다시 골라줘!"
            return {
                "reply": reply,
                "stage": "matched",
                "options": [{"index": i + 1, "label": s.name} for i, (s, _) in enumerate(sub["matches"])],
            }
        selected, needs_income_check = sub["matches"][idx]
        sub["selected"] = selected
        sub["needs_income_check"] = needs_income_check
        guide = bot_core.generate_action_guide(selected)
        if needs_income_check:
            guide += (
                "\n\n(참고: 이 장학금은 학자금지원구간 확인이 필요해요. "
                "한국장학재단 홈페이지에서 구간 확인하고 다시 알려주면 최종 확정해줄게.)"
            )
        guide += "\n\n서류 준비하면서 궁금한 거 편하게 물어봐도 돼!"
        sub["stage"] = "consult"
        return {"reply": guide, "stage": "consult", "options": []}

    # stage == "consult"
    # 아까 번호 붙여서 보여준 후보 전체(sub["matches"])를 같이 넘겨줌 — 학생이 자기가 고른
    # 것 말고 "근데 1번은 얼마야?"처럼 다른 번호를 다시 물어봐도 실제 DB 정보로 답하게 함
    # (bot_core.generate_consult_answer 주석 참고 — 예전엔 선택된 것 1개만 봐서 다른 번호
    # 질문엔 정보 있는데도 "모른다"고 지어내는 버그가 있었음).
    all_candidates = [s for s, _ in sub.get("matches") or []]
    reply = bot_core.generate_consult_answer(convo, sub["selected"], all_candidates)
    return {"reply": reply, "stage": "consult", "options": []}


def _match_courses(sub: dict, target: str) -> None:
    """수집된 completed_courses(학생이 직접 말한 것 + course_overlap_check에서 확인받아
    추가된 것)를 curriculum.match_completed_courses로 최종 매칭해서 sub['matched']/['unmatched']에
    저장한다. course_collect(겹치는 과목이 없어서 바로 넘어올 때)와 course_overlap_check
    (겹치는 과목 확인 마친 뒤) 양쪽에서 공유해서 쓴다."""
    matched, unmatched = curriculum.match_completed_courses(target, sub["completed_courses"])
    sub["matched"] = matched
    sub["unmatched"] = unmatched


def _proceed_after_matching(sub: dict, target: str) -> dict:
    """매칭이 끝난 뒤 다음 단계를 정한다. 공식 양식엔 과목별 '이수학기(연도/학기)' 칸이
    있는데, 매칭된 과목 중 학생이 실제로 언제 들었는지 아직 말 안 한 게 있으면 그 칸이
    빈칸으로 남으니 먼저 한 번 물어보고(실사용자 요청: "이수학기도 채워주면 좋겠는데"),
    없으면(또는 이미 다 확인됐으면) 바로 course_draft로 간다. 교육과정표의 편성 학년/학기를
    대신 채우지 않는 이유는 curriculum.match_completed_courses의 taken_year/taken_semester
    주석 참고 — 실제 이수 시점은 학생마다 다를 수 있는 별개의 사실이라 지어내면 안 됨."""
    missing = [m.course_name for m in sub["matched"] if not m.taken_year and not m.taken_semester]
    if missing:
        sub["stage"] = "course_semester_check"
        sub["semester_pending"] = missing
        listing = "\n".join(f"- {n}" for n in missing)
        reply = (
            f"하나만 더 물어볼게! 아래 과목은 실제로 몇 년도/몇 학기에 들었는지 몰라서 신청서 "
            f"'이수학기' 칸이 비어있게 돼:\n{listing}\n\n"
            "알고 있으면 알려줘 (예: \"C프로그래밍은 2024년 1학기, 자료구조는 2학년 2학기\"), "
            "기억 안 나면 '몰라'라고 말해줘 — 그럼 그 칸은 비워두고 넘어갈게!"
        )
        return {"reply": reply, "stage": "course_semester_check"}

    reply = _build_course_draft_reply(sub, target)
    return {"reply": reply, "stage": "course_draft", "matched_count": len(sub["matched"])}


def _build_course_draft_reply(sub: dict, target: str) -> str:
    """course_draft 단계 진입 안내 메시지를 만든다(매칭·이수학기 확인은 이미 끝났다고 가정)."""
    sub["stage"] = "course_draft"
    reply = bot_core.generate_recognition_draft_message(target, sub["matched"], sub["unmatched"])
    # "파일은 못 만들어주고 네가 손으로 옮겨 적어" 같은 안내는 LLM이 자기 능력을 과소평가해서
    # (텍스트 생성기인 자신은 파일을 못 만든다고 여기고) 프롬프트 지시를 무시하고 즉흥적으로
    # 지어낼 수 있음 — 실사용자 리포트로 확인됨("옮겨적으라해도 파일 만들기는 못한다던데?").
    # 이건 이 프로젝트의 핵심 설계원칙과 같은 이유로 LLM 재량에 맡기면 안 되는 부분이라
    # (돈/자격요건 판단을 LLM에 안 맡기듯, 이 안내문구도) 고정 문구를 덧붙여서 무조건 보장함.
    if sub["matched"]:
        reply += (
            "\n\n(참고: '제출하기' 누르면 이름/학번/학년/소속 단과대학만 몇 개 확인하고 바로 "
            "공식 양식(.docx) 파일에 과목까지 전부 채워서 다운로드 버튼으로 줄게 — 손으로 옮겨 "
            "적을 거 없어!)"
        )
    return reply


def _rejection_reason(app: RecognitionApplication, formal: bool = False) -> str:
    """신청서가 반려됐을 때(status == "rejected") 어느 단계에서 왜 반려됐는지 보여줄 문장을
    만든다. 3단계(학과장/복수전공학과장/행정처) 중 실제로 반려 처리된 단계를 찾아서 그
    기록(decided_by/note)만 그대로 옮긴다 — 지어내지 않음. 3단계 승인 기능이 생기기 전의
    구버전 기록(레거시 decision_note)만 있는 경우를 대비해 그쪽도 대체 경로로 확인한다
    (STAGE_LABELS/decide 라우트의 레거시 호환 주석과 동일한 이유).

    formal=False(기본)면 챗봇에서 학생한테 반말로 보여줄 때 쓰고(_handle_application_lookup),
    formal=True면 이메일 본문처럼 존댓말이 필요한 곳에서 쓴다(실사용자 요청: "이메일이랑
    교수님들이나 직원들이 보는 페이지는 무조건 존댓말로") — 문구만 다르고 로직은 동일."""
    stages = [
        ("학과장", app.home_chair_approval),
        ("복수전공학과장", app.dual_chair_approval),
        ("행정처", app.admin_approval),
    ]
    for label, step in stages:
        if step.status == "rejected":
            who = step.decided_by or label
            if formal:
                if step.note:
                    return f"{label}({who})님이 반려하셨습니다. 사유: {step.note}"
                return f"{label}({who})님이 반려하셨습니다(별도로 남긴 사유는 없습니다)."
            if step.note:
                return f"{label}({who})이 반려했어. 사유: {step.note}"
            return f"{label}({who})이 반려했어(별도로 남긴 사유는 없어)."
    # 레거시(구버전 1단계 승인) 기록만 있는 경우
    if formal:
        if app.decision_note:
            return f"반려되었습니다. 사유: {app.decision_note}"
        return "반려되었으나, 구체적인 사유는 따로 남아있지 않습니다."
    if app.decision_note:
        return f"반려됐어. 사유: {app.decision_note}"
    return "반려됐는데, 구체적인 사유는 따로 남아있지 않아."


_IDENTITY_LABELS = {
    "name": "이름",
    "student_id": "학번",
    "grade": "학년",
    "college": "소속 단과대학",
    "email": "이메일",
}


def _missing_identity_labels(identity: dict) -> list[str]:
    """student_identity 딕셔너리에서 아직 안 채워진 항목의 한글 라벨 목록을 돌려준다.
    student_info 단계에서 새로 물어볼 때와, 반려->재제출 시 예전 신청서 정보를 이어받아
    이미 다 채워졌는지 확인할 때(course_draft 제출 분기) 둘 다에서 같이 쓴다."""
    return [label for key, label in _IDENTITY_LABELS.items() if not identity.get(key)]


def _identity_confirm_message(identity: dict) -> str:
    """student_info_confirm 단계 진입 시 보여줄 확인 문구 — 두 진입 경로(직접 입력 완료 /
    반려->재제출 시 예전 정보 이어받아 전부 채워진 경우)가 똑같은 문구를 쓰게 공유한다."""
    return (
        f"확인할게! 이름: {identity['name']} / 학번: {identity['student_id']} / "
        f"학년: {identity['grade']} / 소속: {identity['college']} / 이메일: {identity['email']} "
        f"— 맞으면 '제출', 틀린 게 있으면 '다시입력'이라고 알려줘!"
    )


# 학번 기반 세션 복원 — _LOOKUP_TRIGGER_RE/_LOOKUP_STUDENT_ID_RE 주석 참고. 새 탭/새 세션으로
# 돌아온 학생이 학번만 알려주면, 그 학번으로 마지막에 낸 인정신청서를 찾아서 지금 상태를
# 알려주고, 반려 상태면 기존 rejection_followup 흐름(수정 후 재제출)에 그대로 이어붙인다 —
# "다른 세션에서 반려 처리됨"과 "같은 세션에서 반려 처리됨"을 같은 코드 경로로 합쳐서
# 새로 짤 로직을 최소화함.
def _handle_application_lookup(sess: dict, student_id: str) -> dict:
    apps = load_recognition_apps()
    mine = [a for a in apps if a.student_id == student_id]
    if not mine:
        return {
            "reply": f"학번 {student_id}로 낸 이수과목 인정신청서를 못 찾았어. 학번 다시 한번 확인해줄래?",
            "stage": "intent",
        }
    latest = max(mine, key=lambda a: a.created_at)

    if latest.status == "rejected":
        sub = sess["dual_major"]
        sub["last_application_id"] = latest.id
        sub["rejection_notified"] = True
        sub["stage"] = "rejection_followup"
        sess["mode"] = "dual_major"
        reason = _rejection_reason(latest)
        reply = (
            f"찾았다! 저번에 낸 인정신청서가 반려됐었네. {reason}\n\n"
            "일부만 수정해서 다시 제출할 수 있어 — 지금 바로 수정해서 다시 낼까? "
            "('응'이라고 하면 저번에 확인됐던 과목 그대로 들고 이어서 빼거나 더할 수 있게 해줄게, "
            "'아니'라고 하면 나중에 다시 얘기하자)"
        )
        return {"reply": reply, "stage": "rejection_followup"}

    if latest.status == "approved":
        reply = f"찾았다! {latest.target_major} 인정신청서 3단계 승인 다 끝나서 전산 반영까지 완료됐어. 축하해!"
        return {"reply": reply, "stage": "intent"}

    reply = (
        f"찾았다! {latest.target_major} 인정신청서는 아직 승인 절차 진행 중이야 "
        "(학과장/복수전공학과장 → 행정처 순으로 확인 중, 끝나면 알려줄게)."
    )
    return {"reply": reply, "stage": "intent"}


def handle_dual_major_turn(session_id: str, sub: dict, convo: str, user_msg: str) -> dict:
    stage = sub["stage"]

    if stage == "slot_filling":
        sub["state"] = bot_core.extract_dual_major_slots(convo, sub["state"])
        missing = dual_major_missing_slots(sub["state"])
        if missing:
            reply = bot_core.generate_dual_major_followup_question(convo, missing)
            return {"reply": reply, "stage": "slot_filling"}

        # 슬롯 다 채워짐 -> 결정론적 규칙으로 판정 (LLM 아님)
        result = check_dual_major_eligibility(sub["state"])
        sub["result"] = result
        reply = bot_core.generate_dual_major_verdict_message(result)
        sub["stage"] = "done"
        return {"reply": reply, "stage": "done", "eligible": result.eligible}

    if stage == "done":
        # (0) 방금 제출한 신청서(sub["last_application_id"])가 그 사이 어딘가에서 반려됐는데
        # 아직 학생한테 못 알려줬으면, 다른 무엇보다 먼저 그것부터 알려준다(실사용자 요청:
        # "어떤 단계에서 반려되면 학생한테 알려줘서 일부수정해서 다시 제출할 수 있는 단계를
        # 만들어주면 좋겠어"). rejection_notified 플래그로 한 번만 알리고, 학생이 뭐라고
        # 말했든(인사든 다른 질문이든) 이 알림이 먼저 나가야 놓치지 않는다.
        if sub.get("last_application_id") and not sub.get("rejection_notified"):
            apps = load_recognition_apps()
            application = next((a for a in apps if a.id == sub["last_application_id"]), None)
            if application and application.status == "rejected":
                sub["rejection_notified"] = True
                sub["stage"] = "rejection_followup"
                reason = _rejection_reason(application)
                reply = (
                    f"어! 저번에 낸 인정신청서가 반려됐어. {reason}\n\n"
                    "일부만 수정해서 다시 제출할 수 있어 — 지금 바로 수정해서 다시 낼까? "
                    "('응'이라고 하면 저번에 확인됐던 과목 그대로 들고 이어서 빼거나 더할 수 있게 해줄게, "
                    "'아니'라고 하면 나중에 다시 얘기하자)"
                )
                return {"reply": reply, "stage": "rejection_followup"}

        # (0-1) "파일/다운로드/양식" 요청 -> 이미 제출한 신청서가 있으면 다운로드 탭을 다시
        # 띄워준다. 원래는 제출 직후 그 한 턴에만 application_id가 응답에 실려서 프론트가
        # 다운로드 버튼을 보여줬는데, 그 뒤로는 다시 안 보였음(실사용자 요청: "수정제출할때
        # 학생한테 수정파일이 안 보이는데 학생이 파일 보여달라고하면 다운로드하는 탭을
        # 보여주면 좋겠어") — 그래서 학생이 명시적으로 다시 요청하면 다시 실어서 보내준다.
        FILE_REQUEST_WORDS = ("파일", "다운로드", "양식")
        if sub.get("last_application_id") and any(w in user_msg for w in FILE_REQUEST_WORDS):
            reply = "여기! 아까 제출한 신청서 파일이야 — 위에 뜬 다운로드 버튼 눌러줘."
            result = sub["result"]
            return {
                "reply": reply,
                "stage": "done",
                "eligible": result.eligible if result else None,
                "application_id": sub["last_application_id"],
            }

        # "이미 들은 과목 인정받고 싶어" 류의 요청 -> 2단계(이수과목 인정) 진입.
        # 자격 충족 + 실제로 데이터가 준비된 학과 조합일 때만 지원 (데모 범위).
        result = sub["result"]
        wants_recognition = "인정" in user_msg
        if wants_recognition and result and result.eligible:
            sub["state"] = bot_core.extract_dual_major_slots(convo, sub["state"])
            home = sub["state"].home_college
            target = sub["state"].target_major

            if not curriculum.resolve_major(home):
                sub["stage"] = "awaiting_home_major"
                reply = "인정신청서 만들어보자! 근데 소속 학과를 아직 몰라서 — 무슨 학과 소속이야?"
                return {"reply": reply, "stage": "awaiting_home_major", "eligible": result.eligible}

            if not curriculum.is_supported_pair(home, target):
                reply = (
                    "오 좋은 질문인데, 이수과목 인정신청 데모는 지금 "
                    "'미래자동차공학과 → 컴퓨터공학전공' 조합 교육과정표만 준비돼 있어. "
                    "다른 학과 조합은 아직 데이터가 없어서 정확하게 확인 못 해줘, 미안!"
                )
                return {"reply": reply, "stage": "done", "eligible": result.eligible}

            sub["stage"] = "course_collect"
            sub["completed_courses"] = []
            extraction = bot_core.extract_completed_courses(convo, [])
            for c in extraction.courses:
                sub["completed_courses"].append(c)
            reply = bot_core.generate_course_ask_more_message(
                convo, [c.course_name for c in sub["completed_courses"]]
            )
            return {"reply": reply, "stage": "course_collect", "eligible": result.eligible}

        reply = bot_core.generate_dual_major_consult_answer(convo, result)
        return {"reply": reply, "stage": "done", "eligible": result.eligible if result else None}

    if stage == "rejection_followup":
        # done 단계에서 반려 사실을 알린 직후 나오는 예/아니오 확인. "응"이면 예전에 이미
        # 확인/매칭됐던 과목들을 그대로 들고 course_collect로 돌아가서, 학생이 처음부터 다시
        # 다 말 안 해도 빼거나("~~ 빼줘") 더할 수 있게 한다(실사용자 요청: "일부수정해서
        # 다시 제출"). "아니"면 그냥 done으로 돌아가고 신청서는 반려 상태로 남는다.
        YES_WORDS = {"응", "어", "그래", "네", "좋아", "수정", "다시제출", "yes", "오케이", "ㅇㅋ", "ㅇㅇ"}
        NO_WORDS = {"아니", "아니요", "안할래", "no", "나중에", "됐어"}
        norm = user_msg.replace(" ", "")

        if norm in NO_WORDS:
            sub["stage"] = "done"
            reply = "알겠어, 나중에 준비되면 다시 얘기해줘! 그때 다시 도와줄게."
            result = sub["result"]
            return {"reply": reply, "stage": "done", "eligible": result.eligible if result else None}

        if norm not in YES_WORDS:
            reply = "지금 바로 수정해서 다시 낼지 알려줄래? ('응' 또는 '아니'로 답해줘)"
            return {"reply": reply, "stage": "rejection_followup"}

        apps = load_recognition_apps()
        old_app = next((a for a in apps if a.id == sub["last_application_id"]), None)
        carried = [
            CompletedCourseItem(
                course_name=m.course_name,
                course_code=m.course_code,
                taken_year=m.taken_year,
                taken_semester=m.taken_semester,
            )
            for m in (old_app.matched_courses if old_app else [])
        ]
        sub["completed_courses"] = carried
        # 실사용자 요청: "반려->재제출 시 이름/학번/이메일 등 자동 재사용" — 예전 신청서에
        # 남아있던 개인정보를 그대로 이어받아둔다. 이메일 기능 도입 전 신청서라 student_email이
        # 없을 수도 있는데, 그런 항목은 None으로 남아서 나중에 student_info 단계에서 그것만
        # 다시 물어보면 됨(_missing_identity_labels가 항목별로 판단).
        if old_app:
            sub["student_identity"] = {
                "name": old_app.student_name,
                "student_id": old_app.student_id,
                "grade": old_app.student_grade,
                "college": old_app.student_college,
                "email": old_app.student_email,
            }
        sub["matched"] = []
        sub["unmatched"] = []
        sub["overlap_candidates"] = []
        sub["semester_pending"] = []
        sub["stage"] = "course_collect"
        names = ", ".join(c.course_name for c in carried) or "(없음)"
        reply = (
            f"좋아, 저번에 확인됐던 과목 그대로 이어서 할게: {names}\n\n"
            "여기서 뺄 과목 있으면 \"OO 빼줘\"라고 말해주고, 더 추가할 과목 있으면 이름 알려줘. "
            "다 됐으면 '다 말했어'라고 해줘!"
        )
        return {"reply": reply, "stage": "course_collect"}

    if stage == "awaiting_home_major":
        sub["state"] = bot_core.extract_dual_major_slots(convo, sub["state"])
        home = sub["state"].home_college
        target = sub["state"].target_major

        if not curriculum.resolve_major(home):
            reply = "음, 학과 이름을 정확히 못 알아들었어. 예를 들면 '미래자동차공학과'처럼 정식 학과명으로 말해줄래?"
            return {"reply": reply, "stage": "awaiting_home_major"}

        if not curriculum.is_supported_pair(home, target):
            reply = (
                f"'{home} → {target}' 조합은 아직 교육과정표 데이터가 준비 안 됐어. "
                "데모는 지금 '미래자동차공학과 → 컴퓨터공학전공' 조합만 지원해, 미안!"
            )
            sub["stage"] = "done"
            return {"reply": reply, "stage": "done", "eligible": sub["result"].eligible if sub["result"] else None}

        sub["stage"] = "course_collect"
        sub["completed_courses"] = []
        reply = bot_core.generate_course_ask_more_message(convo, [])
        return {"reply": reply, "stage": "course_collect"}

    if stage == "course_collect":
        FINISH_WORDS = {"다말했어", "다했어", "그만", "완료", "done", "없어", "그게다야", "끝"}
        already = [c.course_name for c in sub["completed_courses"]]
        extraction = bot_core.extract_completed_courses(convo, already)
        for c in extraction.courses:
            if c.course_name not in already:
                sub["completed_courses"].append(c)
                already.append(c.course_name)

        # 반려된 신청서를 일부수정해서 다시 낼 때(rejection_followup에서 이 단계로 돌아온
        # 경우) 예전에 확인됐던 과목이 미리 들어와 있는데, 그중 일부를 빼달라고 할 수 있음
        # (실사용자 요청: "일부수정해서 다시 제출"). LLM이 지어낸 이름을 빼는 걸 막기 위해
        # 실제로 completed_courses 안에 있는 이름인지 한 번 더 검증한다(이중 안전장치,
        # course_overlap_check/course_semester_check와 동일한 패턴).
        current_names = {c.course_name for c in sub["completed_courses"]}
        for name in extraction.removed_course_names:
            if name in current_names:
                sub["completed_courses"] = [c for c in sub["completed_courses"] if c.course_name != name]
                current_names.discard(name)
        already = [c.course_name for c in sub["completed_courses"]]  # 제거 반영해서 다시 계산

        finished = extraction.done or user_msg.replace(" ", "") in FINISH_WORDS
        if not finished:
            reply = bot_core.generate_course_ask_more_message(convo, already)
            return {"reply": reply, "stage": "course_collect"}

        # 수집 종료 -> 최종 매칭 전에, 홈학과·목표학과 교육과정표에 둘 다 편성돼 있는데
        # 학생이 아직 말 안 한 과목이 있는지 먼저 확인한다(실사용자 요청: "학생이 만약에
        # 들었는데 까먹고 이야기 안 했을 수도 있으니까, 비교해서 겹치는 과목 이미 들은거같으면
        # 미리 물어보고 신청서에 기재해줄 수 있도록"). 겹치는 과목이 없으면 바로 매칭으로 넘어감.
        target = sub["state"].target_major
        home = sub["state"].home_college
        already_names = [c.course_name for c in sub["completed_courses"]]
        overlap = curriculum.find_potential_overlap(home, target, already_names)
        if overlap:
            sub["overlap_candidates"] = overlap
            sub["stage"] = "course_overlap_check"
            listing = "\n".join(f"{i + 1}. {o['name']}" for i, o in enumerate(overlap))
            reply = (
                f"잠깐, 하나만 더 확인해볼게! {home}이랑 {target} 교육과정표에 둘 다 편성돼 있는데 "
                f"네가 아직 말 안 한 과목이 있어:\n{listing}\n\n"
                "혹시 이 중에 이미 들은 과목 있어? 있으면 번호나 과목명으로 알려주고, 없으면 "
                "'없어'라고 말해줘!"
            )
            return {"reply": reply, "stage": "course_overlap_check"}

        _match_courses(sub, target)
        return _proceed_after_matching(sub, target)

    if stage == "course_overlap_check":
        # 결정론적으로 뽑아둔 후보(sub["overlap_candidates"])에 대해 학생이 뭐라고 답했는지만
        # LLM으로 구조화하고("들었다"고 확인한 과목이 어떤 건지), 그 과목을 실제로 이수과목
        # 목록에 추가할지는 여기 코드가 최종 결정함 — LLM이 후보 목록 밖 과목명을 지어냈을
        # 가능성에 대비해 candidate_names 안에 있는지 한 번 더 검증(이중 안전장치).
        NONE_WORDS = {"없어", "없음", "아니", "no", "none", "안들었어", "안들었음", "못들었어"}
        norm = user_msg.replace(" ", "")
        candidates = sub.get("overlap_candidates") or []
        candidate_names = [c["name"] for c in candidates]

        if norm not in NONE_WORDS and candidate_names:
            try:
                confirmation = bot_core.extract_overlap_confirmation(candidate_names, user_msg)
            except Exception:  # noqa: BLE001 — 추출 실패해도 진행은 막지 않고 그냥 추가 없이 넘어감
                confirmation = None
            if confirmation:
                already = {c.course_name for c in sub["completed_courses"]}
                by_name = {c["name"]: c for c in candidates}
                for name in confirmation.confirmed_course_names:
                    if name not in by_name or name in already:
                        continue
                    row = by_name[name]
                    sub["completed_courses"].append(
                        CompletedCourseItem(course_name=row["name"], course_code=row["code"])
                    )
                    already.add(name)

        sub["overlap_candidates"] = []
        target = sub["state"].target_major
        _match_courses(sub, target)
        return _proceed_after_matching(sub, target)

    if stage == "course_semester_check":
        # 매칭된 과목 중 실제 이수 연도/학기를 몰랐던 것들(sub["semester_pending"])에 대해
        # 학생이 뭐라고 답했는지만 LLM으로 구조화하고, 그 값을 실제로 반영할지는 여기 코드가
        # 최종 결정함 — LLM이 후보 목록 밖 과목명을 지어냈을 가능성에 대비해 semester_pending
        # 안에 있는지 한 번 더 검증(이중 안전장치, course_overlap_check와 동일한 패턴).
        SKIP_WORDS = {"몰라", "모름", "모르겠어", "없어", "패스", "skip", "기억안나", "기억안남"}
        norm = user_msg.replace(" ", "")
        pending = sub.get("semester_pending") or []

        if norm not in SKIP_WORDS and pending:
            try:
                extraction = bot_core.extract_course_semesters(pending, user_msg)
            except Exception:  # noqa: BLE001 — 추출 실패해도 진행은 막지 않고 빈칸인 채로 넘어감
                extraction = None
            if extraction:
                by_name = {m.course_name: m for m in sub["matched"]}
                for item in extraction.items:
                    if item.course_name not in pending:
                        continue
                    target_course = by_name.get(item.course_name)
                    if not target_course:
                        continue
                    if item.taken_year:
                        target_course.taken_year = item.taken_year
                    if item.taken_semester:
                        target_course.taken_semester = item.taken_semester

        sub["semester_pending"] = []
        target = sub["state"].target_major
        reply = _build_course_draft_reply(sub, target)
        return {"reply": reply, "stage": "course_draft", "matched_count": len(sub["matched"])}

    if stage == "course_draft":
        CONFIRM_WORDS = {"제출", "응", "어", "그래", "네", "좋아", "submit", "yes", "오케이", "ㅇㅋ", "ㅇㅇ"}
        CANCEL_WORDS = {"취소", "아니", "안할래", "no", "그만할게", "됐어"}
        norm = user_msg.replace(" ", "")

        if norm in CONFIRM_WORDS:
            if not sub["matched"]:
                reply = "지금은 인정 가능한 과목이 없어서 제출할 신청서가 없어. 다른 과목 알려주고 싶으면 말해줘!"
                sub["stage"] = "done"
                return {"reply": reply, "stage": "done", "eligible": sub["result"].eligible if sub["result"] else None}
            # 과목표만 채워진 빈 서류는 직원한테 올려도 의미가 없다는 피드백("개인정보도 없고 ...
            # 이걸 전산화 한다는데에 의의가 있어야지", "모든 개인정보를 채워넣을 수 있도록")에
            # 따라, 실제 신청서에 필요한 성명/학번/학년/소속 단과대학을 확인받고 나서야
            # 신청서를 생성함(바로 만들지 않음).
            # 단, 반려->재제출(rejection_followup)로 들어온 경우는 예전 신청서의 개인정보를
            # 이미 이어받아놨을 수 있음(실사용자 요청: "반려->재제출 시 자동 재사용") — 그때는
            # 이름/학번/학년/소속/이메일을 처음부터 다시 물어보지 않고 바로 확인만 받는다.
            identity = sub["student_identity"]
            if not _missing_identity_labels(identity):
                sub["stage"] = "student_info_confirm"
                reply = (
                    "저번에 낸 신청서에 있던 개인정보 그대로 이어서 쓸게! "
                    + _identity_confirm_message(identity)
                )
                return {"reply": reply, "stage": "student_info_confirm"}

            sub["stage"] = "student_info"
            reply = (
                "좋아, 마지막으로 신청서에 넣을 정보만 확인할게! "
                "이름, 학번, 학년, 소속 단과대학, 이메일 알려줄래? "
                "(예: 이민수 20231234 3학년 공과대학 minsu@yu.ac.kr) "
                "— 이메일은 반려/승인 결과 나오는 즉시 바로 알려주려고 받는 거야!"
            )
            return {"reply": reply, "stage": "student_info"}

        if norm in CANCEL_WORDS:
            reply = "알겠어, 신청 안 할게! 다른 거 궁금한 거 있으면 편하게 물어봐."
            sub["stage"] = "done"
            return {"reply": reply, "stage": "done", "eligible": sub["result"].eligible if sub["result"] else None}

        reply = "이대로 인정신청서 제출할지 알려줄래? ('제출' 또는 '취소'라고 말해줘)"
        return {"reply": reply, "stage": "course_draft"}

    if stage == "student_info":
        CANCEL_WORDS = {"취소", "아니", "안할래", "no", "그만할게", "됐어"}
        if user_msg.replace(" ", "") in CANCEL_WORDS:
            reply = "알겠어, 신청 안 할게! 다른 거 궁금한 거 있으면 편하게 물어봐."
            sub["stage"] = "done"
            return {"reply": reply, "stage": "done", "eligible": sub["result"].eligible if sub["result"] else None}

        identity = sub["student_identity"]
        try:
            extracted = bot_core.extract_student_identity(user_msg)
            if extracted.name:
                identity["name"] = extracted.name
            if extracted.student_id:
                identity["student_id"] = extracted.student_id
            if extracted.grade:
                identity["grade"] = extracted.grade
            if extracted.college:
                identity["college"] = extracted.college
            if extracted.email:
                identity["email"] = extracted.email
        except Exception:  # noqa: BLE001
            pass  # 추출 실패해도 아래에서 부족한 항목 다시 물어보면 되니 신청 자체는 안 막음

        missing_labels = _missing_identity_labels(identity)
        if missing_labels:
            reply = f"{', '.join(missing_labels)}을(를) 아직 못 알아들었어. 다시 한번 알려줄래?"
            return {"reply": reply, "stage": "student_info"}

        # 다 모였다고 바로 신청서를 만들지 않고, 학생한테 한 번 더 확인받고 나서 제출함
        # (실사용자 요청: "채우고 난 다음에 학생에게 confirm을 받아서 제출할 수 있도록") —
        # 잘못 알아들은 정보가 있으면 여기서 고칠 기회를 준다.
        sub["stage"] = "student_info_confirm"
        reply = _identity_confirm_message(identity)
        return {"reply": reply, "stage": "student_info_confirm"}

    if stage == "student_info_confirm":
        CONFIRM_WORDS = {"제출", "응", "어", "그래", "네", "좋아", "맞아", "submit", "yes", "오케이", "ㅇㅋ", "ㅇㅇ"}
        RETRY_WORDS = {"다시입력", "다시", "수정", "틀렸어", "아니"}
        CANCEL_WORDS = {"취소", "안할래", "no", "그만할게", "됐어"}
        norm = user_msg.replace(" ", "")

        if norm in RETRY_WORDS:
            sub["student_identity"] = {
                "name": None,
                "student_id": None,
                "grade": None,
                "college": None,
                "email": None,
            }
            sub["stage"] = "student_info"
            reply = (
                "알겠어, 다시 알려줄래? 이름, 학번, 학년, 소속 단과대학, 이메일 "
                "(예: 이민수 20231234 3학년 공과대학 minsu@yu.ac.kr)"
            )
            return {"reply": reply, "stage": "student_info"}

        if norm in CANCEL_WORDS:
            reply = "알겠어, 신청 안 할게! 다른 거 궁금한 거 있으면 편하게 물어봐."
            sub["stage"] = "done"
            return {"reply": reply, "stage": "done", "eligible": sub["result"].eligible if sub["result"] else None}

        if norm not in CONFIRM_WORDS:
            reply = "이대로 제출할지 알려줄래? ('제출' 또는 정보가 틀렸으면 '다시입력'이라고 말해줘)"
            return {"reply": reply, "stage": "student_info_confirm"}

        identity = sub["student_identity"]
        application = RecognitionApplication(
            id=uuid.uuid4().hex[:12],
            session_id=session_id,
            student_name=identity["name"],
            student_id=identity["student_id"],
            student_grade=identity["grade"],
            student_college=identity["college"],
            student_email=identity["email"],
            home_major=sub["state"].home_college,
            target_major=sub["state"].target_major,
            matched_courses=sub["matched"],
            unmatched_course_names=sub["unmatched"],
            status="pending",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        # 신청서 파일은 다른 세션의 제출/직원 승인화면의 승인·반려·삭제와 같은 파일을
        # 공유하니, 읽고-고치고-쓰는 구간 전체를 전역 락으로 감싸서 서로 덮어쓰지 않게 함.
        with RECOGNITION_APPS_LOCK:
            apps = load_recognition_apps()
            apps.append(application)
            save_recognition_apps(apps)
        sub["last_application_id"] = application.id
        sub["rejection_notified"] = False  # 새 신청서니까, 이게 나중에 반려되면 그때 다시 알려줘야 함
        reply = bot_core.generate_recognition_submitted_message(application)
        # course_draft 단계와 같은 이유로 고정 문구 보장(LLM 즉흥 안내에 맡기지 않음) — 특히
        # "학과장 찾아가서 서명 받아와" 같은 예전 방식 안내는 이 3단계 전산승인 기능이 없애려는
        # 바로 그 수고라서, 프롬프트만 믿지 않고 여기서도 3단계 승인 흐름을 한 번 더 명시함.
        reply += (
            "\n\n(위에 뜬 '신청서 양식(.docx)으로 다운로드' 버튼 눌러서 파일 받아! "
            "과목이랑 개인정보까지 이미 다 채워져 있어. 이제부터는 학과장 → 복수전공학과장"
            "(이 둘은 순서 상관없이 동시에 진행돼) → 행정처 순으로 전산 승인이 진행되고, "
            "셋 다 승인되면 자동으로 전산 반영돼 — 서명 받으러 직접 찾아다닐 필요 없어!)"
        )
        sub["stage"] = "done"
        return {
            "reply": reply,
            "stage": "done",
            "eligible": sub["result"].eligible if sub["result"] else None,
            "application_id": sub["last_application_id"],
        }

    # 알 수 없는 stage로 빠졌을 때를 위한 안전장치
    reply = "잠깐 흐름이 꼬였나봐. '처음부터'라고 입력해서 다시 시작해줘!"
    return {"reply": reply, "stage": stage}


# 실사용자 요청: "사람들이 나눈 대화를 로그처럼 저장해놓을 수 있으면 좋겠는데" — `_chat_impl`
# 안에 return 지점이 여러 군데(RESET/EXIT/도움말/신고 후속질문/학번조회/본래 상태머신 등)라
# 각각에 로깅 코드를 넣으면 하나라도 빠뜨리기 쉬움. 대신 호출부인 `chat()`에서 결과를 받은
# 직후 한 곳에서만 기록해서, 실제로 학생에게 나간 응답과 항상 정확히 일치하게 함.
def _log_chat_turn(session_id: str, user_msg: str, result: dict) -> None:
    entry = ChatLogEntry(
        id=uuid.uuid4().hex[:12],
        session_id=session_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        student_message=user_msg,
        ai_reply=result.get("reply", ""),
        mode=result.get("mode"),
        stage=result.get("stage"),
    )
    try:
        with CHAT_LOGS_LOCK:
            logs = load_chat_logs()
            logs.append(entry)
            save_chat_logs(logs)
    except Exception:  # noqa: BLE001 — 로그 저장 실패가 실제 챗봇 응답까지 막으면 안 됨
        pass


@app.post("/api/chat")
def chat(body: ChatIn):
    # 같은 세션(같은 학생)에서 거의 동시에 요청이 두 번 들어와도(더블클릭, 응답 느릴 때
    # 재전송 등) 반드시 순서대로 하나씩 처리되게 세션별 락을 건다 — 특히 신청서 제출
    # 단계에서 "제출"이 두 번 겹치면 신청서가 2개로 접수되는 걸 막기 위함.
    session_id = body.session_id or uuid.uuid4().hex
    with _get_session_lock(session_id):
        result = _chat_impl(session_id, body)
        _log_chat_turn(session_id, body.message, result)
        return result


def _chat_impl(session_id: str, body: ChatIn):
    sess = get_unified_session(session_id)
    user_msg = body.message.strip()

    if not config.has_api_key():
        return {
            "session_id": session_id,
            "reply": "아직 Gemini API 키가 설정 안 됐어요. 관리자(/admin) 페이지에서 먼저 등록해주세요.",
            "stage": "intent",
            "mode": sess["mode"],
            "options": [],
        }

    if user_msg in RESET_WORDS:
        sess["history"] = []
        sess["pending_incident"] = None
        if sess["mode"] == "scholarship":
            sess["scholarship"] = _new_scholarship_state()
            reply = "처음부터 다시 시작할게! 몇 학년이야?"
        elif sess["mode"] == "dual_major":
            sess["dual_major"] = _new_dual_major_state()
            reply = "처음부터 다시 확인해볼게! 지금 재학 중이야, 아니면 휴학/복학예정이야?"
        else:
            sess["scholarship"] = _new_scholarship_state()
            sess["dual_major"] = _new_dual_major_state()
            reply = "좋아, 처음부터! 장학금이 궁금해, 아니면 복수전공/이수과목 인정이 궁금해?"
        return {"session_id": session_id, "reply": reply, "stage": "intent", "mode": sess["mode"], "options": []}

    if user_msg in EXIT_WORDS:
        reply = "여기까지 도와줄게! 필요하면 언제든 다시 시작해줘."
        sess["history"].append(f"학생: {user_msg}")
        sess["history"].append(f"AI: {reply}")
        return {"session_id": session_id, "reply": reply, "stage": "exit", "mode": sess["mode"], "options": []}

    # 도움말 — RESET/EXIT와 달리 세션 상태(mode/scholarship/dual_major/pending_incident/
    # awaiting_lookup_id)는 하나도 건드리지 않는다. 상담이나 신고 접수 중간에 물어봐도
    # 답만 해주고 하던 흐름 그대로 이어갈 수 있어야 하기 때문 (실사용자 요청: "내가 여태
    # 구현해놓은 기능들을 도움말?형태로 해서 볼 수 있도록").
    if _looks_like_help_request(user_msg):
        sess["history"].append(f"학생: {user_msg}")
        sess["history"].append(f"AI: {HELP_MESSAGE}")
        return {
            "session_id": session_id,
            "reply": HELP_MESSAGE,
            "stage": "help",
            "mode": sess["mode"],
            "options": [],
        }

    # 캠퍼스 안전신고 후속 질문(위치/인원수/이름·연락처) 답변 대기 중이면, 이번 메시지는
    # 그 답으로 취급해서 해석한다 — 계속 캐물을지 여기서 접수를 끝낼지는
    # `_handle_incident_followup_message` 안에서 판단(실사용자: "112신고하면 계속
    # 물어보잖아 그런걸 모델로해서 뭔가 계속 물어보면 좋겠어. 대신 언제든 신고를 끝낼
    # 수는 잇도록"). RESET/EXIT보다는 뒤, 학번조회 대기(awaiting_lookup_id)보다는 앞에
    # 둬서 두 대기 상태가 동시에 걸릴 일이 없게 함.
    if sess.get("pending_incident"):
        result = _handle_incident_followup_message(sess, user_msg)
        sess["history"].append(f"학생: {user_msg}")
        sess["history"].append(f"AI: {result['reply']}")
        result["session_id"] = session_id
        result["mode"] = sess["mode"]
        return result

    # 학번 기반 세션 복원 (2단계: 학번 대기 -> 조회). _LOOKUP_TRIGGER_RE/_handle_application_lookup
    # 주석 참고 — 새 탭이라 예전 대화가 없어도, 학번만 알려주면 저번 신청서 상태(특히 반려
    # 여부)를 바로 알려주고 필요하면 수정·재제출 흐름으로 바로 이어붙인다.
    if sess.get("awaiting_lookup_id"):
        m = _LOOKUP_STUDENT_ID_RE.search(user_msg)
        if not m:
            reply = "학번(숫자)으로 알려줘야 찾을 수 있어! 다시 한번 알려줄래?"
            sess["history"].append(f"학생: {user_msg}")
            sess["history"].append(f"AI: {reply}")
            return {"session_id": session_id, "reply": reply, "stage": "intent", "mode": sess["mode"], "options": []}
        sess["awaiting_lookup_id"] = False
        result = _handle_application_lookup(sess, m.group())
        sess["history"].append(f"학생: {user_msg}")
        sess["history"].append(f"AI: {result['reply']}")
        result["session_id"] = session_id
        result["mode"] = sess["mode"]
        result.setdefault("options", [])
        return result

    if _LOOKUP_TRIGGER_RE.search(user_msg):
        sess["awaiting_lookup_id"] = True
        reply = "학번 알려주면 예전에 낸 신청 내역 찾아줄게!"
        sess["history"].append(f"학생: {user_msg}")
        sess["history"].append(f"AI: {reply}")
        return {"session_id": session_id, "reply": reply, "stage": "intent", "mode": sess["mode"], "options": []}

    sess["history"].append(f"학생: {user_msg}")

    # 실사용자 요청: "안전신고 탭을 안 들어가고 메인 챗봇에서 신고할 수 있도록" — 진행 중인
    # 상담(모드 None/scholarship/dual_major 무엇이든)을 방해하지 않고 아무 때나 툭 던진
    # 신고를 그 자리에서 감지함. 값싼 키워드 사전필터를 통과했을 때만 LLM으로 최종
    # 확인하고(_try_handle_incident_report), 신고가 아니라고 판정되면(None 반환) 아래로
    # 그대로 흘러내려가서 원래 하던 라우팅/상담을 이어감 — 신고 감지가 sess["mode"]를 절대
    # 건드리지 않으므로 신고 후에도 하던 상담을 정확히 그대로 이어갈 수 있음. 맞으면
    # sess["pending_incident"]로 넘어가 112 지령실처럼 위치/인원수/이름·연락처를 몇 차례
    # 더 캐묻고(실사용자: "112신고하면 계속 물어보잖아 그런걸 모델로해서 뭔가 계속
    # 물어보면 좋겠어"), 다음 메시지부터는 위의 pending_incident 분기가 이어받는다.
    if _looks_like_incident(user_msg):
        incident_result = _try_handle_incident_report(sess, user_msg)
        if incident_result is not None:
            sess["history"].append(f"AI: {incident_result['reply']}")
            incident_result["session_id"] = session_id
            incident_result["mode"] = sess["mode"]
            return incident_result

    convo = "\n".join(sess["history"])

    detected = detect_mode_keywords(user_msg)
    if sess["mode"] is None:
        mode = detected
        if mode is None:
            classified = bot_core.classify_intent(user_msg)
            if classified == "incident_report":
                # 키워드 사전필터에 안 걸린 창의적인 표현(예: "복도에 누가 쓰러져있어요")도
                # 첫 메시지라면 classify_intent가 한 번 더 잡아줄 수 있음 — 여기서도 최종
                # 확인은 반드시 _try_handle_incident_report(LLM 재확인)를 거쳐야 하고, 맞으면
                # 마찬가지로 후속 질문부터 시작함.
                incident_result = _try_handle_incident_report(sess, user_msg)
                if incident_result is not None:
                    sess["history"].append(f"AI: {incident_result['reply']}")
                    incident_result["session_id"] = session_id
                    incident_result["mode"] = None
                    return incident_result
                mode = None
            else:
                mode = classified if classified in ("scholarship", "dual_major") else None
        if mode is None:
            # 예전엔 여기서 고정 문구("장학금이 궁금한 거야, ...")를 무조건 그대로 반복해서,
            # 학생이 인사("안녕")를 하거나 "왜 같은 말만 반복하냐"고 항의해도 그 말을 전혀
            # 못 알아듣고 계속 똑같은 문장만 뱉는 앵무새 버그가 있었음(실사용자 리포트로 확인).
            # 이제는 실제 대화 내용을 읽고 자연스럽게 반응한 다음에 물어보게 함.
            reply = bot_core.generate_intent_clarify_reply(convo)
            sess["history"].append(f"AI: {reply}")
            return {"session_id": session_id, "reply": reply, "stage": "intent", "mode": None, "options": []}
        sess["mode"] = mode
    elif detected and detected != sess["mode"]:
        sess["mode"] = detected

    mode = sess["mode"]
    if mode == "scholarship":
        result = handle_scholarship_turn(sess["scholarship"], convo, user_msg)
    else:
        result = handle_dual_major_turn(session_id, sess["dual_major"], convo, user_msg)

    sess["history"].append(f"AI: {result['reply']}")
    result["session_id"] = session_id
    result["mode"] = mode
    result.setdefault("options", [])
    return result


# ---------------- 이수과목 인정신청서 승인 (직원용, HITL) ----------------

@app.get("/api/recognition/applications")
def list_recognition_applications(status: Optional[str] = None):
    apps = load_recognition_apps()
    if status:
        apps = [a for a in apps if a.status == status]
    apps_sorted = sorted(apps, key=lambda a: a.created_at, reverse=True)
    return [a.model_dump() for a in apps_sorted]


@app.get("/api/recognition/applications/{app_id}/docx")
def download_recognition_application_docx(app_id: str):
    """공식 '부(복수)전공 이수과목 인정신청서' 양식에 맞춰 채운 .docx 다운로드.
    과목뿐 아니라 성명/학번/학년/소속 단과대학까지 student_info/student_info_confirm
    단계에서 이미 학생 본인 확인을 받아 채워둔 상태. 실제 승인은 학생이 이 파일을 들고
    학과장을 찾아다니는 게 아니라 /staff에서 학과장 -> 복수전공학과장(둘은 순서 무관) ->
    행정처 순으로 3단계 전산승인이 진행되며, 이 파일은 그 진행상황이 그대로 반영되는
    참고·보관용 문서임 — AI는 초안 작성까지만, 최종 승인 3단계는 전부 사람(HITL, /staff)."""
    apps = load_recognition_apps()
    target = next((a for a in apps if a.id == app_id), None)
    if target is None:
        return JSONResponse(status_code=404, content={"error": "신청서를 찾을 수 없어요"})
    docx_bytes = recognition_doc.build_recognition_docx(target)
    filename = f"이수과목인정신청서_{target.id}.docx"
    # 파일명에 한글이 들어가서 그냥 filename="..."만 주면 일부 브라우저/클라이언트에서
    # 깨질 수 있음 -> RFC 6266대로 filename*(UTF-8 인코딩)도 같이 내려줌
    quoted = urllib.parse.quote(filename)
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": (
                f'attachment; filename="recognition_{target.id}.docx"; '
                f"filename*=UTF-8''{quoted}"
            )
        },
    )


# 3단계 승인 — 실사용자 요청: "학과장/복수전공학과장 승인 -> 행정처 최종승인, 총 세 번의
# 승인이 되면 전산에 반영". 학과장 2명은 순서 무관(병렬), 행정처는 반드시 그 둘이 전부
# 승인된 뒤에만 처리 가능 — 이 순서는 화면(staff.html)에서 버튼을 숨기는 것만으로 끝내지
# 않고 여기 서버에서도 다시 확인함(화면 조작/직접 API 호출로 순서를 건너뛸 수 없게).
STAGE_FIELDS = {
    "home_chair": "home_chair_approval",
    "dual_chair": "dual_chair_approval",
    "admin": "admin_approval",
}
STAGE_LABELS = {"home_chair": "학과장", "dual_chair": "복수전공학과장", "admin": "행정처"}


class DecisionIn(BaseModel):
    stage: str  # "home_chair" | "dual_chair" | "admin"
    decision: str  # "approved" | "rejected"
    decided_by: Optional[str] = None
    note: Optional[str] = None


@app.post("/api/recognition/applications/{app_id}/decide")
def decide_recognition_application(app_id: str, body: DecisionIn):
    if body.stage not in STAGE_FIELDS:
        return JSONResponse(
            status_code=400, content={"error": "stage는 home_chair/dual_chair/admin 중 하나여야 해요"}
        )
    if body.decision not in ("approved", "rejected"):
        return JSONResponse(status_code=400, content={"error": "decision은 approved 또는 rejected여야 해요"})

    # 읽기부터 쓰기까지 전역 락으로 감싸서, 다른 직원이 동시에 처리하거나 학생이 동시에
    # 새 신청서를 제출해도 파일 저장이 서로 덮어쓰지 않게 함.
    with RECOGNITION_APPS_LOCK:
        apps = load_recognition_apps()
        target = next((a for a in apps if a.id == app_id), None)
        if target is None:
            return JSONResponse(status_code=404, content={"error": "신청서를 찾을 수 없어요"})

        if target.status in ("approved", "rejected"):
            return JSONResponse(status_code=400, content={"error": "이미 최종 처리(승인/반려)된 신청서예요"})

        step = getattr(target, STAGE_FIELDS[body.stage])
        if step.status != "pending":
            return JSONResponse(
                status_code=400, content={"error": f"{STAGE_LABELS[body.stage]} 단계는 이미 처리됐어요"}
            )

        # 행정처는 학과장·복수전공학과장이 둘 다 승인된 뒤에만 처리 가능 — 순서를 건너뛰려는
        # 시도는 여기서 막음(화면에서 버튼을 숨겨도 API를 직접 호출하면 뚫릴 수 있어서 이중 검증).
        if body.stage == "admin":
            if target.home_chair_approval.status != "approved" or target.dual_chair_approval.status != "approved":
                return JSONResponse(
                    status_code=400,
                    content={"error": "학과장·복수전공학과장 승인이 둘 다 끝나야 행정처 처리가 가능해요"},
                )

        step.status = body.decision
        step.decided_by = body.decided_by or STAGE_LABELS[body.stage]
        step.decided_at = datetime.now(timezone.utc).isoformat()
        step.note = body.note

        if body.decision == "rejected":
            # 어느 단계에서든 반려되면 그 즉시 전체 신청서가 반려로 확정됨 — 이미 승인됐던
            # 다른 단계의 기록은 지우지 않고 그대로 남겨둠(실제로 있었던 일이니까). 학생은
            # 반려 사유(note)를 보고 수정해서 챗봇으로 다시 신청하면 됨(새 신청서로 재제출).
            target.status = "rejected"
            # 실사용자 요청: "반려되는 순간 바로 알림" — 학생이 챗봇을 다시 열 때까지 기다리지
            # 않고, 반려가 확정되는 이 자리에서 바로 이메일 발송. 실패해도(주소 없음/API 키
            # 미설정/네트워크 오류) 신청서 반려 처리 자체는 그대로 진행됨(notify.send_email이
            # 예외를 던지지 않게 만들어둠).
            # 실사용자 요청: "이메일이랑 교수님들이나 직원들이 보는 페이지는 무조건 존댓말로"
            # — 학생용 챗봇(반말)과 달리 이메일은 공식 통지문이라 전부 존댓말(합쇼체)로 씀.
            notify.send_email(
                target.student_email,
                subject="[Uni-VOC] 이수과목 인정신청서가 반려되었습니다",
                text_body=(
                    f"{target.student_name}님, 신청하신 이수과목 인정신청서(타전공: {target.target_major})가 "
                    f"반려되었습니다.\n\n{_rejection_reason(target, formal=True)}\n\n"
                    "챗봇에서 '신청확인'이라고 말씀하시고 학번을 알려주시면 바로 이어서 재제출하실 수 있습니다."
                ),
                status="rejected",
            )
        elif (
            target.home_chair_approval.status == "approved"
            and target.dual_chair_approval.status == "approved"
            and target.admin_approval.status == "approved"
        ):
            # 세 단계 전부 승인돼야만 전산 반영(최종 승인) 처리됨
            target.status = "approved"
            notify.send_email(
                target.student_email,
                subject="[Uni-VOC] 이수과목 인정신청서가 승인되었습니다",
                text_body=(
                    f"{target.student_name}님, 신청하신 이수과목 인정신청서(타전공: {target.target_major})가 "
                    "3단계 승인 절차를 모두 마치고 전산 반영까지 완료되었습니다. 축하드립니다!"
                ),
                status="approved",
            )
        # 셋 중 일부만 승인된 상태면 status는 계속 "pending"으로 남음(아직 전산 반영 전)

        save_recognition_apps(apps)
        return target.model_dump()


# 직원 화면(staff.html)에서 여러 신청서를 체크박스로 골라 한 번에 지우는 기능 — 실사용자
# 요청("선택삭제+전체선택 기능 추가"). 승인/반려 감사기록까지 포함해서 완전히 삭제되는
# 되돌릴 수 없는 작업이라, 화면에서도 확인창을 한 번 거치게 해뒀음(아래 staff.html 참고).
class DeleteApplicationsIn(BaseModel):
    ids: list[str]


@app.post("/api/recognition/applications/delete")
def delete_recognition_applications(body: DeleteApplicationsIn):
    if not body.ids:
        return JSONResponse(status_code=400, content={"error": "삭제할 신청서를 선택해줘"})
    with RECOGNITION_APPS_LOCK:
        apps = load_recognition_apps()
        ids_set = set(body.ids)
        remaining = [a for a in apps if a.id not in ids_set]
        deleted_count = len(apps) - len(remaining)
        save_recognition_apps(remaining)
    return {"deleted": deleted_count}


# ---------------- 캠퍼스 안전/시설 신고 ----------------
# 실사용자 요청: "정문에 신천지 돌아다녀요 이렇게 레포트하면 즉각적으로 보안팀이나 다른
# 행정팀에 레포트가 간다던가" — 챗봇과 완전히 분리된 독립 신고 폼(/report) +
# 보안/시설/기타 탭으로 나눠보는 직원 대시보드(/incidents, 실시간 폴링 갱신).


# `/api/incidents`(독립 신고 폼 `/report`용)와 메인 챗봇 대화 중 감지된 신고(아래
# `_try_handle_incident_report`) 둘 다 결국 "신고 한 건을 저장한다"는 같은 일을 하므로
# 저장 로직을 여기 하나로 합쳐서 공유함 — 검증 규칙이나 저장 방식이 바뀌면 한 곳만 고치면 됨.
def _save_new_incident_report(
    category: str,
    description: str,
    location: Optional[str] = None,
    people_count: Optional[str] = None,
    reporter_name: Optional[str] = None,
    reporter_contact: Optional[str] = None,
) -> IncidentReport:
    report = IncidentReport(
        id=uuid.uuid4().hex[:12],
        category=category,
        description=description,
        location=location or None,
        people_count=people_count or None,
        reporter_name=reporter_name or None,
        reporter_contact=reporter_contact or None,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    with INCIDENT_REPORTS_LOCK:
        reports = load_incident_reports()
        reports.append(report)
        save_incident_reports(reports)
    return report


# 챗봇의 112식 후속 질문(`_handle_incident_followup_message`)이 이미 저장된 신고를
# 실시간으로 보강할 때, 그리고 직원이 대시보드에서 직접 정보를 채워 넣는
# `/api/incidents/{id}/update`가 저장할 때 — 결국 둘 다 "이미 있는 신고 건의 일부
# 필드만 덮어쓴다"는 같은 일이라 여기로 합침. fields로 넘긴 키만 덮어쓰고, 넘기지
# 않은 필드는 기존 값을 그대로 둔다. 값을 None으로 명시적으로 넘기면(예: 익명 처리 시
# reporter_name=None) 그 필드를 지운다.
def _sync_incident_fields(report_id: str, **fields) -> Optional[IncidentReport]:
    with INCIDENT_REPORTS_LOCK:
        reports = load_incident_reports()
        target = next((r for r in reports if r.id == report_id), None)
        if target is None:
            return None
        for key, value in fields.items():
            setattr(target, key, value)
        save_incident_reports(reports)
        return target


class IncidentReportIn(BaseModel):
    category: str
    description: str
    location: Optional[str] = None
    reporter_name: Optional[str] = None
    reporter_contact: Optional[str] = None


@app.post("/api/incidents")
def create_incident_report(body: IncidentReportIn):
    category = (body.category or "").strip()
    description = (body.description or "").strip()
    if category not in INCIDENT_CATEGORIES:
        return JSONResponse(
            status_code=400,
            content={"error": f"category는 {'/'.join(INCIDENT_CATEGORIES)} 중 하나여야 해요"},
        )
    if not description:
        return JSONResponse(status_code=400, content={"error": "신고 내용을 입력해주세요"})

    report = _save_new_incident_report(
        category=category,
        description=description,
        location=(body.location or "").strip() or None,
        reporter_name=(body.reporter_name or "").strip() or None,
        reporter_contact=(body.reporter_contact or "").strip() or None,
    )
    return report.model_dump()


@app.get("/api/incidents")
def list_incident_reports(category: Optional[str] = None, status: Optional[str] = None):
    reports = load_incident_reports()
    if category:
        reports = [r for r in reports if r.category == category]
    if status:
        reports = [r for r in reports if r.status == status]
    reports_sorted = sorted(reports, key=lambda r: r.created_at, reverse=True)
    return [r.model_dump() for r in reports_sorted]


class ResolveIncidentIn(BaseModel):
    resolved_by: Optional[str] = None
    note: Optional[str] = None


@app.post("/api/incidents/{report_id}/resolve")
def resolve_incident_report(report_id: str, body: ResolveIncidentIn):
    with INCIDENT_REPORTS_LOCK:
        reports = load_incident_reports()
        target = next((r for r in reports if r.id == report_id), None)
        if target is None:
            return JSONResponse(status_code=404, content={"error": "신고를 찾을 수 없습니다"})
        target.status = "resolved"
        target.resolved_by = body.resolved_by
        target.resolved_at = datetime.now(timezone.utc).isoformat()
        target.resolved_note = body.note
        save_incident_reports(reports)
        return target.model_dump()


@app.post("/api/incidents/{report_id}/reopen")
def reopen_incident_report(report_id: str):
    # 실수로 처리완료 처리했을 때 되돌릴 수 있게 — 직원 대시보드 UX 상 필요한 안전장치.
    with INCIDENT_REPORTS_LOCK:
        reports = load_incident_reports()
        target = next((r for r in reports if r.id == report_id), None)
        if target is None:
            return JSONResponse(status_code=404, content={"error": "신고를 찾을 수 없습니다"})
        target.status = "open"
        target.resolved_by = None
        target.resolved_at = None
        target.resolved_note = None
        save_incident_reports(reports)
        return target.model_dump()


@app.post("/api/incidents/{report_id}/update")
def update_incident_report(report_id: str, body: IncidentUpdateIn):
    # 실사용자 피드백: "너무 세세하게 물어보면 긴급/응급때 귀찮을 수 있으니 일단 접수 —
    # 사람이 이야기해주면 직원용 페이지에서 업데이트되는 거로". 챗봇이 접수 시점에 안
    # 물어본 위치(못 들었을 때)/인원수/이름/연락처를, 직원이 현장에서 직접 듣거나
    # 확인한 뒤 여기서 채워 넣는다. body에 실제로 넘어온 필드만 덮어씀(model_fields_set) —
    # 그래야 프론트에서 일부 칸만 채워 보내도 나머지 기존 값이 사라지지 않는다. 빈 문자열을
    # 명시적으로 보내면(예: "" 로 지우기 버튼) 그 필드를 null로 지운다.
    updates = {
        field: ((getattr(body, field) or "").strip() or None)
        for field in ("location", "people_count", "reporter_name", "reporter_contact")
        if field in body.model_fields_set
    }
    target = _sync_incident_fields(report_id, **updates)
    if target is None:
        return JSONResponse(status_code=404, content={"error": "신고를 찾을 수 없습니다"})
    return target.model_dump()


class DeleteIncidentsIn(BaseModel):
    ids: list[str]


@app.post("/api/incidents/delete")
def delete_incident_reports(body: DeleteIncidentsIn):
    if not body.ids:
        return JSONResponse(status_code=400, content={"error": "삭제할 신고를 선택해주세요"})
    with INCIDENT_REPORTS_LOCK:
        reports = load_incident_reports()
        ids_set = set(body.ids)
        remaining = [r for r in reports if r.id not in ids_set]
        deleted_count = len(reports) - len(remaining)
        save_incident_reports(remaining)
    return {"deleted": deleted_count}


# ---------------- 직원 접속 로그 (staff.html / incidents.html 진입 시 체크인) ----------------
# 실사용자 요청: "직원 페이지는 직원 페이지 들어갈때마다 본인의 이름과 소속을 확인해서
# 항상 로그를 남길 수 있도록 하면 좋겠어" — schemas.StaffAccessLog 주석 참고. 정식
# 로그인은 아니고(비밀번호 검증 없음), 페이지 진입 시 이름/소속을 입력받아 그 사실을
# 기록만 남기는 가벼운 체크인 방식.
class StaffAccessLogIn(BaseModel):
    name: str
    affiliation: str
    page: str


@app.post("/api/staff/access-log")
def create_staff_access_log(body: StaffAccessLogIn):
    name = (body.name or "").strip()
    affiliation = (body.affiliation or "").strip()
    page = (body.page or "").strip() or "unknown"
    if not name or not affiliation:
        return JSONResponse(status_code=400, content={"error": "이름과 소속을 모두 입력해주세요"})
    entry = StaffAccessLog(
        id=uuid.uuid4().hex[:12],
        name=name,
        affiliation=affiliation,
        page=page,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    with STAFF_ACCESS_LOGS_LOCK:
        logs = load_staff_access_logs()
        logs.append(entry)
        save_staff_access_logs(logs)
    return entry.model_dump()


@app.get("/api/staff/access-log")
def list_staff_access_logs():
    # 관리자 페이지(admin.html)에서 "누가 언제 직원 화면에 들어왔는지" 확인하는 용도 —
    # 최근 기록이 위로 오게 정렬.
    logs = load_staff_access_logs()
    logs_sorted = sorted(logs, key=lambda r: r.created_at, reverse=True)
    return [r.model_dump() for r in logs_sorted]


# ---------------- 대화 로그 조회 (관리자 페이지용) ----------------
@app.get("/api/chat-logs")
def list_chat_logs(session_id: Optional[str] = None, limit: int = 200):
    # 실사용자 요청: "사람들이 나눈 대화를 로그처럼 저장해놓을 수 있으면 좋겠는데" — 저장은
    # `_log_chat_turn`(위 /api/chat 참고)이 매 턴마다 하고, 여긴 관리자 화면에서 확인하는
    # 조회용 API. session_id로 필터링 가능, limit으로 최근 N건만(기본 200건, 로그가 계속
    # 쌓이는데 매번 전체를 다 내려주면 화면이 무거워지므로) 최신순으로 잘라서 반환.
    logs = load_chat_logs()
    if session_id:
        logs = [r for r in logs if r.session_id == session_id]
    logs_sorted = sorted(logs, key=lambda r: r.created_at, reverse=True)[: max(1, limit)]
    return [r.model_dump() for r in logs_sorted]


if __name__ == "__main__":
    import uvicorn

    print(f"[시스템] {len(load_db())}개 장학금 로드 완료 (data/scholarship_db.json)")
    uvicorn.run(app, host="127.0.0.1", port=8000)
