"""
대화 로직 본체 — LLM 호출 + 슬롯추출 + 서류 인정신청서 자동추출
CLI/웹이 공통으로 사용. API 키/모델은 config.py에서 매번 새로 읽어와서
관리자 페이지에서 키를 바꾸면 재시작 없이 바로 반영됨.
"""
import json
import time

from google import genai

import config
from schemas import (
    CompletedCoursesExtraction,
    CourseSemesterExtraction,
    DualMajorState,
    Eligibility,
    EligibilityResult,
    GroundingCheck,
    IncidentFollowupExtraction,
    IncidentReportExtraction,
    IntentClassification,
    MatchedCourse,
    OverlapConfirmation,
    RecognitionApplication,
    REQUIRED_SLOTS,
    Scholarship,
    SlotExtractionResult,
    SlotState,
    SoftMatchBatch,
    StudentIdentity,
)

# 실사용자 리포트: 화면(static/index.html)이 챗봇 답변을 순수 텍스트로만 표시함
# (`bubble.textContent = text` — 마크다운을 렌더링하는 뷰어가 아님). 그런데 LLM이 답변에
# **볼드**, ### 제목, 번호 매기기, 구분선(---), 이모지를 잔뜩 섞어 쓰면 그게 그대로
# 화면에 별표/샵 기호로 노출돼서 가독성이 심하게 떨어짐. 거기에 더해 "반말로 답해라"라고만
# 시켜놨더니 대화 도중 존댓말이 슬쩍 섞여 나오는 경우도 있었음(실사용자 리포트: "존대 반말
# 계속 바뀌는데 통일해줘"). 그래서 사용자에게 직접 보여지는 텍스트를 만드는 모든
# 시스템 프롬프트 끝에 이 규칙을 공통으로 붙여서 형식/말투를 강제로 통일함.
_OUTPUT_STYLE_RULES = """

[출력 형식 규칙 — 반드시 지켜라, 예외 없음]
- 마크다운 문법을 절대 쓰지 마라: **볼드**, # 제목, - 리스트, 1. 2. 3. 번호 매기기, 표,
  구분선(---) 전부 금지. 이 화면은 마크다운을 그림으로 안 바꿔주는 순수 텍스트 채팅창이라
  별표/샵 기호가 그대로 지저분한 글자로 보인다 — 강조하고 싶으면 "진짜", "완전" 같은 말이나
  느낌표로 표현해라.
- 항목이 여러 개면 번호/기호 없이 줄바꿈과 자연스러운 문장으로 풀어서 이어 써라(예:
  "1. 서류 준비 2. 접수" 대신 "먼저 서류부터 챙기고, 그다음에 접수하면 돼").
- 이모지는 메시지당 최대 1개, 꼭 필요할 때만 아주 가끔 써라.
- 문체는 첫 글자부터 끝 글자까지 100% 반말로만 써라. "~요"/"~습니다"/"~세요"/"~해요"처럼
  존댓말로 끝나는 문장은 절대 한 문장도 섞지 마라 — 카카오톡으로 친한 친구한테 편하게
  톡 보내는 말투를 대화 끝까지 흔들림 없이 유지해라.
"""


CHAT_SYSTEM_PROMPT = """너는 대학 장학금 안내 AI다. 친근하고 간결한 말투로 학생을 돕는다.
장학금은 조건 하나하나가 실제 당락(그리고 돈)에 영향을 주니까, 대충 물어보고 대충 찾아주면 안 된다 —
꼼꼼하고 살짝 집요하게 물어봐야 정확한 후보를 찾아줄 수 있다.
- 정보가 부족하면 적극적으로 질문한다 (한 번에 너무 많이 묻지 말 것, 2개까지는 묶어서 물어봐도 됨).
- 학생이 "그냥 그래요", "좋은 편이에요", "잘 몰라요"처럼 두루뭉술하게 답하면 그냥 넘어가지 말고,
  구체적인 숫자/표현으로 다시 한번 명확하게 되물어라. 예시를 들어서 어떤 형식으로 답해야 하는지
  보여주면 좋다 (예: "학점 좋아요" → "오 정확히 몇 점대야? 예를 들면 4.5 만점에 3.8 이런 식으로 알려줘!").
- 절대로 확인되지 않은 장학금 조건을 지어내지 않는다.
- 사용자가 반말/줄임말을 써도 편하게 맞춰서 대답한다.
- 너는 사무적으로 정보만 캐묻는 봇이 아니라, 실제 학교 상담창구의 다정한 상담사처럼 느껴져야 한다.
  학생이 경제적 어려움이나 가정사처럼 힘든 얘기를 살짝 비치면, 바로 다음 질문으로 넘어가지 말고
  "그랬구나, 힘들었겠다" 정도로 짧게 먼저 공감한 다음 자연스럽게 이어가라(장황한 위로는 금지).
  학생이 안부를 묻거나 잡담을 걸면 상담 목적에만 매몰되지 말고 사람처럼 짧고 편하게 받아준 뒤,
  부드럽게 다시 필요한 이야기로 돌아와라. 공감/잡담은 짧게, 본론을 아예 놓치지는 마라.
""" + _OUTPUT_STYLE_RULES

ACTIONABLE_PROMPT = """너는 대학 행정 안내 AI다. 학생이 선택한 장학금에 대해
지금 당장 해야 할 첫 액션부터 짚어주는 실행 가이드를 준다.
- 서류 목록 중 가장 먼저 준비해야 할 것부터 안내한다 (발급처, 방법 포함).
- 말투는 "~부터 뽑으러 갑시다!"처럼 행동을 재촉하는 친근한 톤을 쓴다.
- 근거 없는 절차는 지어내지 않는다. 제공된 정보 안에서만 안내한다.
""" + _OUTPUT_STYLE_RULES

CONSULT_PROMPT = """너는 대학 장학금 상담 AI다. 학생이 이미 특정 장학금을 선택하고
서류를 준비하는 단계다. 이어지는 자유 질문에 친근하게 답한다.
- 제공된 장학금 정보 범위 밖의 사실은 지어내지 말고, 모르면 "정확한 건 장학팀에 확인해보라"고 안내한다.
- 반말/줄임말 편하게 맞춰준다.
- 실제 상담사처럼 대해라 — 서류 얘기만 기계적으로 반복하지 말고, 학생이 힘들어하거나 걱정되는
  티를 내면 짧게 공감해주고, 잡담이나 안부를 물으면 자연스럽게 받아준 뒤 다시 본론으로 돌아와라.
""" + _OUTPUT_STYLE_RULES


def get_client() -> genai.Client:
    api_key = config.get_api_key()
    if not api_key:
        raise RuntimeError(
            "Gemini API 키가 설정되지 않았어요. /admin 페이지에서 먼저 등록해주세요."
        )
    return genai.Client(api_key=api_key)


# 실사용자 리포트: "보내고 답장 오기까지 너무 오래 걸림". Gemini 모델들은 답 내놓기 전에
# 화면에 안 보이는 "생각하는 토큰"(thinking)을 기본으로 만드는데, 여기서 하는 작업(슬롯
# 추출, 짧은 안내문구 생성)은 복잡한 추론이 필요 없는 단순작업이라 켜둘 이유가 없음.
#
# 근데 thinking을 끄는 API 파라미터가 모델 세대별로 다름(2026-09 기준):
# - Gemini 2.5 계열: thinking_budget(토큰 수)으로 조절, 0을 주면 완전히 꺼짐.
# - Gemini 3 계열(3.5-flash 포함): thinking_level(low/medium/high, 대부분 minimal 미지원)
#   으로 조절하는 새 방식으로 바뀌었고, 공식 문서에 "Gemini 3 Flash/Flash-Lite는 thinking을
#   완전히 끄는 걸 지원하지 않음"이라고 명시돼 있음 — thinking_budget=0을 줘도 안 먹히거나
#   무시될 수 있음. 두 파라미터를 동시에 넣으면 400 에러가 나서 같이 쓸 수도 없음.
# 그래서 모델명 보고 세대를 구분해서 맞는 파라미터만 골라 씀 — config.get_model()이 바뀌면
# (관리자 페이지에서 모델명 변경) 자동으로 맞는 쪽으로 전환됨.
def _thinking_config_for(model: str) -> dict:
    model = (model or "").lower()
    if model.startswith("gemini-3") or "gemini-3" in model:
        # Gemini 3 계열은 완전 OFF가 안 되니, 그나마 제일 빠른 low로 맞춤
        return {"thinking_level": "low"}
    return {"thinking_budget": 0}


def _generate_json(prompt: str, schema, temperature: float = 0.0, system_instruction: str | None = None):
    client = get_client()
    model = config.get_model()
    t0 = time.perf_counter()
    gen_config = {
        "response_mime_type": "application/json",
        "response_schema": schema,
        "temperature": temperature,
        "thinking_config": _thinking_config_for(model),
    }
    # JSON 모드(구조화 추출)여도 system_instruction은 같이 줄 수 있음 — 호출 합치기
    # (extract_slots_with_followup)에서 "추출"과 "다정한 말투로 다음 질문 생성"을 한 번에
    # 하려면 CHAT_SYSTEM_PROMPT의 성격/말투 규칙이 같이 필요해서 옵션으로 추가함.
    if system_instruction:
        gen_config["system_instruction"] = system_instruction
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=gen_config,
    )
    # 응답이 느리다는 리포트가 있어서, 실제로 Gemini 호출 자체가 얼마나 걸리는지 터미널에
    # 바로 보이게 함(한 턴에 이 호출이 여러 번 겹쳐서 체감 속도가 더 느려지는 구조라, 콜드
    # 스타트/네트워크/모델 문제 중 뭐가 진짜 원인인지 이 숫자로 구분할 수 있음).
    print(f"[Gemini] {model} JSON 호출 {time.perf_counter() - t0:.2f}s")
    return response.text


def _generate_text(prompt: str, system_instruction: str, temperature: float = 0.4) -> str:
    client = get_client()
    model = config.get_model()
    t0 = time.perf_counter()
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={
            "system_instruction": system_instruction,
            "temperature": temperature,
            "thinking_config": _thinking_config_for(model),
        },
    )
    print(f"[Gemini] {model} 텍스트 호출 {time.perf_counter() - t0:.2f}s")
    return response.text


# ---------- 0. 통합 챗봇 진입 라우팅(의도 분류) ----------
# 장학금 vs 복수전공 중 뭘 물어보는 건지 애매할 때만 호출됨(키워드로 먼저 걸러지고
# 남은 경우만). 이것도 "최종 판단"이 아니라 상담 종류 라우팅일 뿐이고, 애매하면
# unclear로 던져서 사람한테 직접 물어보게 만듦 — 오분류 리스크를 최대한 줄이는 설계.

INTENT_SYSTEM_PROMPT = """너는 대학 행정봇의 라우터다. 학생의 메시지 하나만 보고
아래 넷 중 하나로 분류한다:
- "scholarship": 장학금, 학자금지원구간, 근로장학, 등록금지원, 장학사정 등 장학 관련
- "dual_major": 복수전공, 부전공, 다전공, 이수과목 인정, 전공 이수학점 관련
- "incident_report": 캠퍼스에서 목격한 안전/시설 문제를 알리는 경우(예: 수상한 사람,
  포교/신천지 같은 종교단체 캠퍼스 내 활동, 사고, 시설 고장·파손·누수 등) — 학생이 딱히
  "신고"라는 단어를 안 써도, 캠퍼스에서 있었던 문제 상황을 알리려는 의도가 분명하면 이걸로
- "unclear": 위 셋 다 아니거나, 인사말/잡담처럼 애매해서 판단 못하는 경우
확신 없으면 반드시 unclear로 답해라 — 지어내서 단정하지 마라."""


def classify_intent(message: str) -> str:
    # 이 함수가 원래 INTENT_SYSTEM_PROMPT(분류 기준 설명)를 정의만 해두고 실제 호출에는
    # 넘기지 않던 버그가 있었음 — 그동안은 response_schema(IntentClassification)만으로
    # 모델이 "intent: str" 필드가 있다는 것만 알고 무슨 기준으로 채워야 하는지는 전혀
    # 모르는 채로 호출되고 있었다는 뜻. incident_report 분류를 새로 추가하면서 이 누락을
    # 발견해서 같이 고침(system_instruction을 실제로 전달).
    prompt = f"학생 메시지: {message}"
    text = _generate_json(prompt, IntentClassification, temperature=0.0, system_instruction=INTENT_SYSTEM_PROMPT)
    result = IntentClassification.model_validate_json(text)
    if result.intent not in ("scholarship", "dual_major", "incident_report"):
        return "unclear"
    return result.intent


# ---------- 0-1. 캠퍼스 안전/시설 신고 감지 + 추출 ----------
# 실사용자 요청: "안전신고 탭을 안 들어가고 메인 챗봇에서 신고할 수 있도록" — 장학금/복수전공
# 상담 중이든 아니든, 학생이 카톡하듯 툭 던진 메시지가 캠퍼스 신고인지 판단하고 맞으면
# 카테고리/내용/장소까지 한 번에 뽑아낸다. app.py에서 값싼 키워드 사전필터를 통과한
# 메시지에 대해서만 호출됨(모든 메시지마다 호출하면 비용·응답속도가 늘어나서).

INCIDENT_SYSTEM_PROMPT = """너는 대학 캠퍼스 안전/시설 신고를 접수하는 라우터다. 학생 메시지
하나를 보고, 이게 진짜로 캠퍼스에서 있었던 안전/시설 문제를 알리는 신고인지 최종 판단한다.
- 진짜 신고면 is_incident_report=true로 하고, category를 아래 중 가장 알맞은 걸로 고른다:
  "보안"(수상한 사람, 포교/신천지 등 종교단체 활동, 절도, 폭행, 성희롱 등 치안 문제),
  "시설"(고장, 파손, 누수, 정전 등 시설물 문제), "기타"(위 둘에 안 맞는 나머지 문제).
- description에는 학생이 말한 상황을 한두 문장으로 간결하게 정리해서 담아라 — 존댓말로 써라
  (담당 직원이 보는 공식 신고 내용이다). 학생이 말한 내용만 담고 지어내지 마라.
- location은 학생이 장소를 명시적으로 언급했을 때만 채우고, 언급 안 했으면 null로 둬라.
- people_count는 학생이 관련 인원수를 이미 언급했을 때만("셋이서", "저 혼자", "몇 명 모여있음"
  등) 자유텍스트로 채우고, 언급 안 했으면 null로 둬라 — 지어내지 마라.
- 신고와 무관한 잡담이나 다른 맥락(예: "나 오늘 게임하다 신고당함ㅋㅋ", "저기 계단 몇 개야"
  같은 단순 질문)이면 is_incident_report=false로 하고 나머지 필드는 채우지 마라(기본값 사용).
확신 없으면 false로 답해라 — 애매한 잡담을 신고로 잘못 접수시키면 안 된다."""


def extract_incident_report(message: str) -> IncidentReportExtraction:
    prompt = f"학생 메시지: {message}"
    text = _generate_json(
        prompt, IncidentReportExtraction, temperature=0.0, system_instruction=INCIDENT_SYSTEM_PROMPT
    )
    return IncidentReportExtraction.model_validate_json(text)


# ---------- 0-2. 캠퍼스 안전/시설 신고 후속 질문 답변 해석 ----------
# 실사용자 요청: "112신고하면 어디에 칼부림났어요하면 계속 물어보잖아 그런걸 모델로해서
# 뭔가 계속 물어보면 좋겠어. 대신 언제든 신고를 끝낼 수는 잇도록 안내하고, 개인정보
# 익명처리하고 싶다하면 그렇게 할 수 있도록." — 112 지령실이 위치·인원 등을 계속
# 확인하는 것처럼, 1차 감지(extract_incident_report) 직후 바로 접수하지 않고 app.py가
# sess["pending_incident"]로 넘어가 위치/인원수/이름·연락처를 몇 차례 더 캐묻는다
# (app.py의 _incident_next_action이 단계/횟수를 관리). 이 함수는 그 후속 대화에서
# 학생이 남긴 답변 한 개를 해석 — 상황 정보뿐 아니라 "이제 그만하고 싶어하는지"
# "익명으로 하고 싶어하는지"까지 같이 판단해서 app.py가 언제든 안전하게 빠져나올 수
# 있게 한다. (한때 이 단계 자체를 없애고 즉시 접수만 했었는데, "긴급할 때 캐물으면
# 귀찮다"는 이전 피드백과 정반대인 이번 피드백을 따라 되돌림 — 대신 "그만"이라고
# 하면 언제든 즉시 끝낼 수 있게 만들어서 두 피드백의 절충점을 맞춤.)

INCIDENT_FOLLOWUP_SYSTEM_PROMPT = """너는 대학 캠퍼스 안전/시설 신고를 접수하면서
112 지령실처럼 상황을 좀 더 확인하는 중이다. 학생이 방금 남긴 답변 메시지를 보고
아래를 판단한다:
- wants_to_finish: 학생이 "그만", "됐어", "그냥 접수해줘", "빨리 접수나 해줘"처럼 더 이상
  질문에 답하지 않고 지금까지 내용으로 바로 접수를 끝내고 싶어하면 true, 아니면 false.
- wants_anonymous: 학생이 "이름/연락처 말하기 싫어", "익명으로 할래", "그냥 익명으로 해줘"
  처럼 신원을 밝히고 싶지 않다는 의사를 명시했으면 true, 아니면 false.
- location: 장소를 새로 언급했으면 채우고, 없으면 null.
- people_count: 인원수를 새로 언급했으면 채우고, 없으면 null.
- extra_detail: 상황 파악에 도움되는 추가 정보(예: "흉기를 들고 있었다", "다친 사람은
  없다")가 있으면 존댓말 한두 문장으로 정리, 없으면 null.
- reporter_name / reporter_contact: 이름/연락처를 이번 메시지에서 말했으면 채우고,
  없으면 null.
전부 선택 입력이니 학생이 "몰라"/무응답/스킵이면 해당 필드를 null로 남겨라 — 지어내지
마라."""


def extract_incident_followup(message: str) -> IncidentFollowupExtraction:
    prompt = f"학생 답변: {message}"
    text = _generate_json(
        prompt, IncidentFollowupExtraction, temperature=0.0, system_instruction=INCIDENT_FOLLOWUP_SYSTEM_PROMPT
    )
    return IncidentFollowupExtraction.model_validate_json(text)


INTENT_CLARIFY_PROMPT = """너는 대학 학생상담 AI Uni-VOC다. 지금은 학생이 장학금 상담을 원하는지
복수전공/이수과목 인정 상담을 원하는지 아직 확인이 안 된 상태다.
- 절대로 고정 문구를 기계처럼 반복하지 마라 — 학생의 방금 메시지를 실제로 읽고 자연스럽게
  반응부터 해라. 인사("안녕" 등)면 인사로 받아주고, 잡담이면 짧게 자연스럽게 받아줘라. 학생이
  "왜 그 말만 반복하냐"처럼 답답해하면 그 지적을 인정하고 사과 한마디 정도 자연스럽게 곁들여라.
- 그런 다음 자연스럽게(매번 같은 문장 말고 표현을 조금씩 바꿔서) 장학금이 궁금한지 복수전공/
  이수과목 인정이 궁금한지 물어봐라.
- 사용자가 반말/줄임말을 써도 편하게 맞춰서 대답한다.
""" + _OUTPUT_STYLE_RULES


def generate_intent_clarify_reply(conversation_history: str) -> str:
    prompt = f"""[대화 이력 — 마지막 학생 메시지에 실제로 반응한 다음 자연스럽게 이어가라]
{conversation_history}
"""
    return _generate_text(prompt, INTENT_CLARIFY_PROMPT, temperature=0.6)


# ---------- 1. 슬롯필링 ----------

def extract_slots(conversation_history: str, current_state: SlotState) -> SlotState:
    prompt = f"""아래는 학생과 장학금 안내 챗봇의 대화 이력이다.
대화에서 "구체적이고 명확하게" 언급된 정보만 추출하라. 애매하거나 두루뭉술한 답변에서
값을 추측/어림잡아 채우면 절대 안 된다 — 그런 경우엔 null로 남겨서 다시 물어보게 해야 한다.
- gpa: 학생이 실제 숫자를 말했을 때만 채워라 (예: "3.8", "4.0 만점에 3.5"). "좋아요"/"보통이에요"
  같은 말만으로는 절대 숫자를 지어내지 마라 — null로 둬라.
- income_bracket: "3구간"처럼 정확한 구간 번호를 말했거나, 학생이 "모른다"고 명시했을 때만 채워라
  (모른다고 명시한 경우엔 "모름"으로 채워서 다음 질문으로 안 넘어가게 해라). 애매하면 null.
- special_conditions: 학생이 실제로 언급한 특례조건들을 리스트로 담거나, "없다"고 명시했으면
  ["없음"]으로 채워라. 아무 말도 안 했으면 null로 둬라(있는지 없는지 자체를 추측하지 마라).
  주의: 학생이 특례조건 하나만 말했다고 해서(예: "다자녀야") 다른 조건은 해당 없다는 뜻이
  아니다 — 말한 것만 그대로 담아라(예: ["다자녀"]), 언급 안 된 다른 카테고리를 "없음"으로
  단정하지 마라.
- region: 본인(또는 부모) 거주지역을 실제로 말했을 때만 채워라(예: "대구", "경산"). 지역
  장학금이 은근히 많아서 물어보는 거니, 안 물어봤으면 null로 둬라.
- student_status/major/grade_or_semester도 마찬가지로 실제로 말한 내용만, 추측 금지.
이미 알고 있는 정보는 그대로 유지하고, 이번 대화에서 새로 "명확하게" 언급된 정보만 채워라.

[현재까지 알고 있는 정보]
{current_state.model_dump_json()}

[대화 이력]
{conversation_history}
"""
    text = _generate_json(prompt, SlotState)
    return SlotState.model_validate_json(text)


# extract_slots() + generate_followup_question()를 순서대로 호출 2번 하던 걸 하나로 합침
# (실사용자 리포트: "답장 너무 오래 걸림" — 이 둘이 한 턴에 순서대로 나가느라 응답이
# 거의 2배로 느려지고 있었음). 추출 규칙은 extract_slots()랑 완전히 동일하게 유지하고,
# 거기에 "그 추출 결과 기준으로 아직 빈 게 있으면 다음 질문도 같이 만들어라"만 추가함.
# CHAT_SYSTEM_PROMPT을 system_instruction으로 같이 줘서 질문 말투/성격은 그대로 유지되게 함.
#
# 안전장치: 여기서 나온 followup_question은 참고용일 뿐, 실제로 뭘 물어봐야 하는지는
# app.py에서 항상 missing_slots(state)로 다시 결정론적으로 계산해서 검증함. LLM이 자기가
# 채운 슬롯을 헷갈려서 이미 채워진 걸 또 묻거나 빈 필드를 안 묻는 드문 경우엔, 호출을
# 한 번 더 해서(generate_followup_question) 예전 방식으로 자동 폴백함 — 최악의 경우에도
# 지금보다 느려지지 않고, 맞아떨어지는 대부분의 경우엔 호출 1번으로 끝나서 빨라짐.
def extract_slots_with_followup(
    conversation_history: str, current_state: SlotState
) -> tuple[SlotState, str | None]:
    prompt = f"""아래는 학생과 장학금 안내 챗봇의 대화 이력이다.
[1단계: 정보 추출]
대화에서 "구체적이고 명확하게" 언급된 정보만 추출해서 state에 채워라. 애매하거나 두루뭉술한
답변에서 값을 추측/어림잡아 채우면 절대 안 된다 — 그런 경우엔 null로 남겨서 다시 물어보게
해야 한다.
- gpa: 학생이 실제 숫자를 말했을 때만 채워라 (예: "3.8", "4.0 만점에 3.5"). "좋아요"/"보통이에요"
  같은 말만으로는 절대 숫자를 지어내지 마라 — null로 둬라.
- income_bracket: "3구간"처럼 정확한 구간 번호를 말했거나, 학생이 "모른다"고 명시했을 때만 채워라
  (모른다고 명시한 경우엔 "모름"으로 채워서 다음 질문으로 안 넘어가게 해라). 애매하면 null.
- special_conditions: 학생이 실제로 언급한 특례조건들을 리스트로 담거나, "없다"고 명시했으면
  ["없음"]으로 채워라. 아무 말도 안 했으면 null로 둬라(있는지 없는지 자체를 추측하지 마라).
  주의: 학생이 특례조건 하나만 말했다고 해서(예: "다자녀야") 다른 조건은 해당 없다는 뜻이
  아니다 — 말한 것만 그대로 담아라(예: ["다자녀"]), 언급 안 된 다른 카테고리를 "없음"으로
  단정하지 마라.
- region: 본인(또는 부모) 거주지역을 실제로 말했을 때만 채워라(예: "대구", "경산"). 지역
  장학금이 은근히 많아서 물어보는 거니, 안 물어봤으면 null로 둬라.
- student_status/major/grade_or_semester도 마찬가지로 실제로 말한 내용만, 추측 금지.
이미 알고 있는 정보는 그대로 유지하고, 이번 대화에서 새로 "명확하게" 언급된 정보만 채워라.

[2단계: 다음 질문 생성]
위에서 채운 state를 기준으로, 아래 필수 항목({REQUIRED_SLOTS}) 중 여전히 null/빈 값으로
남아있는 게 있으면, 그중 자연스럽게 이어서 물어볼 다음 질문 하나를 만들어서
followup_question에 담아라(이미 채워진 항목은 절대 다시 묻지 마라). 바로 직전 학생 답변이
애매/두루뭉술했다면(예: "좋아요", "그냥 그래요", "잘 몰라요") 그 항목을 그냥 넘어가지 말고
구체적인 형식 예시를 들어서 다시 한번 명확하게 되물어라. 필수 항목이 전부 채워졌으면
followup_question은 null로 둬라.

[현재까지 알고 있는 정보]
{current_state.model_dump_json()}

[대화 이력]
{conversation_history}
"""
    text = _generate_json(prompt, SlotExtractionResult, temperature=0.3, system_instruction=CHAT_SYSTEM_PROMPT)
    result = SlotExtractionResult.model_validate_json(text)
    return result.state, result.followup_question


def generate_followup_question(conversation_history: str, missing: list[str]) -> str:
    prompt = f"""아래 대화 이력을 보고, 아직 모르는 정보인 {missing}에 대해 자연스럽게 이어서 질문하라.
이미 아는 정보는 다시 묻지 마라.
바로 직전 학생 답변이 애매/두루뭉술했다면(예: "좋아요", "그냥 그래요", "잘 몰라요") 그 항목을
그냥 넘어가지 말고 구체적인 형식 예시를 들어서 다시 한번 명확하게 되물어라.

[대화 이력]
{conversation_history}
"""
    return _generate_text(prompt, CHAT_SYSTEM_PROMPT, temperature=0.5)


# ---------- 2. 후보 제시 ----------

def generate_candidate_presentation(matches: list[tuple[Scholarship, bool]]) -> str:
    def _caveats(s: Scholarship) -> str:
        e = s.eligibility
        parts = []
        if e.grade_or_semester_note:
            parts.append(f"학년/학기조건: {e.grade_or_semester_note}")
        if e.major_restriction:
            parts.append(f"학과제한: {e.major_restriction}")
        if e.special_conditions:
            parts.append(f"특례조건: {', '.join(e.special_conditions)}")
        return " / ".join(parts)

    listing = "\n".join(
        f"{i + 1}. {s.name} — {s.eligibility.other_conditions or '조건 충족'}"
        + (f" [참고: {_caveats(s)}]" if _caveats(s) else "")
        + (" (※소득분위 확인 필요)" if needs_check else "")
        for i, (s, needs_check) in enumerate(matches)
    )
    prompt = f"""학생에게 아래 매칭된 장학금 후보를 번호 붙여서 간단히 제시하고,
어떤 걸로 진행할지 물어봐라. 각 항목은 왜 이 학생이 해당되는지 한 줄로 짧게 붙여라.
"[참고: ...]"에 학년/학과/특례조건이 있으면, 이건 학생 본인이 실제로 해당하는지 스스로
한번 더 확인해봐야 하는 조건이라는 뉘앙스로 자연스럽게 짧게 언급해라(너무 길게 나열하지 말고
핵심만). "※소득분위 확인 필요"가 붙은 건 학자금지원구간을 몰라서 확정은 아니라는 점을 언급해라.

[매칭된 장학금]
{listing}
"""
    return _generate_text(prompt, CHAT_SYSTEM_PROMPT, temperature=0.4)


# ---------- 2-1. 후보 목록 제시 후 — 번호 없이 다른 말을 했을 때 ----------
#
# 예전엔 "matched" 단계에서 사용자 메시지에 숫자가 없으면 무조건 "번호로 골라줘"라는
# 고정 문구만 반복했음. 그러면 학생이 "이거 나 해당 안 되는데?"(예: 후보 장학금이 특정
# 지역 거주자만 되는데 자기는 다른 지역이라고 말한 경우)처럼 실제로 의미 있는 말을 해도
# 완전히 무시하고 같은 문장을 계속 뱉는 무한루프가 생겼음 — 실사용자 리포트로 확인된 버그.
# 이제는 학생이 뭐라고 했는지 실제로 반영해서 자연스럽게 반응하게 함.

def generate_matched_stage_reply(
    conversation_history: str, matches: list[tuple[Scholarship, bool]]
) -> str:
    listing = "\n".join(f"{i + 1}. {s.name}" for i, (s, _) in enumerate(matches))
    prompt = f"""아래는 학생한테 이미 제시한 장학금 후보 목록이다. 방금 학생이 번호로 고르지 않고
다른 말을 했다 — 후보 중 하나가 자기 상황과 안 맞는다고 반박했거나(예: "이거 나 해당 안 되는데",
거주지역이 다르다 등), 다른 질문을 했을 수 있다. 학생 말을 절대 무시하지 말고 그 내용에
자연스럽게 반응한 다음(안 맞는다고 한 후보가 있으면 그건 공감하고 다시 권하지 마라), 나머지
후보 중에서 번호로 고를 수 있게 다시 안내해라. 목록에 없는 새 정보를 지어내지 마라. 모든
후보가 다 안 맞는다고 하면, '처음부터'라고 입력하면 조건을 다시 입력할 수 있다고 안내해라.

[제시된 후보 목록]
{listing}

[대화 이력 — 마지막 학생 메시지에 집중해서 답해라]
{conversation_history}
"""
    return _generate_text(prompt, CHAT_SYSTEM_PROMPT, temperature=0.4)


# ---------- 2-2. 근거 검증(환각 필터) ----------
#
# 지금까지는 "근거 데이터 안에서만 답해라"를 시스템 프롬프트로만 시켰는데, 이건 LLM한테
# 말로 부탁하는 거라 100% 지켜진다는 보장이 없음 — 특히 사실 정보(금액/절차/서류)가 실제
# 학생 행동(서류 준비, 제출)으로 이어지는 액션 가이드·상담 답변에서 한 번 더 검증 없이
# 그대로 내보내는 건 위험함. 그래서 답변을 생성한 뒤, 그 답변이 실제로 건네준 근거
# 데이터(JSON) 안의 사실만 담고 있는지 별도 LLM 호출로 재검증하는 2차 패스를 추가함.
# 검증 자체도 temperature=0, JSON 스키마 강제라 "그럴듯한 말"이 아니라 구조화된 판정만 나옴.
# grounded=false면 답변을 그대로 노출하지 않고, 안전한 대체 문구(장학팀 확인 안내)로 바꿔치기함
# — HITL 승인 큐에 들어가기 전에 지어낸 내용이 학생한테 노출되는 것 자체를 막는 게 목적.

GROUNDING_CHECK_SYSTEM_PROMPT = """너는 사실검증 AI다. [근거 데이터]와 [생성된 답변]을 비교해서,
답변에 근거 데이터로 뒷받침되지 않는 구체적인 사실(금액, 날짜, 조건, 절차, 서류명 등)이 있는지 확인한다.
- 근거 데이터에 명시된 내용을 답변이 그대로 전달하거나 자연스럽게 풀어쓴 건 문제 없다(grounded=true).
- 근거 데이터에 없는 구체적인 금액/날짜/조건/절차/서류를 답변이 새로 만들어냈으면 grounded=false로
  하고 unsupported_claims에 그 문장을 그대로 적어라.
- "정확한 건 확인해봐", "장학팀에 문의해라"처럼 확인을 유도하는 안내 문구는 사실 주장이 아니니
  문제 없다.
- 판단이 애매하면 grounded=false로 둬라 — 환각을 놓치는 것보다 과잉검열이 훨씬 안전하다.
"""


def verify_grounding(answer: str, source_data: str) -> GroundingCheck:
    """생성된 답변이 근거 데이터(source_data) 밖의 사실을 지어내지 않았는지 검증.
    검증 호출 자체가 실패(네트워크 등)하면 학생 경험을 막지 않기 위해 통과(grounded=true)
    시키되, 최종 방어선인 HITL 승인은 그대로 남아있음."""
    prompt = f"""[근거 데이터]
{source_data}

[생성된 답변]
{answer}
"""
    try:
        text = _generate_json(
            prompt, GroundingCheck, temperature=0.0,
            system_instruction=GROUNDING_CHECK_SYSTEM_PROMPT,
        )
        return GroundingCheck.model_validate_json(text)
    except Exception as exc:
        print(f"[grounding-check] 검증 호출 실패, 통과 처리: {exc}")
        return GroundingCheck(grounded=True, unsupported_claims=[])


_GROUNDING_FALLBACK_ACTION = (
    "어 잠깐, 내가 갖고 있는 정보로는 {name} 신청 절차를 정확히 정리 못 하겠어 — "
    "틀린 안내 해주느니 장학팀에 직접 확인하고 알려줄게. 일단 서류는 {docs}부터 챙겨두면 돼!"
)
_GROUNDING_FALLBACK_CONSULT = (
    "어 그 부분은 내가 갖고 있는 정보로는 정확히 확인이 안 되네 — 지어내서 알려주느니 "
    "장학팀(담당 부서)에 직접 한 번 더 확인해보는 게 맞을 것 같아!"
)


# ---------- 3. 액션 가이드 ----------

def generate_action_guide(selected: Scholarship) -> str:
    prompt = f"""[선택된 장학금]
이름: {selected.name}
필요서류: {', '.join(selected.required_documents)}
신청절차: {' → '.join(selected.application_steps) if selected.application_steps else selected.how_to_apply}
출처: {selected.source_url or '정보 없음'}
"""
    answer = _generate_text(prompt, ACTIONABLE_PROMPT, temperature=0.3)
    check = verify_grounding(answer, selected.model_dump_json())
    if not check.grounded:
        print(f"[grounding-check] action_guide 근거 부족: {check.unsupported_claims}")
        return _GROUNDING_FALLBACK_ACTION.format(
            name=selected.name,
            docs=', '.join(selected.required_documents) or '필요서류 확인',
        )
    return answer


# ---------- 4. 선택 이후 자유 상담 ----------

# 실사용자 리포트: 학생이 최종 선택한 장학금(1개)으로 상담 단계에 들어간 뒤, "근데 1번은
# 얼마 주는거야?"처럼 자기가 고른 게 아닌 "다른 번호" 후보를 다시 물어보면, 이 함수가
# selected(선택된 것 딱 1개)만 갖고 있어서 그 후보의 실제 DB 정보(amount 등)를 아예 볼 수가
# 없었음 — 그래서 실제로는 DB에 금액이 버젓이 있는데도 "정보가 없다"고 지어내서 답하는
# 버그가 있었음. 아까 번호 붙여서 보여줬던 후보 전체 목록(all_candidates)을 같이 넘겨서,
# 선택 안 한 다른 번호에 대한 질문도 실제 데이터로 답할 수 있게 함.
def generate_consult_answer(
    conversation_history: str,
    selected: Scholarship,
    all_candidates: list[Scholarship] | None = None,
) -> str:
    candidates_block = ""
    if all_candidates:
        listing = "\n".join(
            f"{i + 1}번: {s.model_dump_json()}" for i, s in enumerate(all_candidates)
        )
        candidates_block = f"""

[아까 학생한테 번호 붙여서 보여준 후보 전체 목록 — 학생이 지금 선택한 것 말고 "n번은
얼마야/무슨 조건이야" 같은 식으로 다른 번호를 물어보면, 절대 모른다고 하지 말고 반드시
여기서 그 번호를 찾아 실제 데이터로 답해라. 여기 없는 정보(예: amount가 실제로 null)만
"정확한 건 확인해봐"라고 안내해라]
{listing}"""

    prompt = f"""[학생이 선택한 장학금 정보]
{selected.model_dump_json(indent=2)}
{candidates_block}

[지금까지 대화]
{conversation_history}

위 대화의 마지막 학생 질문에 자연스럽게 답해라. 선택한 장학금이 아니라 다른 번호의 후보를
물어보면, 위 [후보 전체 목록]에서 그 번호를 찾아서 답해라 — 목록에 있는데도 "정보 없다"고
하면 안 된다.
"""
    answer = _generate_text(prompt, CONSULT_PROMPT, temperature=0.4)

    source_data = selected.model_dump_json()
    if all_candidates:
        source_data += "\n" + json.dumps(
            [c.model_dump() for c in all_candidates], ensure_ascii=False
        )
    check = verify_grounding(answer, source_data)
    if not check.grounded:
        print(f"[grounding-check] consult_answer 근거 부족: {check.unsupported_claims}")
        return _GROUNDING_FALLBACK_CONSULT
    return answer


# ---------- 5. RAG 자료 추가 (공지 원문 → 구조화 Scholarship) ----------

def extract_scholarship(raw_markdown: str) -> Scholarship:
    prompt = f"""아래는 대학 장학금(또는 기타) 공지사항이다. 내용을 보고 정확히 구조화해서 추출하라.
공지문에 명시되지 않은 조건은 추측하지 말고 null/빈 값으로 둬라.
날짜는 YYYY-MM-DD 형식으로, "상시"나 "정보 없음"이면 null로 둬라.
gpa_requirement_percent는 "70/100"이면 70.0, "3.0/4.5"면 66.7처럼 100점 만점으로 환산하되,
환산이 애매하면 null로 두고 gpa_requirement_raw만 채워라.
is_open_application은 "지금 학생이 신규로 신청 가능한 장학금 공지"일 때만 true로,
선발결과 발표·사후절차 안내·정책 예고 같은 건 false로 표시하라.
benefit_type은 장학금/근로장학/대출/이자지원/등록금지원 중 하나로 분류하라 (대출류는 장학금과 다르게 분류).

[공지 원문]
{raw_markdown}
"""
    text = _generate_json(prompt, Scholarship, temperature=0.0)
    return Scholarship.model_validate_json(text)


# ---------- 5-1. 장학금 매칭 2차 — 자유서술형 조건 소프트매칭 ----------
#
# 실제 DB(68건) 조사 결과: 신청가능한 41건 전부가 special_conditions를 공지 원문
# 그대로의 자유텍스트로 갖고 있음(예: "국민기초생활 수급자 우대", "본인 또는 부모가
# 대전 거주") — 학과제한(major_restriction)·학년조건(grade_or_semester_note)도 마찬가지로
# 자유텍스트라 고정된 어휘가 없음. matching.py의 1차 규칙필터는 이걸 정확매칭으로 거르지
# 않고 전부 후보에 남겨두는데, 그러면 이번엔 반대로 전혀 안 맞는 후보까지 다 보여주게 됨.
# 그래서 여기서 LLM한테 "학생 상황이 이 조건과 맞는지"를 판단하게 하는 2차 소프트매칭을
# 한 번 거침 (uni_voc_장학금챗봇_MVP설계.md 0.6장 5·9번에서 이미 이 방식을 권고함).
#
# 설계 원칙: 이것도 "최종 자격 확정"이 아니라 "후보 정리 보조판단"임 — 장학금은 결국
# 학생이 직접 신청해서 학교 심사를 받아야 확정되는 거라, 여기서 잘못 걸러내서 진짜
# 받을 수 있는 장학금을 안 보여주는 게 안 맞는 걸 보여주는 것보다 훨씬 나쁨. 그래서
# 프롬프트에서 "애매하면 무조건 배제하지 말고 포함시켜라"를 명시적으로 강제함.

SOFT_MATCH_SYSTEM_PROMPT = """너는 대학 장학금 자격조건 보조판단 AI다. 학생 정보와 장학금별
특례조건(공지 원문 그대로, 고정된 어휘 없음)을 비교해서, 이 후보를 계속 보여줘도 되는지 판단한다.
너의 기본값은 무조건 eligible=true다 — 아래 "명백한 모순" 기준에 정확히 해당할 때만 false를
쓰고, 그 외에는 전부 true를 써야 한다. 이건 최종 심사가 아니라 "혹시 몰라서 보여주는 후보
찾기"라서, 안 보여줘도 될 걸 보여주는 것보다 보여줘야 할 걸 놓치는 게 훨씬 나쁘다.

eligible=false는 딱 하나의 경우에만 써라:
- 학생이 명시적으로 말한 사실이 조건과 정면으로, 100% 모순될 때만
  (예: 조건은 "대전 거주"인데 학생이 "나 대구 살아"라고 실제로 말했을 때).

절대로 false로 쓰면 안 되는 경우들 (전부 흔한 실수니까 특히 조심해라):
- 학생이 그 조건에 대해 아무 말도 안 한 경우 ("말 안 했다"는 "아니다"라는 뜻이 아니다).
- 학생이 다른 특례조건 하나를 말했다고 해서, 이 조건은 자동으로 해당 안 된다고 넘겨짚는 경우
  (예: 학생이 "다자녀야"라고만 말했다고, 국적/지역/소득 관련 다른 조건들까지 전부 "해당 안 됨"
  으로 단정하지 마라 — 그 학생이 다자녀"이면서 동시에" 다른 조건에도 해당할 수 있다. 말 안 한
  건 그냥 모르는 거다).
- "우대"라고 적힌 조건 — 필수요건이 아니라 가산요소라서, 학생이 해당 안 되더라도 배제 사유가
  아니다.
- 소속 대학(영남대)이나 전공만으로 지역/가구형태 등을 추측하는 경우 — "영남대생이니까 아마
  대구/경산에 살겠지" 같은 짐작은 명시적으로 말한 사실이 아니니 절대 근거로 쓰지 마라.

eligible=false를 쓸 때도 reason에 학생이 정확히 뭐라고 말했는지(어떤 발언과 모순되는지) 한 줄로
남겨라. eligible=true인데 확인이 필요한 상황이면 reason에 "확인 필요: 왜"를 짧게 남겨라.
목록에 있는 scholarship_id는 하나도 빠짐없이 results에 담아라. 절대 조건을 지어내거나 새로운
기준을 만들지 마라 — 주어진 조건 원문 안에서만 판단해라.
"""


def soft_match_conditions(
    student_profile: str, candidates: list[dict]
) -> dict[str, tuple[bool, str]]:
    """candidates: [{"scholarship_id": ..., "conditions": [...]}] — 장학금별 자유서술형 조건 원문.
    반환: {scholarship_id: (eligible, reason)}. candidates가 비어있으면 호출 자체를 생략."""
    if not candidates:
        return {}
    prompt = f"""{SOFT_MATCH_SYSTEM_PROMPT}

[학생 정보 — 지금까지 대화에서 확인된 내용]
{student_profile}

[판단할 장학금별 자유서술형 조건 원문]
{json.dumps(candidates, ensure_ascii=False, indent=2)}
"""
    text = _generate_json(prompt, SoftMatchBatch, temperature=0.0)
    batch = SoftMatchBatch.model_validate_json(text)
    return {r.scholarship_id: (r.eligible, r.reason) for r in batch.results}


# ---------- 5-2. 장학금 매칭 1.5차 — 후보들의 실제 특례조건을 직접 짚어 물어보기 ----------
#
# soft_match_conditions()는 학생이 "말한 것"에 근거해 판단하지만, 그동안은 학생이 애초에
# 그 조건에 대해 말할 기회 자체가 없었음(region 슬롯 하나 추가한다고 해결 안 됨 — 사용자
# 피드백: "고작 거주지역을 한번더물어본다고 걸러지겠냐 좀더 공격적으로 물어보고 이장학금에는
# 해당안하는지 등등 세세하게 물어봐야지"). 그래서 1차 규칙필터를 통과한 실제 후보들의
# special_conditions 원문(중복 제거)을 모아서, "이 중에 너 해당되는 거 있어?"라고 구체적으로
# 직접 물어보는 단계를 하나 추가함.
#
# 처음엔 학생 답변을 별도 LLM 호출로 한 번 더 구조화해서(extract_condition_answers) 넘겼는데,
# 그러면 slot_filling(추출) → condition_check 질문 생성 → 답변 추출 → soft_match → 후보제시로
# 한 턴에 LLM을 3번 연달아 호출하게 돼서 체감 응답 속도가 눈에 띄게 느려짐(사용자 피드백으로
# 확인). soft_match_conditions는 애초에 대화 이력 전체를 프롬프트에 받고 "말 안 한 건 모르는
# 거지 아니라는 뜻이 아니다"라는 가드레일도 이미 갖고 있어서, 학생의 답변을 별도로 구조화하지
# 않고 convo(대화 이력, 방금 물어본 질문+학생 답 포함) 그대로 넘겨도 정확도 손해 없이 판단
# 가능함 — 그래서 별도 추출 단계는 제거하고 2번으로 줄임(app.py의 filter_by_soft_conditions 참고).

def generate_condition_check_question(
    conversation_history: str, conditions: list[str]
) -> str:
    listing = "\n".join(f"- {c}" for c in conditions)
    prompt = f"""아래는 1차로 조건이 맞는 장학금 후보들에 실제로 달려있는 특례조건 원문 몇 개다
(이미 소수로 추려놓은 것들이다). 이걸 학생한테 물어보되, 절대 번호/항목 나눠서 목록·표처럼
딱딱하게 다 늘어놓지 마라 — 의사가 진료할 때 증상을 한꺼번에 쫙 나열하면서 묻지 않고, 짧게
"이런 거 해당돼?" 하고 몇 개만 골라 자연스러운 말투로 툭 던지듯 묻는 것처럼, 한두 문장 안에
자연스럽게 녹여서 캐주얼하게 물어봐라(예: "혹시 한부모가정이거나 다자녀야? 이런 거 있으면
알려줘!"). 목록에 없는 조건을 지어내지 마라.

[물어볼 특례조건]
{listing}

[대화 이력]
{conversation_history}
"""
    return _generate_text(prompt, CHAT_SYSTEM_PROMPT, temperature=0.5)


# =========================================================
# 시나리오2: "복수전공 가능한가요?" — 자격요건 판정
#
# 설계 원칙: 자격요건 "판정"(가능/불가능) 자체는 절대 LLM이 하지 않음.
# LLM은 (a) 자연어 대화 -> 슬롯 추출, (b) rules.py가 이미 계산한 결정론적
# 결과를 자연스러운 문장으로 풀어 설명하는 역할만 맡음. 근거가 이미 계산돼
# 있으므로 이 단계에서 LLM이 지어낼 여지가 없음 — 돈/졸업요건에 영향 주는
# 판단이라 위험도가 높다고 보고 의도적으로 이렇게 나눔.
# =========================================================

DUAL_MAJOR_SYSTEM_PROMPT = """너는 영남대학교 복수전공 자격요건 안내 AI다. 친근하고 간결한 말투로 답한다.
- 정보가 부족하면 자연스럽게 하나씩 질문한다 (한 번에 2개까지는 묶어서 물어봐도 됨).
- 절대로 규정을 지어내지 않는다.
- 사용자가 반말/줄임말을 써도 편하게 맞춰서 대답한다.
- 실제 학과 상담사처럼 따뜻하게 대해라 — 요건만 딱딱하게 캐묻지 말고, 학생이 고민이나 걱정을
  비치면 짧게 공감해준 다음 이어가고, 잡담이나 안부에도 사람처럼 자연스럽게 반응한 뒤 부드럽게
  본론으로 돌아와라.
""" + _OUTPUT_STYLE_RULES

DUAL_MAJOR_VERDICT_PROMPT = """너는 영남대학교 복수전공 자격요건 안내 AI다.
아래는 이미 규정에 따라 계산이 끝난 판정 결과다. 이 결과를 바꾸거나 새로운 조건을 지어내지 말고,
주어진 내용을 학생에게 친근하고 명확하게 설명만 하라.
- eligible이 true면 축하 인사와 함께 다음 단계(정식 신청은 URP종합정보 학적관리 메뉴에서 가능하다는 것)를 안내한다.
- eligible이 false면 어떤 조건이 왜 부족한지 failed 항목을 풀어서 설명하고, 부족한 게 채워지면 다시 확인 가능하다고 안내한다.
- notes에 있는 내용(문의처, 예외 가능성 등)은 참고사항으로 자연스럽게 붙인다.
- 이미 이전에 들은 과목이 있어서 복수전공 학점으로 인정받고 싶은 경우가 있는지, 궁금하면 물어봐도 된다고 짧게 안내한다.
""" + _OUTPUT_STYLE_RULES


def extract_dual_major_slots(
    conversation_history: str, current_state: DualMajorState
) -> DualMajorState:
    prompt = f"""아래는 학생과 복수전공 자격요건 안내 챗봇의 대화 이력이다.
대화에서 명시적으로 언급된 정보만 추출하라. 언급 안 된 항목은 null로 둬라.
이미 알고 있는 정보는 그대로 유지하고, 새로 언급된 정보만 채워라.
grad_credit_basis(졸업학점 기준)는 120/130/140/150/160 중 하나여야 한다 — 학생이 모르면 null로 두고,
대부분 학과는 120학점이 기본이라는 걸 참고해서 되물어봐도 된다.

[현재까지 알고 있는 정보]
{current_state.model_dump_json()}

[대화 이력]
{conversation_history}
"""
    text = _generate_json(prompt, DualMajorState)
    return DualMajorState.model_validate_json(text)


def generate_dual_major_followup_question(
    conversation_history: str, missing: list[str]
) -> str:
    prompt = f"""아래 대화 이력을 보고, 아직 모르는 정보인 {missing}에 대해 자연스럽게 이어서 질문하라.
이미 아는 정보는 다시 묻지 마라. grad_credit_basis를 물어볼 땐 "본인 학과 졸업학점이 120/130/140/150/160학점 중
몇 학점 기준인지"처럼 구체적으로 물어봐라 (모르면 보통 120학점이라고 알려줘도 된다).

[대화 이력]
{conversation_history}
"""
    return _generate_text(prompt, DUAL_MAJOR_SYSTEM_PROMPT, temperature=0.5)


def generate_dual_major_verdict_message(result: EligibilityResult) -> str:
    prompt = f"""[판정 결과 — 이미 계산 완료, 절대 바꾸지 말 것]
{result.model_dump_json(indent=2)}
"""
    return _generate_text(prompt, DUAL_MAJOR_VERDICT_PROMPT, temperature=0.3)


def generate_dual_major_consult_answer(
    conversation_history: str, result: EligibilityResult
) -> str:
    prompt = f"""[이미 나온 판정 결과 — 절대 바꾸지 말 것]
{result.model_dump_json(indent=2)}

[지금까지 대화]
{conversation_history}

위 대화의 마지막 학생 질문에 답해라. 판정 결과 자체를 새로 계산하거나 바꾸지 말고,
이미 나온 결과를 기준으로만 설명해라. "이미 들은 과목을 인정받고 싶다" 같은 요청이 오면,
반갑게 응하고 이제부터 이미 들은 과목을 하나씩 말해달라고 안내해라
(예: "오 좋아! 이미 들은 과목 있으면 말해줘 — 과목명(학수번호 알면 같이) 편하게 알려줘").
"""
    return _generate_text(prompt, DUAL_MAJOR_SYSTEM_PROMPT, temperature=0.4)


# =========================================================
# 시나리오2 2단계: 이수과목 인정신청서 자동 초안
#
# 설계 원칙 동일: "이 과목이 인정되는지"는 LLM이 절대 판단하지 않음.
# LLM은 (a) 학생이 말한 과목명을 구조화 추출, (b) curriculum.py가 이미
# 계산한 매칭 결과를 자연스러운 문장으로 설명하는 역할만 맡음.
# =========================================================

COURSE_EXTRACTION_SYSTEM_PROMPT = """너는 대학 행정 보조 AI다. 학생이 이미 이수한 과목을
자연어로 말하면, 언급된 과목명(및 학수번호가 있으면 학수번호, 실제로 이수한 연도/학기를
같이 말했으면 그것도)만 정확히 추출한다.
- 과목명을 절대 지어내거나 고쳐 쓰지 않는다 (학생이 말한 표현 그대로 course_name에 담는다).
- 학수번호가 언급 안 됐으면 course_code는 null로 둔다.
- taken_year/taken_semester: 학생이 "2024년 1학기", "작년 2학기", "2학년 1학기"처럼 실제로
  그 과목을 이수한 시점을 그 과목과 같이 명시적으로 말했을 때만 채운다(연도만 말했으면
  taken_year만, 학기만 말했으면 taken_semester만 채워도 됨). 교육과정표에 이 과목이 몇 학년
  때 편성돼 있는지를 추측해서 채우면 절대 안 된다 — 실제로 학생이 들은 시점은 사람마다
  다를 수 있는 별개의 사실이다. 말 안 했으면 둘 다 null로 둬라.
- "더 없어", "이제 없어", "그게 다야", "끝" 같은 표현이 있으면 done을 true로 한다.
- 학생이 이미 언급했던 과목을 "빼줘", "취소", "그거 아니었어", "제외해줘"처럼 빼달라고
  하면, 그 과목명을 removed_course_names에 담는다 — 단, 반드시 아래로 전달되는
  "이미 추출된 과목명 목록"에 실제로 있는 이름 중에서만 골라야 한다(그 목록에 없는
  이름을 지어내서 removed_course_names에 넣으면 절대 안 된다).
""" + _OUTPUT_STYLE_RULES


def extract_completed_courses(
    conversation_history: str, already_extracted: list[str]
) -> CompletedCoursesExtraction:
    prompt = f"""아래는 학생이 이미 이수한 과목을 말하는 대화 이력이다.
이미 추출된 과목명 목록(중복 추출 방지용이자, 빼달라는 요청이 있을 때 고를 수 있는 유일한
후보 목록): {already_extracted}

가장 최근 학생 발화에서 새로 언급된 과목만 courses에 담아라 (이미 추출된 건 다시 담지 마라).
과목명을 지어내지 마라 — 언급 안 된 과목은 절대 포함하지 마라.
학생이 위 목록 중 일부를 빼달라고 했으면 그 이름을 removed_course_names에 담아라
(위 목록에 없는 이름은 절대 담지 마라). 빼달라는 말이 없으면 빈 리스트로 둬라.

[대화 이력]
{conversation_history}
"""
    text = _generate_json(prompt, CompletedCoursesExtraction, temperature=0.0)
    return CompletedCoursesExtraction.model_validate_json(text)


def generate_course_ask_more_message(conversation_history: str, collected_so_far: list[str]) -> str:
    prompt = f"""지금까지 학생이 말한 이수과목: {collected_so_far or '아직 없음'}
학생에게 더 이수한 과목이 있으면 계속 말해달라고 하고, 다 말했으면 "다 말했어"처럼 알려달라고
짧고 친근하게 안내해라. 이번이 첫 안내라면(collected_so_far가 비어있다면), 가능하면 몇 학년/
몇 학기(또는 몇 년도 몇 학기)에 들었는지도 과목명이랑 같이 말해주면 신청서 칸까지 더 정확히
채워줄 수 있다고 짧게 한 번 덧붙여도 좋다(강요하지는 말 것 — 몰라도 나중에 따로 물어볼
거니까 괜찮다는 뉘앙스로).

[대화 이력]
{conversation_history}
"""
    return _generate_text(prompt, COURSE_EXTRACTION_SYSTEM_PROMPT, temperature=0.5)


# ---------- 5-1-2. 이수과목 인정신청서 2단계 보조: "이 과목 몇 학년/학기에 들었어?" ----------
#
# 공식 양식엔 과목별로 '이수학기(연도/학기)' 칸이 따로 있는데, 학생이 course_collect
# 단계에서 처음부터 말해주지 않으면 챗봇이 아는 정보가 없어서 계속 빈칸이었음(실사용자
# 요청: "이수학기도 채워주면 좋겠는데"). 교육과정표의 편성 학년/학기를 대신 채우면
# 실제 이수 시점을 지어내는 게 되므로(사람마다 언제 들었는지 다를 수 있음), 반드시
# 학생 본인한테 확인받은 값만 채운다 — 매칭 여부 판단과 마찬가지로 LLM은 추출만,
# 최종 반영은 코드(app.py)가 후보 목록과 대조해서 검증한 뒤 처리한다.

COURSE_SEMESTER_PROMPT = """학생한테 아래 과목들을 실제로 몇 년도/몇 학기에 이수했는지
물어봤고, 방금 학생이 답했다.

[물어본 과목 목록]
{candidates}

[학생 답변]
"{user_message}"

규칙:
- course_name은 반드시 위 후보 목록에 있는 과목명 중에서만, 정확히 그대로 써라. 목록에 없는
  과목명을 새로 만들어내거나 비슷하게 바꿔 쓰지 마라.
- taken_year: "2024년"처럼 연도를 명시했을 때만 그 4자리 숫자를 문자열로 담아라(예: "2024").
  "2학년"처럼 학년만 말한 건 연도가 아니니 taken_year에 넣지 마라.
- taken_semester: "1학기"/"2학기"라고 했으면 "1"/"2"로, "여름학기"/"겨울학기"라고 했으면
  그 표현 그대로 담아라. "2학년 1학기"처럼 학년+학기를 같이 말했으면 학기 부분(1)만 담아라.
- 특정 과목에 대해 아무 정보도 안 줬으면 그 과목은 items에 아예 포함하지 마라(빈 값 채워
  넣지 말고 생략).
- "몰라"/"모르겠어"처럼 전체를 모른다고 하면 items를 빈 리스트로 둬라.
"""


def extract_course_semesters(candidates: list[str], user_message: str) -> CourseSemesterExtraction:
    """course_semester_check 단계에서 학생 답변을 후보 과목별 이수 연도/학기로 구조화.
    실패해도 신청서 생성 자체는 막히면 안 되므로 예외는 호출부에서 흡수함."""
    prompt = COURSE_SEMESTER_PROMPT.format(candidates=candidates, user_message=user_message)
    text = _generate_json(prompt, CourseSemesterExtraction, temperature=0.0)
    return CourseSemesterExtraction.model_validate_json(text)


DRAFT_EXPLAIN_PROMPT = """너는 대학 복수전공 이수과목 인정신청서 초안을 학생에게 설명하는 AI다.
아래는 이미 규정에 따라 계산이 끝난 매칭 결과다 — 이 결과를 바꾸거나 새로운 판단을 하지 말고
그대로 설명만 하라.

아주 중요: 이 시스템은 학생이 밑에서 "제출하기"를 누르면, 먼저 챗봇이 이름/학번/학년/
소속 단과대학을 채팅으로 몇 개 더 확인한 다음, 그 정보까지 전부 반영해서 학교 공식
'부(복수)전공 이수과목 인정신청서' 양식(.docx 파일)에 과목과 개인정보를 직접 채우는 동시에,
전산으로 (1) 학과장 (2) 복수전공학과장(이 둘은 순서 상관없이 동시에 처리 가능) (3) 행정처
(학과장 둘이 전부 승인한 뒤에만) 순서로 3단계 승인 절차에 자동으로 올려서, 승인이 전부
끝나면 전산 반영까지 된다. 그러니 "이 내용을 참고해서 네가 직접 URP나 학과 사무실 양식에
옮겨 적어서 내"라거나 "학과장 찾아가서 서명 받아와"처럼 학생더러 손으로 하라고 시키지
마라 — 서류 들고 서명 받으러 다니는 수고를 없애려고 만든 기능인데 그걸 안내 안 하고 손으로
하라고 하면 이 기능을 만든 의미가 없다. 학생이 할 일은 딱 하나, 제출하기 누르고
이름/학번/학년/소속 단과대학 몇 개만 확인해주는 것뿐이라고 안내해라 — 그다음 승인 절차는
전부 시스템이 전산으로 알아서 처리한다.

- matched_courses는 목표 학과 교육과정표와 실제로 대조해서 확인된 과목들 — "이 과목들은
  인정신청서에 넣을 수 있어"라고 안내한다. 각 과목의 matched_note(타전공인정 등)가
  있으면 왜 인정 가능한지 근거로 자연스럽게 언급한다.
- unmatched_course_names는 학생이 말했지만 목표 학과 교육과정표에서 정확히 일치하는 과목을
  찾지 못한 것들 — "이 과목들은 목표 학과 교육과정표에 정확히 일치하는 과목이 없어서 이번
  신청서에는 못 넣어. 비슷한 과목이라도 완전히 같은 과목이 아니면 자동으로는 인정 못 하고,
  필요하면 학과 사무실에 별도 문의해봐"처럼 설명한다.
- matched_courses가 하나도 없으면, 신청서를 만들 게 없다고 솔직하게 안내한다(이 경우엔
  제출 버튼 얘기를 하지 마라 — 만들 신청서가 없으니까).
- matched_courses가 있으면 마지막에 "이대로 제출할까? 제출하기 누르면 양식 파일로 바로
  만들어줄게"처럼 확인을 구하되, 매번 똑같은 문장 말고 자연스럽게 표현을 바꿔라.
""" + _OUTPUT_STYLE_RULES


def generate_recognition_draft_message(
    target_major: str, matched: list[MatchedCourse], unmatched: list[str]
) -> str:
    prompt = f"""[목표 복수전공]
{target_major}

[매칭 결과 — 이미 계산 완료, 절대 바꾸지 말 것]
인정 가능(matched_courses): {[m.model_dump() for m in matched]}
인정 불가/미확인(unmatched_course_names): {unmatched}
"""
    return _generate_text(prompt, DRAFT_EXPLAIN_PROMPT, temperature=0.3)


STUDENT_IDENTITY_EXTRACTION_PROMPT = """학생이 방금 자기 이름/학번/학년/소속 단과대학/이메일을
알려주는 메시지에서 그 정보만 그대로 뽑아내라. 지어내지 말고, 메시지에 없는 항목은 null로
둬라(예: 학번만 말했으면 나머지는 null). "3학년"처럼 학년만 말해도 grade에 그대로 담고,
"공과대학"처럼 단과대학 이름만 말해도 college에 그대로 담아라. email은 "xxx@yyy.zzz" 형태로
이메일 주소가 명확하게 포함돼 있을 때만 담고, 없으면 절대 지어내지 마라."""


def extract_student_identity(user_message: str) -> StudentIdentity:
    """신청서에 실제로 들어갈 학생 본인 정보(성명/학번/학년/소속 단과대학/이메일) 추출 —
    인정 여부처럼 판단이 필요한 게 아니라 학생이 말한 걸 그대로 옮기는 것뿐이라 LLM이 해도
    되는 범위(구조화 파싱). 실패해도 신청 자체가 막히면 안 되므로 예외는 호출부에서 흡수하고
    빈 값으로 계속 물어보게 함.
    (기존에 STUDENT_IDENTITY_EXTRACTION_PROMPT가 정의만 되고 실제로는 안 넘겨지고 있었음 —
    이메일 지어내지 말라는 규칙이 실제로 적용되게 system_instruction으로 제대로 연결함.)"""
    text = _generate_json(
        user_message, StudentIdentity, temperature=0.0, system_instruction=STUDENT_IDENTITY_EXTRACTION_PROMPT
    )
    return StudentIdentity.model_validate_json(text)


SUBMITTED_PROMPT = """너는 대학 복수전공 이수과목 인정신청서 제출 완료를 안내하는 AI다.
아래는 방금 제출된 신청서 정보다.

아주 중요(예전 방식과 달라진 부분 — 절대 옛날 방식으로 안내하지 마라): 이 시스템은 학생이
서류를 인쇄해서 학과장을 직접 찾아가 서명을 받아야 하던 수고를 없애려고 만든 거다. 제출하는
순간 신청서가 전산으로 (1) 학과장, (2) 복수전공학과장 — 이 둘은 순서 상관없이 동시에 처리될
수 있음 — 을 거쳐 (3) 행정처(학과장 둘이 전부 승인한 뒤에만 처리) 순서로 3단계 승인
절차에 자동으로 올라간다. 그러니 "서명 받아서 내라", "학과장 찾아가라", "인쇄해서 제출해라"
같은 안내는 절대 하지 마라 — 전부 전산으로 처리된다. 3단계 승인이 모두 완료되면 전산에
자동 반영된다는 것과, 진행상황/승인·반려 결과는 학생이 별도로 안내받게 된다는 걸 친근하게
설명해라.

화면에 방금 뜬 "신청서 양식(.docx)으로 다운로드" 버튼을 누르면, 지금까지의 승인 진행상황이
그대로 반영된 참고·보관용 파일을 받을 수 있다고 짧게 언급해라(서명 받으러 들고 다닐 필요는
없고, 순전히 확인·보관용).

판단이나 새로운 조건을 지어내지 마라.""" + _OUTPUT_STYLE_RULES


def generate_recognition_submitted_message(application: RecognitionApplication) -> str:
    prompt = f"""[제출된 신청서]
{application.model_dump_json(indent=2)}
"""
    return _generate_text(prompt, SUBMITTED_PROMPT, temperature=0.3)


# =========================================================
# 시나리오2 2단계 보조: "혹시 이 과목도 들었는데 깜빡한 거 아니야?" 확인
#
# 홈학과·목표학과 교육과정표에 둘 다 올라있는데 학생이 course_collect 단계에서
# 언급 안 한 과목을 curriculum.find_potential_overlap()이 결정론적으로 찾아서 후보로
# 제시하면, LLM은 학생의 답변("1,2번 들었어" / "다 들었어" / "없어" 등)을 후보 목록
# 안에서만 골라 구조화하는 역할만 맡는다 — 여기서도 "이 과목을 들었는지 여부"를
# LLM이 판단/추측하지 않고, 학생이 실제로 확인한 것만 그대로 옮긴다.
# =========================================================

OVERLAP_CONFIRMATION_PROMPT = """학생한테 아래 후보 과목 목록 중에 실제로 이수한 과목이
있는지 물어봤고, 방금 학생이 답했다.

[제시한 후보 과목명 목록]
{candidates}

[학생 답변]
"{user_message}"

규칙:
- 반드시 위 후보 목록에 있는 과목명 중에서만 confirmed_course_names에 담아라. 목록에 없는
  과목명을 새로 만들어내거나 비슷한 이름으로 바꿔 쓰지 마라 — 후보 목록의 과목명을 정확히
  그대로 써야 한다.
- "다 들었어"/"전부"/"다"처럼 전체를 가리키면 후보 전부를 담아라.
- "없어"/"안 들었어"/"아니"처럼 전체 부정이면 빈 리스트를 반환해라.
- "1번이랑 3번"처럼 번호로 답하면 후보 목록 순서대로 매칭해서 담아라.
- 일부 과목명만 콕 집어 말했으면 그 과목들만 담아라.
- 애매하거나 후보 목록과 무관한 대답이면 빈 리스트로 둬라(추측해서 채우지 마라).
"""


def extract_overlap_confirmation(candidates: list[str], user_message: str) -> OverlapConfirmation:
    """겹침 확인 후보(candidates) 중 학생이 실제로 이수했다고 확인한 과목명만 추출.
    실패해도 진행이 막히면 안 되므로 예외는 호출부에서 흡수함. 호출부에서도 반환값이
    candidates 안에 실제로 있는지 한 번 더 검증한다(프롬프트만으로 LLM의 과목명 창작을
    막지 않고 코드로 이중 검증 — 이 프로젝트 핵심 설계원칙과 동일한 이유)."""
    prompt = OVERLAP_CONFIRMATION_PROMPT.format(candidates=candidates, user_message=user_message)
    text = _generate_json(prompt, OverlapConfirmation, temperature=0.0)
    return OverlapConfirmation.model_validate_json(text)
