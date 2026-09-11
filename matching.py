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
from datetime import date

from schemas import Scholarship, SlotState


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
