"""
매칭 로직 — 1차 규칙필터(여기) + 2차 LLM 소프트매칭(bot_core.soft_match_conditions,
app.py의 filter_by_soft_conditions에서 호출).
(uni_voc_장학금챗봇_MVP설계.md 6장·9장 + 0.6장 "전수검토 결과" 반영)

실제 DB 68건을 조사해보니 신청가능한 41건 전부(100%)가 special_conditions를
free-text로 갖고 있었음(예: "국민기초생활 수급자 우대", "본인 또는 부모가 대전 거주"
처럼 공지마다 표현이 제각각) — 이걸 학생이 입력한 자유텍스트와 set 교집합으로
정확매칭하면 사실상 절대 안 맞아서 있는 장학금도 다 걸러지는 버그가 있었음
(0.6장 5번에서 이미 이 문제를 지적하고 "고정 리스트 set 교집합은 폐기, LLM 소프트매칭으로
대체"를 권고함). 그래서 여기 1차 필터에서는 날짜/구조화 수치조건(재학상태·평점·소득분위)만
결정론적으로 걸러내고, 자유서술형 조건(special_conditions/major_restriction/학년조건)은
아예 안 걸러낸 채로 후보에 다 남겨두고 app.py가 2차로 LLM 보조판단을 거쳐 정리함.
"""
import re
from datetime import date

from schemas import Scholarship, SlotState

# ---------------- 지역(거주지) 하드필터 ----------------
#
# 실사용자 리포트: 인천 미추홀구 거주 학생한테 "본인 또는 보호자가 포항시에 1년 이상
# 주민등록을 두고 있는 자"가 조건인 (재)포항시장학회를 추천한 버그. special_conditions는
# 자유서술형이라 1차 필터에서 안 걸러내고 2차 LLM 소프트매칭(bot_core.soft_match_conditions)
# 에만 맡겨뒀는데, 그 판단이 이번처럼 틀릴 수 있고(soft_match_conditions는 확률적 판단이라
# 100% 신뢰 불가) LLM 호출 자체가 실패하면 filter_by_soft_conditions가 조건 없이 전부
# 통과시키는 폴백까지 있어서 이중으로 뚫릴 수 있었음. "본인/보호자가 특정 지역에
# 거주/주민등록"류 조건은 사실 지명만 비교하면 기계적으로 100% 정확히 판단 가능한
# 영역이라, 여기 1차 필터에서 LLM 없이 바로 걸러냄(전국 행정구역 완전한 사전은 아니고
# DB에 실제 등장하는 지명 위주로 채워둠 — 모르는 지명은 그냥 기존처럼 LLM 소프트매칭에
# 맡기고, 여기서 잘못 걸러내는 일(false positive)은 없게 설계함).
_REGION_ALIASES: dict[str, set[str]] = {
    "서울": {"서울", "서울시", "서울특별시"},
    "부산": {"부산", "부산시", "부산광역시"},
    "대구": {"대구", "대구시", "대구광역시"},
    "인천": {"인천", "인천시", "인천광역시"},
    "광주": {"광주", "광주시", "광주광역시"},
    "대전": {"대전", "대전시", "대전광역시"},
    "울산": {"울산", "울산시", "울산광역시"},
    "세종": {"세종", "세종시", "세종특별자치시"},
    "경기": {"경기", "경기도"},
    "강원": {"강원", "강원도", "강원특별자치도"},
    "충북": {"충북", "충청북도"},
    "충남": {"충남", "충청남도"},
    "전북": {"전북", "전라북도", "전북특별자치도"},
    "전남": {"전남", "전라남도"},
    "경북": {"경북", "경상북도"},
    "경남": {"경남", "경상남도"},
    "제주": {"제주", "제주도", "제주특별자치도"},
    # 실제 DB(scholarship_db.json)에 등장하는 시/군/구 단위 — 새 공지 크롤링하면서
    # 지명이 더 나오면 여기 계속 추가하면 됨.
    "포항": {"포항", "포항시"},
    "경주": {"경주", "경주시"},
    "구미": {"구미", "구미시"},
    "경산": {"경산", "경산시"},
    "안동": {"안동", "안동시"},
    "김천": {"김천", "김천시"},
    "군산": {"군산", "군산시"},
    "익산": {"익산", "익산시"},
    "정읍": {"정읍", "정읍시"},
    "화성": {"화성", "화성시"},
    "창원": {"창원", "창원시"},
    "진주": {"진주", "진주시"},
    "천안": {"천안", "천안시"},
    "청주": {"청주", "청주시"},
    "달서구": {"달서구"},
    "광산구": {"광산구"},
    "미추홀구": {"미추홀구", "미추홀"},
}
#
# 실제 DB 재점검 결과: "대전광역시에 주소를 두고 있는 학생"처럼 "거주"/"주민등록" 없이
# "주소"만으로 거주요건을 표현하는 공지도 있어서(대전 성취(대), 인천인희망드림, 익산 등
# 8건) "거주"/"주민등록"만 보던 기존 정규식이 이런 조건을 하드필터에서 놓치고 있었음.
# DB 전체를 스캔해서 special_conditions 안의 "주소" 언급이 전부 거주요건 표현으로만
# 쓰이고(서류 제출 주소 등 다른 용도로 쓰인 사례 0건) 있는 걸 확인했으므로 안전하게 추가.
_RESIDENCY_CUE_RE = re.compile(r"(거주|주민등록|주소)")


def _region_tokens_in(text: str) -> set[str]:
    """text 안에 등장하는 지명 그룹 키(예: '포항', '대구') 집합을 반환."""
    if not text:
        return set()
    return {key for key, forms in _REGION_ALIASES.items() if any(f in text for f in forms)}


def _region_conflicts(student_region: str | None, condition_texts: list[str]) -> bool:
    """학생이 말한 지역과 장학금의 '거주/주민등록' 조건이 사전에 등록된, 서로 다른 지명을
    가리키는 게 확실할 때만 True. 지명 사전에 없거나 학생이 지역을 안 말했으면 절대 True를
    반환하지 않음 — "모르면 배제하지 않는다"는 기존 소프트매칭 원칙과 동일하게, 여기서도
    확실한 경우에만 걸러낸다."""
    if not student_region:
        return False
    student_keys = _region_tokens_in(student_region)
    if not student_keys:
        return False
    for cond in condition_texts:
        if not cond or not _RESIDENCY_CUE_RE.search(cond):
            continue
        cond_keys = _region_tokens_in(cond)
        if cond_keys and not (cond_keys & student_keys):
            return True
    return False


# ---------------- 학과제한 "명확히 일치" 강제포함 ----------------
#
# 실사용자 리포트: 시각디자인 전공 학생한테 "시각디자인 전공자 및 판화학과"가 학과제한인
# 장학금이 2차 LLM 소프트매칭(soft_match_conditions)에서 걸러지는 문제. 학과제한은 원래
# 자유서술형이라 2차 LLM 판단에 맡기는 영역이지만, "학생이 말한 전공명이 공지 원문에
# 문자 그대로 들어있는" 경우는 지명 비교(_region_conflicts)처럼 규칙만으로 100% 확실하게
# 판단 가능함 — 이런 명확한 경우엔 LLM이 뭐라고 판단하든(확률적 판단이라 틀릴 수 있음)
# 무조건 후보로 유지시킴. 반대방향(불일치라고 배제)으로는 절대 안 씀 — "관련 학과 포함"
# 처럼 표현이 다양해서 안 맞는다고 자동배제하면 오배제 위험이 큼(기존 소프트매칭 원칙과
# 동일하게 여기서도 "확실한 포함"에만 개입하고 배제는 절대 안 함).
_MAJOR_SUFFIXES = ("학과", "전공", "학부", "계열", "과")


def _major_core(text: str) -> str:
    """학과명 비교용 — 흔한 접미사(학과/전공/학부/계열/과) 제거. 학생 발화("시각디자인학과")와
    공지 원문("시각디자인 전공자")이 접미사만 다르게 표현되는 경우가 많아서, 접미사를 뗀
    "핵심 학과명"을 공지 원문 안에서 그대로 찾아야 이런 표현 차이에도 안정적으로 매칭됨."""
    text = (text or "").strip()
    for suf in _MAJOR_SUFFIXES:
        if text.endswith(suf) and len(text) > len(suf):
            return text[: -len(suf)]
    return text


def major_clearly_matches(student_major: str | None, major_restriction: str | None) -> bool:
    """학생이 말한 전공의 핵심 학과명이 공지의 학과제한 원문에 그대로 포함되면 True.
    2글자 미만인 핵심명은(오탐 위험이 커서) 매칭 대상에서 제외함."""
    if not student_major or not major_restriction:
        return False
    core = _major_core(student_major)
    if len(core) < 2:
        return False
    return core in major_restriction


def _status_matches(student_status: str | None, allowed: list[str]) -> bool:
    """재학상태 비교 — 공지 원문(재학생/신입생/휴학생...)과 학생 자유발화(재학/휴학...)가
    독립적으로 추출돼서 표현이 완전히 똑같으란 보장이 없음("재학" vs "재학생" 등).
    그래서 정확일치 대신 부분포함(양방향)으로 비교함 — 학생이 실제 재학생인데
    "재학"이라고만 말했다고 "재학생" 조건에서 튕겨나가는 걸 막기 위함."""
    student_status = (student_status or "재학생").strip()
    for a in allowed:
        a = a.strip()
        if not a:
            continue
        if student_status in a or a in student_status:
            return True
    return False


def is_currently_open(s: Scholarship, today: date) -> bool:
    if not s.is_open_application:
        return False
    if s.benefit_type not in ("장학금", "근로장학"):
        # 대출/이자지원류는 기본 매칭에서는 제외 (필요시 별도 안내)
        return False
    if s.apply_end:
        try:
            if date.fromisoformat(s.apply_end) < today:
                return False
        except ValueError:
            pass
    return True


def match_scholarships(
    db: list[Scholarship], state: SlotState, today: date | None = None
) -> list[tuple[Scholarship, bool]]:
    """반환: (장학금, needs_income_check) 튜플 리스트"""
    today = today or date.today()
    results: list[tuple[Scholarship, bool]] = []

    for s in db:
        if not is_currently_open(s, today):
            continue
        e = s.eligibility

        if e.student_status and not _status_matches(state.student_status, e.student_status):
            continue

        if _region_conflicts(state.region, e.special_conditions):
            continue

        if e.gpa_requirement_percent is not None:
            if state.gpa is None:
                continue
            student_gpa_percent = state.gpa / 4.5 * 100
            if student_gpa_percent < e.gpa_requirement_percent:
                continue

        needs_income_check = False
        if e.income_bracket_max is not None:
            if state.income_bracket in (None, "모름"):
                needs_income_check = True
            else:
                try:
                    bracket_num = int(
                        state.income_bracket.replace("구간", "").strip()
                    )
                    if bracket_num > e.income_bracket_max:
                        continue
                except ValueError:
                    needs_income_check = True

        # special_conditions / major_restriction / grade_or_semester_note는 여기서
        # 걸러내지 않음 — 전부 자유서술형 텍스트라 정확매칭이 불가능함(위 모듈 docstring 참고).
        # app.py의 filter_by_soft_conditions()가 LLM 보조판단으로 2차 정리함.

        results.append((s, needs_income_check))

    return results
