"""
복수전공 자격요건 판정 — 순수 규칙 기반(결정론적), LLM 사용 안 함.

돈/졸업요건에 영향 주는 판단이라 AI가 혼자 판정하지 않는다는 설계 원칙 그대로 적용.
LLM은 자연어 → 슬롯 추출(bot_core.extract_dualmajor_slots)에만 쓰고,
"가능/불가능" 최종 판정은 이 파일의 결정론적 함수가 담당함.

근거:
- 2026학년도 1학기 복수전공(부전공) 신청 안내 (yu.ac.kr, articleNo=227973509)
- 영남대학교 2026학년도 교육과정 이수지침 II장 6.복수전공 (특히 6.마)
"""
from schemas import DualMajorState, EligibilityResult

# 졸업학점 기준별 최소 수료학점 (1학년 수료 기준)
COMPLETION_CREDITS_BY_GRAD_BASIS = {
    120: 30,
    130: 32,
    140: 35,
    150: 37,
    160: 40,
}

# 타 대학(학과·전공) 소속 학생이 "목표"로 복수전공 지정할 수 없는 대학/학부
# (이수지침 II장 6.마 — 사범대학·예술대학은 내부 이동에 한해 예외 있음)
EXCLUDED_TARGET_COLLEGES = {
    "건축학부", "건축학전공",
    "스마트모빌리티학과",
    "항공운송학과",
    "산업경영학과",
    "의과대학", "의예과", "의학과",
    "약학대학", "약학과",
    "체육학부",
    "사범대학",
    "예술대학",
}

# 사범대학/예술대학 내부에서만 허용되는 특례가 있는 대상 (완전 차단 아님)
INTERNAL_EXCEPTION_COLLEGES = {"사범대학", "예술대학"}

NOT_APPLICABLE_STATUS = {"휴학", "휴학중", "졸업예정", "2월졸업예정", "제적예정"}
OK_STATUS = {"재학", "복학예정"}


def _normalize_status(raw: str) -> str:
    s = (raw or "").replace(" ", "")
    for bad in NOT_APPLICABLE_STATUS:
        if bad in s:
            return bad
    for ok in OK_STATUS:
        if ok in s:
            return ok
    return s


def check_dual_major_eligibility(state: DualMajorState) -> EligibilityResult:
    passed: list[str] = []
    failed: list[str] = []
    notes: list[str] = []

    # 1. 재학상태
    status = _normalize_status(state.student_status or "")
    if status in OK_STATUS:
        passed.append("재학생 또는 복학예정자 조건 충족")
    else:
        failed.append(
            f"재학상태 조건 미충족 (휴학 중·졸업예정·제적예정자는 신청 불가, 입력값: '{state.student_status}')"
        )

    # 2. 이수 학기
    if state.completed_semesters is not None and state.completed_semesters >= 2:
        passed.append(f"2개 학기 이상 이수 조건 충족 ({state.completed_semesters}학기)")
    else:
        failed.append(
            f"2개 학기 이상 이수해야 함 (현재 {state.completed_semesters}학기)"
        )

    # 3. 수료학점
    basis = state.grad_credit_basis
    required = COMPLETION_CREDITS_BY_GRAD_BASIS.get(basis) if basis else None
    if required is None:
        failed.append(
            f"졸업학점 기준을 확인할 수 없음 (입력값: {basis}) — 120/130/140/150/160 중 하나여야 함"
        )
    elif state.earned_credits is not None and state.earned_credits >= required:
        passed.append(
            f"수료학점 조건 충족 (졸업학점 {basis}학점 기준 {required}학점 이상 필요, 현재 {state.earned_credits}학점)"
        )
    else:
        failed.append(
            f"수료학점 부족 (졸업학점 {basis}학점 기준 {required}학점 이상 필요, 현재 {state.earned_credits}학점)"
        )

    # 4. 목표 학과 제외 대상 여부
    target = (state.target_major or "").strip()
    home = (state.home_college or "").strip()
    hit = next((c for c in EXCLUDED_TARGET_COLLEGES if c and c in target), None)
    if hit:
        if hit in INTERNAL_EXCEPTION_COLLEGES and home and hit in home:
            notes.append(
                f"'{hit}' 소속 학생의 내부 학과 간 이동은 예외적으로 허용될 수 있음 "
                f"(세부 제한 있음 — 수업학적팀 확인 필요, ☎ 053-810-1095)"
            )
            passed.append(f"목표 학과 제한 — 소속대학 내부 이동으로 예외 대상일 수 있음")
        else:
            failed.append(
                f"'{hit}'은(는) 타 대학(학과·전공) 소속 학생의 복수전공 목표로 지정할 수 없음"
            )
    else:
        passed.append("목표 학과 제한 대상 아님")

    eligible = len(failed) == 0
    if not eligible:
        notes.append("최종 판정은 참고용이며, 실제 신청 가능 여부는 수업학적팀(☎ 053-810-1095) 확인이 필요함")

    return EligibilityResult(eligible=eligible, passed=passed, failed=failed, notes=notes)
