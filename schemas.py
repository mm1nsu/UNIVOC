"""
데이터 구조 정의 — 건드릴 필요 없음
(uni_voc_장학금챗봇_MVP설계.md 2장·5장 스키마 그대로)
"""
from typing import Optional
from pydantic import BaseModel


class Eligibility(BaseModel):
    student_status: list[str] = []
    grade_or_semester_note: Optional[str] = None
    major_restriction: Optional[str] = None
    gpa_requirement_raw: Optional[str] = None
    gpa_requirement_percent: Optional[float] = None
    income_bracket_max: Optional[int] = None
    income_bracket_priority_note: Optional[str] = None
    credit_requirement: Optional[str] = None
    special_conditions: list[str] = []
    other_conditions: Optional[str] = None


class Scholarship(BaseModel):
    id: Optional[str] = None  # 저장 시 서버가 uuid 부여
    doc_type: str = "장학공지"          # DOCUMENT_TYPE 그대로 (장학공지/기타공지)
    category: str = "교내장학"          # CATEGORY 그대로
    name: str
    apply_start: Optional[str] = None
    apply_end: Optional[str] = None
    amount: str = "정보 없음"
    is_open_application: bool = True
    benefit_type: str = "장학금"        # 장학금/근로장학/대출/이자지원/등록금지원
    eligibility: Eligibility = Eligibility()
    required_documents: list[str] = ["없음"]
    how_to_apply: str = "정보 없음"
    application_steps: list[str] = []
    notes: Optional[str] = None
    source_url: Optional[str] = None
    target_year: str = "2026"


class IntentClassification(BaseModel):
    """학생 메시지가 어떤 상담 종류인지 분류 — 통합 챗봇 진입 라우팅용.
    이것도 최종 판단은 아니고(상담 종류일 뿐 돈/졸업요건과 무관), 애매하면
    unclear로 두고 사람에게 직접 물어보게 한다."""
    intent: str  # "scholarship" | "dual_major" | "unclear"


class SlotState(BaseModel):
    student_status: Optional[str] = None
    major: Optional[str] = None
    grade_or_semester: Optional[str] = None
    gpa: Optional[float] = None                  # 4.5 만점 기준
    income_bracket: Optional[str] = None          # "3구간" 또는 "모름"
    special_conditions: Optional[list[str]] = None
    region: Optional[str] = None                  # 거주지역(본인 또는 부모) — 지역장학금 매칭용


# major/grade_or_semester/region도 필수로 물어봄 — 실제 DB(68건) 조사해보니 학과제한(6건)뿐
# 아니라 학년/이수학기 조건(28건, 68%!), 거주지역 조건(15건, 37%! 대전/인천/포항/경주 등
# 지자체 장학재단이 특히 이럼)이 특례조건만큼이나 흔했는데 기존엔 아예 안 물어봐서 매칭
# 정확도에 못 쓰이고 있었음. "대충 답하면 대충 찾아준다"는 문제의 핵심 원인 중 하나.
# 특히 region은 안 물어보면 AI가 "영남대생이니까 아마 대구/경산이겠지" 식으로 근거 없이
# 짐작하게 되는데, 그게 바로 자유서술형 조건 소프트매칭(soft_match_conditions)에서
# 애먼 지역장학금을 잘못 배제하는 원인이 될 수 있어서 명시적으로 확보해둠.
REQUIRED_SLOTS = [
    "student_status",
    "major",
    "grade_or_semester",
    "gpa",
    "income_bracket",
    "special_conditions",
    "region",
]


class SoftMatchItem(BaseModel):
    """규칙만으로 판단 못 하는(자유서술형) 특례조건 하나에 대한 보조판단 결과.
    최종 배제 여부가 아니라 "이 후보를 계속 보여줘도 되는지"에 대한 판단이고,
    애매하면 반드시 eligible=true(배제 안 함)로 두게 프롬프트에서 강제함 —
    장학금 매칭은 최종 확정이 아니라 후보 탐색이라 과소매칭이 과다매칭보다 더 나쁨."""
    scholarship_id: str
    eligible: bool
    reason: str


class SoftMatchBatch(BaseModel):
    results: list[SoftMatchItem] = []


def missing_slots(state: SlotState) -> list[str]:
    return [s for s in REQUIRED_SLOTS if getattr(state, s) in (None, [], "")]


class SlotExtractionResult(BaseModel):
    """슬롯추출 + 다음 질문 생성을 한 번의 LLM 호출로 합치기 위한 응답 스키마
    (실사용자 리포트: "답장 너무 오래 걸림" — 이 둘이 원래 순서대로 호출 2번이라 응답속도가
    거의 2배로 느려지고 있었음). state는 extract_slots()랑 동일한 추출 결과, followup_question은
    모델이 자기가 방금 채운 state 기준으로 REQUIRED_SLOTS 중 여전히 비어있는 게 있으면 그걸
    자연스럽게 물어보는 질문 — 다 채워졌으면 null.
    주의: missing_slots(state) 최종 판단은 항상 여기(Python)가 결정론적으로 다시 하고,
    followup_question은 "그 판단과 맞아떨어질 때만" 참고용으로 씀 — 모델이 뭘 비웠다고
    착각하거나 헷갈려도(드물지만 가능) 실제 빈 슬롯 판정 자체는 절대 흔들리지 않게 함."""
    state: SlotState
    followup_question: Optional[str] = None


# ---------------- 시나리오2: 복수전공 자격요건 판정 ----------------

class DualMajorState(BaseModel):
    """복수전공 자격요건 판정을 위해 필요한 슬롯.
    판정 자체(eligible 여부)는 LLM이 아니라 rules.py의 결정론적 규칙으로 계산함 —
    돈/졸업 여부에 영향 주는 판단이라 AI가 혼자 판정하지 않는다는 설계 원칙."""

    student_status: Optional[str] = None       # 재학 / 복학예정 / 휴학 / 졸업예정 / 제적예정
    completed_semesters: Optional[int] = None    # 지금까지 이수한 학기 수
    earned_credits: Optional[float] = None        # 지금까지 취득한 총 학점
    grad_credit_basis: Optional[int] = None       # 소속 학과 졸업학점 기준 (120/130/140/150/160)
    home_college: Optional[str] = None            # 소속 단과대학 (예외 판정용, 선택)
    target_major: Optional[str] = None            # 목표 복수전공 학과


DUAL_MAJOR_REQUIRED_SLOTS = [
    "student_status",
    "completed_semesters",
    "earned_credits",
    "grad_credit_basis",
    "target_major",
]


def dual_major_missing_slots(state: DualMajorState) -> list[str]:
    return [s for s in DUAL_MAJOR_REQUIRED_SLOTS if getattr(state, s) in (None, "")]


class EligibilityResult(BaseModel):
    eligible: bool
    passed: list[str] = []
    failed: list[str] = []
    notes: list[str] = []


# ---------------- 시나리오2 2단계: 이수과목 인정신청서 자동 초안 ----------------
# (근거: 이수지침 II장 6.차 — "복수전공 선발 이전에 복수전공 교육과정에 편성된
#  교과목을 미리 이수한 경우, 이수과목 인정 신청서 제출 시 인정 가능")
#
# 설계 원칙 동일 적용: "이 과목이 인정 대상인지"는 절대 LLM이 판단하지 않음.
# curriculum.py의 결정론적 매칭(학수번호/과목명 완전일치)만 판단 근거로 쓰고,
# 최종 승인/반려는 반드시 사람(직원)이 함 — 이게 이 시나리오의 진짜 HITL 지점.

class CompletedCourseItem(BaseModel):
    """학생이 '이미 들었다'고 말한 과목 한 건.
    taken_year/taken_semester: 신청서 양식의 '이수학기(연도/학기)' 칸에 실제로 들어가는
    정보 — 학생이 명시적으로 말했을 때만 채우고, 교육과정표의 편성 학년/학기(언제
    배정돼 있는 과목인지)를 추측해서 채우면 안 된다(그건 사람마다 다를 수 있는 실제
    이수 시점과 다른 정보라 지어내면 안 됨). 연도만 알거나 학기만 아는 등 부분적으로만
    말했어도 아는 만큼만 채운다."""
    course_name: str
    course_code: Optional[str] = None
    taken_year: Optional[str] = None       # 예: "2024" — 실제로 이수한 연도
    taken_semester: Optional[str] = None   # 예: "1", "2", "여름학기" — 실제로 이수한 학기


class CompletedCoursesExtraction(BaseModel):
    """자연어 대화에서 추출한, 학생이 언급한 이수과목 목록.
    LLM은 여기까지만 — 이 과목이 인정되는지 여부는 절대 판단하지 않음."""
    courses: list[CompletedCourseItem] = []
    removed_course_names: list[str] = []  # 학생이 이미 말한 과목 중 빼달라고 한 것 — 반드시 이미 추출된(대화에 등장한) 과목명 중에서만, 절대 지어내면 안 됨
    done: bool = False  # 학생이 "더 없어" 등으로 입력을 마쳤다고 볼 수 있으면 true


class CourseSemesterItem(BaseModel):
    """course_semester_check 단계 전용 — 제시한 후보 과목명 중 하나에 대해 학생이 답해준
    실제 이수 연도/학기. course_name은 반드시 제시된 후보 목록의 과목명 그대로여야 한다
    (호출부에서 후보 목록에 실제로 있는지 한 번 더 검증함 — LLM이 과목명을 지어내거나
    살짝 바꿔 쓰는 걸 막기 위한 이중 안전장치)."""
    course_name: str
    taken_year: Optional[str] = None
    taken_semester: Optional[str] = None


class CourseSemesterExtraction(BaseModel):
    items: list[CourseSemesterItem] = []


class MatchedCourse(BaseModel):
    """목표 학과 교육과정표와 대조해 실제로 일치가 확인된 과목 — 결정론적 매칭 결과."""
    course_code: str
    course_name: str
    credit: float
    year_semester: Optional[str] = None  # 교육과정표상 편성 학년/학기(참고용 — 실제 이수 시점 아님)
    matched_note: Optional[str] = None  # 예: "타전공인정 미래자동차공학과"
    taken_year: Optional[str] = None       # 학생이 실제로 이수한 연도(신청서 '연도' 칸)
    taken_semester: Optional[str] = None   # 학생이 실제로 이수한 학기(신청서 '학기' 칸)


class OverlapConfirmation(BaseModel):
    """홈학과·목표학과 교육과정표에 겹치는데 학생이 아직 말 안 한 과목 후보들 중,
    학생이 실제로 이수했다고 확인한 과목명 목록. LLM은 반드시 제시된 후보 목록
    안에서만 골라야 하고, 후보에 없는 과목명을 새로 지어내면 절대 안 된다 — 애매하면
    빈 리스트로 둔다(호출부에서도 후보 목록에 있는지 한 번 더 검증함, 이중 안전장치)."""
    confirmed_course_names: list[str] = []


class StudentIdentity(BaseModel):
    """자연어에서 추출한 학생 본인 정보(성명/학번/학년/소속 단과대학) — 신청서에 실제로
    들어가야 "표만 채워주는" 빈 서류가 아니라 제출 가능한 신청서가 됨(실사용자 피드백:
    "개인정보도 없고 ... 이걸 전산화 한다는데에 의의가 있어야지", "모든 개인정보를
    채워넣을 수 있도록"). LLM은 여기까지만 — 학생이 말한 걸 그대로 파싱만 하고 지어내지
    않는다. college는 공식 양식의 "소속 : ___대학 ___학부(과) ___전공" 중 "대학"
    (단과대학) 칸 — 학과(학부)는 이미 앞 단계에서 확보한 home_college로 채워지므로
    별도로 묻지 않는다."""
    name: Optional[str] = None
    student_id: Optional[str] = None
    grade: Optional[str] = None
    college: Optional[str] = None
    # 실사용자 요청: "반려되는 순간 바로 알림 받고 싶다"는 로그인이 아니라 이메일 같은
    # 컴퓨터/브라우저와 무관한 외부 채널이 있어야 가능함(로그인은 "누군지 확인"만 해줄 뿐,
    # 컴퓨터가 꺼져있으면 어차피 아무것도 못 받음) — 그래서 이름/학번/학년/소속과 함께
    # 이메일도 한 번 더 받아서, 반려/승인되는 그 순간 notify.send_email로 바로 보낸다.
    email: Optional[str] = None


class ApprovalStep(BaseModel):
    """세 번의 승인(학과장/복수전공학과장/행정처) 중 하나의 처리 기록 — 위조된 서명 이미지가
    아니라, 실제로 시스템에서 벌어진 사실(누가 언제 전산으로 승인/반려했는지)만 텍스트로
    남긴다(기존 단일승인 감사기록 설계 원칙과 동일하게 3단계로 확장). 한 번 승인/반려된
    단계는 그 사실 자체를 절대 덮어쓰지 않는다 — 나중에 다른 단계가 반려돼도 이미 승인했던
    단계의 기록은 그대로 남아있어야 진짜 감사기록임."""
    status: str = "pending"  # pending / approved / rejected
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    note: Optional[str] = None


class RecognitionApplication(BaseModel):
    """이수과목 인정신청서 — AI가 초안만 작성하고, 최종 승인/반려는 사람이 함(HITL).

    실사용자 요청으로 승인이 1단계(수업학적팀 단일승인)에서 3단계로 바뀜:
    (1) 학과장, (2) 복수전공학과장 — 이 둘은 순서 상관없이 병렬로 처리 가능, (3) 행정처 —
    반드시 학과장·복수전공학과장 둘 다 승인된 뒤에만 처리 가능(서버가 강제, UI가 아니라
    코드로 순서를 보장 — 이 프로젝트의 "중요한 규칙은 LLM/화면이 아니라 코드가 강제한다"는
    설계 원칙과 같은 이유). 셋 중 어느 단계에서든 반려되면 그 즉시 전체 신청서가 반려로
    확정되고(status="rejected"), 반려 사유는 학생이 다시 수정해서 재신청할 수 있도록
    문서/직원 화면에 그대로 남는다."""
    id: str
    session_id: str
    student_name: Optional[str] = None
    student_id: Optional[str] = None
    student_grade: Optional[str] = None
    student_college: Optional[str] = None  # 소속 단과대학(예: 공과대학) — 양식의 "대학" 칸
    student_email: Optional[str] = None  # 반려/승인 즉시 알림 발송용(notify.send_email) — 양식엔 안 들어감
    home_major: Optional[str] = None
    target_major: str
    matched_courses: list[MatchedCourse] = []
    unmatched_course_names: list[str] = []
    status: str = "pending"  # pending / approved(3단계 전부 승인 = 전산 반영) / rejected(어느 단계든 하나라도 반려)
    created_at: str
    home_chair_approval: ApprovalStep = ApprovalStep()  # 학과장(소속 학과)
    dual_chair_approval: ApprovalStep = ApprovalStep()  # 복수전공(목표 학과) 학과장
    admin_approval: ApprovalStep = ApprovalStep()  # 행정처(사무처) 최종승인 — 위 둘 다 승인 후에만
    # --- 레거시(구버전 1단계 승인) 데이터 호환용 — 새 코드는 더 이상 이 3개를 쓰지 않고
    # 위 3단계 필드만 씀. 예전에 저장된 신청서를 읽을 때 깨지지 않게, 그리고 그 예전 기록의
    # 문서 감사기록을 그대로 보여주기 위해서만 남겨둠(recognition_doc.py 참고).
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    decision_note: Optional[str] = None


INCIDENT_CATEGORIES = ["보안", "시설", "기타"]


class IncidentReport(BaseModel):
    """캠퍼스 안전/시설 신고 — 실사용자 요청: "정문에 신천지 돌아다녀요 이렇게 레포트하면
    즉각적으로 보안팀이나 다른 행정팀에 레포트가 간다던가". 처음엔 완전히 독립된 신고 폼
    (`/report`)으로만 만들었는데, 사용자가 다시 "각 잡고 폼을 만들면 오히려 신고율이
    떨어지지 않냐, 카톡하듯이 편하게 던지는 게 낫지 않냐"고 재지적 → "안전신고 탭을 안
    들어가고 메인 챗봇에서 신고할 수 있도록" 요청함. 그래서 `/report` 폼은 그대로 남겨두고
    (원하면 차분히 채워도 됨), 학생용 메인 챗봇 대화 중 아무 때나(진행 중인 장학금/복수전공
    상담을 방해하지 않고) "야 정문에 신천지 있음ㅡㅡ"처럼 툭 던지면 그 자리에서 바로
    접수되는 경로를 추가함(app.py의 `_looks_like_incident`/`_try_handle_incident_report`
    참고) — 급한 신고인데 몇 단계를 거쳐야 하면 오히려 방해가 된다는 원칙은 동일.

    신고자 정보(이름/연락처)는 선택 입력 — 완전 익명도 아니고 필수 입력도 아님(실사용자가
    "선택 입력"을 선택). 신원 확인보다 "일단 신고 자체가 쉽게 되는 것"이 우선이라는 판단.

    category는 자유 텍스트가 아니라 반드시 INCIDENT_CATEGORIES(보안/시설/기타) 중 하나 —
    직원 대시보드가 이 값 그대로 탭으로 나눠 보여주기 때문에 서버에서 강제 검증함."""
    id: str
    category: str  # "보안" | "시설" | "기타" — INCIDENT_CATEGORIES 참고
    description: str
    location: Optional[str] = None
    people_count: Optional[str] = None  # 관련 인원 수 — 자유텍스트("3명 정도", "혼자" 등), 선택 입력
    reporter_name: Optional[str] = None
    reporter_contact: Optional[str] = None
    status: str = "open"  # open(접수) / resolved(처리완료)
    created_at: str
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None
    resolved_note: Optional[str] = None


class IncidentUpdateIn(BaseModel):
    """직원 대시보드(incidents.html)에서 접수된 신고에 나중에 알게 된 정보를 채워 넣을 때
    쓰는 입력 스키마 — 실사용자 피드백: "너무 세세하게 물어보면 긴급/응급때 귀찮을 수
    있으니 일단 접수, 사람이 이야기해주면 직원용 페이지에서 업데이트되는 거로". 챗봇은
    위치를 학생이 스스로 말했을 때만 담고 나머지(인원수/이름/연락처)는 절대 안 캐물으므로,
    현장에서 직접 듣고 확인한 직원이 이 필드들을 채운다. 넘어온 필드만 덮어쓰고(부분수정),
    필드를 넘기지 않으면(None) 기존 값을 그대로 둔다 — 빈 문자열("")을 명시적으로 보내면
    그 필드를 지운다."""
    location: Optional[str] = None
    people_count: Optional[str] = None
    reporter_name: Optional[str] = None
    reporter_contact: Optional[str] = None


class IncidentReportExtraction(BaseModel):
    """메인 챗봇 대화 중 학생이 툭 던진 메시지(예: "야 정문에 신천지 있음ㅡㅡ")가 실제로
    캠퍼스 안전/시설 신고인지, 아니면 그냥 잡담/다른 상담 중 나온 무관한 말인지 LLM이
    최종 확인하는 스키마. app.py의 키워드 사전필터(INCIDENT_TRIGGER_KEYWORDS)는 어디까지나
    "이 정도면 LLM한테 한 번 물어볼 가치가 있다"는 값싼 1차 필터일 뿐이라 오탐(false
    positive)이 섞일 수 있음 — 예: "나 오늘 롤하다가 신고당함ㅋㅋ"에도 "신고"가 들어있지만
    캠퍼스 신고가 아님. is_incident_report가 최종 판단이고, false면 나머지 필드는 안 써도
    되며 호출부는 이 메시지를 원래 하던 대로(장학금/복수전공 상담 등) 계속 처리한다.

    접수는 항상 즉시·최소정보로 끝난다(실사용자 피드백: "너무 세세하게 물어보면 긴급/
    응급때 귀찮을 수 있으니 일단 접수 — 사람이 이야기해주면 직원용 페이지에서
    업데이트되는 거로" — 접수 전에 위치/인원수/이름/연락처를 캐묻는 후속 질문 단계를
    한때 넣었다가 이 피드백으로 되돌림). location/people_count는 학생이 처음 메시지에서
    이미 자발적으로 말했을 때만 뽑아서 담고, 안 물어본 나머지 항목(이름/연락처 등)은
    직원이 직접 응대하면서 알아낸 뒤 대시보드에서 채워 넣는다(app.py의
    POST /api/incidents/{id}/update 참고)."""
    is_incident_report: bool
    category: str = "기타"  # "보안" | "시설" | "기타" — INCIDENT_CATEGORIES 참고
    description: str = ""  # 신고 내용 요약(존댓말, 담당팀이 보는 공식 신고 내용)
    location: Optional[str] = None  # 학생이 장소를 명시했을 때만
    people_count: Optional[str] = None  # 학생이 인원수를 이미 언급했을 때만(자유텍스트)
