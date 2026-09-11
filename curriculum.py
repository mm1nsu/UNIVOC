"""
학과별 교육과정표 데이터 + 이수과목 인정 매칭 로직 — 순수 데이터/규칙, LLM 사용 안 함.

데모용으로 학과 조합 1쌍을 확정해서 실제 이수지침 PDF(2026학년도 교육과정 이수지침)의
해당 학과 페이지를 원문 그대로 옮겨 구조화함 (추측/생성 금지, 전부 원문 대조 완료):
- 홈학과(예시): 미래자동차공학과 (디지털융합대학) — PDF 문서상 156~157p
- 목표 복수전공: 컴�터공학전공 (디지털융합대학 컴퓨터학부) — PDF 문서상 148~150p

핵심 실증 포인트: 컴퓨터공학전공 커리큘럼표 4학년/1학기에 "AME057 취업/현장기술세미나"가
"타전공인정 미래자동차공학과"로 이미 공식 편성되어 있음 — 즉 미래자동차공학과 학생이
이 과목을 이수했다면 이수지침 II장 6.차에 따라 복수전공(컴퓨터공학전공) 이수과목으로
인정 신청이 가능한 실제 사례. 이 매칭은 학수번호(코드) 완전일치를 기준으로 하며
가짜/추측 데이터가 아니라 원문 표를 그대로 옮긴 것.

매칭 로직 자체는 규칙 기반(학수번호 정확히 일치)이며 LLM이 관여하지 않음 —
학생 자연어 입력을 구조화하는 건 bot_core.extract_completed_courses가 담당,
"인정 대상인지 아닌지"의 최종 판단은 이 파일의 함수만 담당.
"""
from typing import Optional

from schemas import CompletedCourseItem, MatchedCourse

# ---------------------------------------------------------------------------
# 원문 대조 완료된 커리큘럼 데이터
# 각 항목: code(학수번호), name(교과목명), credit(학점), category(구분),
#          year_semester(학년/학기), note(비고 — 타전공인정 등)
# ---------------------------------------------------------------------------

미래자동차공학과 = [
    # 1학년/1학기
    {"code": "U00801", "name": "C프로그래밍", "credit": 3, "category": "교양필수", "year_semester": "1학년/1학기"},
    {"code": "U00911", "name": "대학생활과전공설계", "credit": 1, "category": "교양필수", "year_semester": "1학년/1학기"},
    {"code": "U00145", "name": "미분적분학(1)", "credit": 3, "category": "교양필수", "year_semester": "1학년/1학기"},
    {"code": "U00787", "name": "사회공헌과봉사", "credit": 1, "category": "교양필수", "year_semester": "1학년/1학기"},
    {"code": "U00361", "name": "일반물리(1)", "credit": 3, "category": "교양필수", "year_semester": "1학년/1학기"},
    {"code": "U00364", "name": "일반물리실험(1)", "credit": 1, "category": "교양필수", "year_semester": "1학년/1학기"},
    {"code": "AME060", "name": "공학입문설계", "credit": 1, "category": "전공선택", "year_semester": "1학년/1학기"},
    {"code": "AME077", "name": "미래자동차공학기초", "credit": 3, "category": "전공선택", "year_semester": "1학년/1학기"},
    # 1학년/2학기
    {"code": "U00786", "name": "소프트웨어와인공지능", "credit": 3, "category": "교양필수", "year_semester": "1학년/2학기"},
    {"code": "U00258", "name": "실용영어", "credit": 3, "category": "교양필수", "year_semester": "1학년/2학기"},
    {"code": "U00514", "name": "행렬및행렬식", "credit": 3, "category": "교양필수", "year_semester": "1학년/2학기"},
    {"code": "AME014", "name": "전산기계제도", "credit": 1, "category": "전공핵심", "year_semester": "1학년/2학기"},
    {"code": "AME059", "name": "임베디드시스템", "credit": 3, "category": "전공선택", "year_semester": "1학년/2학기"},
    # 2학년/1학기
    {"code": "AME019", "name": "3D모델링", "credit": 2, "category": "전공핵심", "year_semester": "2학년/1학기"},
    {"code": "AME002", "name": "고체역학", "credit": 3, "category": "전공핵심", "year_semester": "2학년/1학기"},
    {"code": "AME005", "name": "공학설계", "credit": 1, "category": "전공핵심", "year_semester": "2학년/1학기"},
    {"code": "AME067", "name": "일반자동차기능실험", "credit": 1, "category": "전공핵심", "year_semester": "2학년/1학기"},
    {"code": "AME080", "name": "전기전자회로", "credit": 3, "category": "전공핵심", "year_semester": "2학년/1학기"},
    {"code": "AME076", "name": "공업수학", "credit": 3, "category": "전공선택", "year_semester": "2학년/1학기"},
    {"code": "AME009", "name": "수치해석", "credit": 3, "category": "전공선택", "year_semester": "2학년/1학기"},
    # 2학년/2학기
    {"code": "AME008", "name": "동역학", "credit": 3, "category": "전공핵심", "year_semester": "2학년/2학기"},
    {"code": "AME012", "name": "유체역학", "credit": 3, "category": "전공핵심", "year_semester": "2학년/2학기"},
    {"code": "AME046", "name": "전기자동차공학", "credit": 3, "category": "전공핵심", "year_semester": "2학년/2학기"},
    {"code": "AME074", "name": "전기자동차기능실험", "credit": 1, "category": "전공핵심", "year_semester": "2학년/2학기"},
    {"code": "AME062", "name": "ROS기반임베디드시스템응용", "credit": 3, "category": "전공선택", "year_semester": "2학년/2학기"},
    {"code": "AME081", "name": "전산구조해석", "credit": 3, "category": "전공선택", "year_semester": "2학년/2학기"},
    # 3학년/1학기
    {"code": "AME006", "name": "기계요소설계", "credit": 3, "category": "전공핵심", "year_semester": "3학년/1학기"},
    {"code": "AME064", "name": "미래자동차융합실험", "credit": 1, "category": "전공핵심", "year_semester": "3학년/1학기"},
    {"code": "AME079", "name": "자동차재료및가공", "credit": 3, "category": "전공핵심", "year_semester": "3학년/1학기"},
    {"code": "AME011", "name": "열전달", "credit": 3, "category": "전공선택", "year_semester": "3학년/1학기"},
    {"code": "AME058", "name": "자동차공학", "credit": 3, "category": "전공선택", "year_semester": "3학년/1학기"},
    {"code": "AME041", "name": "자동차전산열유체해석", "credit": 3, "category": "전공선택", "year_semester": "3학년/1학기"},
    {"code": "AME045", "name": "자율주행자동차", "credit": 3, "category": "전공선택", "year_semester": "3학년/1학기"},
    {"code": "IPF004", "name": "미래자동차경량소재및구조에너지저장장치", "credit": 3, "category": "전공선택",
     "year_semester": "3학년/1학기", "note": "타전공인정 미래자동차혁신부품전공"},
    {"code": "IPF005", "name": "유분석기반기계상태진단", "credit": 3, "category": "전공선택",
     "year_semester": "3학년/1학기", "note": "타전공인정 미래자동차혁신부품전공"},
    # 3학년/2학기
    {"code": "AME061", "name": "3D모델링응용", "credit": 2, "category": "전공핵심", "year_semester": "3학년/2학기"},
    {"code": "AME055", "name": "자동차공학과제1(캡스톤디자인)", "credit": 2, "category": "전공핵심", "year_semester": "3학년/2학기"},
    {"code": "AME032", "name": "자동차생산공학", "credit": 3, "category": "전공선택", "year_semester": "3학년/2학기"},
    {"code": "AME039", "name": "자동차전산Dynamics해석", "credit": 3, "category": "전공선택", "year_semester": "3학년/2학기"},
    {"code": "AME043", "name": "자동차진동공학", "credit": 3, "category": "전공선택", "year_semester": "3학년/2학기"},
    {"code": "AME071", "name": "자율주행센서", "credit": 3, "category": "전공선택", "year_semester": "3학년/2학기"},
    {"code": "AME072", "name": "자율주행인공지능", "credit": 3, "category": "전공선택", "year_semester": "3학년/2학기"},
    {"code": "M00027", "name": "전공연계취업설계", "credit": 1, "category": "전공선택", "year_semester": "3학년/2학기"},
    {"code": "AME073", "name": "전기모터", "credit": 3, "category": "전공선택", "year_semester": "3학년/2학기"},
    {"code": "IPF011", "name": "미래모빌리티및에너지전환", "credit": 3, "category": "전공선택",
     "year_semester": "3학년/2학기", "note": "타전공인정 미래자동차혁신부품전공"},
    {"code": "IPF010", "name": "미래자동차부품요소설계", "credit": 3, "category": "전공선택",
     "year_semester": "3학년/2학기", "note": "타전공인정 미래자동차혁신부품전공"},
    # 4학년/1학기
    {"code": "AME056", "name": "자동차공학과제2(캡스톤디자인)", "credit": 2, "category": "전공핵심", "year_semester": "4학년/1학기"},
    {"code": "AME078", "name": "e-파워트레인설계", "credit": 2, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "AME063", "name": "기구학기초", "credit": 2, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "AME065", "name": "열역학기초", "credit": 2, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "AME066", "name": "이차전지및연료전지", "credit": 3, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "AME013", "name": "자동제어", "credit": 3, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "AME022", "name": "자동차NVH", "credit": 2, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "AME069", "name": "자동차전산성형해석", "credit": 2, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "AME070", "name": "자동차전산전자기장해석", "credit": 2, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "AME057", "name": "취업/현장기술세미나", "credit": 1, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "AME050", "name": "현장실습(자동차기계)", "credit": 3, "category": "전공선택", "year_semester": "4학년/1학기"},
    # 4학년/2학기
    {"code": "AME021", "name": "미래스마트자동차", "credit": 3, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "AME050", "name": "현장실습(자동차기계)", "credit": 3, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "CEM035", "name": "4차산업혁명과취업/현장기술세미나", "credit": 1, "category": "전공선택",
     "year_semester": "4학년/2학기", "note": "타전공인정 컴퓨터공학전공"},
    {"code": "SCM020", "name": "4차산업혁명과취업/현장기술세미나", "credit": 1, "category": "전공선택",
     "year_semester": "4학년/2학기", "note": "타전공인정 소프트웨어융합전공"},
    {"code": "INC044", "name": "4차산업혁명과취업/현장기술세미나", "credit": 1, "category": "전공선택",
     "year_semester": "4학년/2학기", "note": "타전공인정 정보통신공학전공"},
    {"code": "ELT171", "name": "4차산업혁명과취업/현장기술세미나", "credit": 1, "category": "전공선택",
     "year_semester": "4학년/2학기", "note": "타전공인정 전자공학과"},
]

컴퓨터공학전공 = [
    # 1학년/1학기 (컴퓨터학부 공통)
    {"code": "U00911", "name": "대학생활과전공설계", "credit": 1, "category": "교양필수", "year_semester": "1학년/1학기"},
    {"code": "U00786", "name": "소프트웨어와인공지능", "credit": 3, "category": "교양필수", "year_semester": "1학년/1학기"},
    {"code": "U00258", "name": "실용영어", "credit": 3, "category": "교양필수", "year_semester": "1학년/1학기"},
    {"code": "U00514", "name": "행렬및행렬식", "credit": 3, "category": "교양필수", "year_semester": "1학년/1학기"},
    # 1학년/2학기
    {"code": "U00801", "name": "C프로그래밍", "credit": 3, "category": "교양필수", "year_semester": "1학년/2학기"},
    {"code": "U00145", "name": "미분적분학(1)", "credit": 3, "category": "교양필수", "year_semester": "1학년/2학기"},
    {"code": "U00787", "name": "사회공헌과봉사", "credit": 1, "category": "교양필수", "year_semester": "1학년/2학기"},
    {"code": "U00469", "name": "통계학(1)", "credit": 3, "category": "교양필수", "year_semester": "1학년/2학기"},
    # 2학년/1학기
    {"code": "CEM001", "name": "논리회로", "credit": 3, "category": "전공핵심", "year_semester": "2학년/1학기"},
    {"code": "CEM002", "name": "논리회로실험", "credit": 1, "category": "전공핵심", "year_semester": "2학년/1학기"},
    {"code": "CEM050", "name": "오픈소스SW의이해", "credit": 2, "category": "전공핵심", "year_semester": "2학년/1학기"},
    {"code": "CEM017", "name": "프로그래밍언어", "credit": 3, "category": "전공핵심", "year_semester": "2학년/1학기"},
    {"code": "CEM026", "name": "공학입문설계", "credit": 2, "category": "전공선택", "year_semester": "2학년/1학기"},
    {"code": "CEM010", "name": "이산수학", "credit": 3, "category": "전공선택", "year_semester": "2학년/1학기"},
    # 2학년/2학기
    {"code": "CEM005", "name": "마이크로프로세서", "credit": 2, "category": "전공핵심", "year_semester": "2학년/2학기"},
    {"code": "CEM006", "name": "마이크로프로세서실습", "credit": 1, "category": "전공핵심", "year_semester": "2학년/2학기"},
    {"code": "CEM031", "name": "시스템프로그래밍및보안", "credit": 3, "category": "전공핵심", "year_semester": "2학년/2학기"},
    {"code": "CEM012", "name": "자료구조", "credit": 3, "category": "전공핵심", "year_semester": "2학년/2학기"},
    {"code": "CEM024", "name": "자바프로그래밍및실습", "credit": 3, "category": "전공핵심", "year_semester": "2학년/2학기"},
    {"code": "CEM004", "name": "데이터통신", "credit": 2, "category": "전공선택", "year_semester": "2학년/2학기"},
    {"code": "CEM034", "name": "자료구조실습", "credit": 1, "category": "전공선택", "year_semester": "2학년/2학기"},
    # 3학년/1학기
    {"code": "CEM036", "name": "IoT와임베디드소프트웨어", "credit": 3, "category": "전공핵심", "year_semester": "3학년/1학기"},
    {"code": "CEM008", "name": "알고리즘", "credit": 3, "category": "전공핵심", "year_semester": "3학년/1학기"},
    {"code": "CEM049", "name": "오픈소스SW설계", "credit": 2, "category": "전공핵심", "year_semester": "3학년/1학기"},
    {"code": "CEM009", "name": "운영체제", "credit": 3, "category": "전공핵심", "year_semester": "3학년/1학기"},
    {"code": "CEM016", "name": "컴퓨터네트워크", "credit": 3, "category": "전공핵심", "year_semester": "3학년/1학기"},
    {"code": "CEM052", "name": "AI서비스운영및개발실무", "credit": 3, "category": "전공선택", "year_semester": "3학년/1학기"},
    {"code": "CEM015", "name": "컴퓨터그래픽스", "credit": 3, "category": "전공선택", "year_semester": "3학년/1학기"},
    # 3학년/2학기
    {"code": "CEM003", "name": "데이터베이스", "credit": 3, "category": "전공핵심", "year_semester": "3학년/2학기"},
    {"code": "CEM007", "name": "소프트웨어공학", "credit": 3, "category": "전공핵심", "year_semester": "3학년/2학기"},
    {"code": "CEM014", "name": "컴퓨터구조", "credit": 3, "category": "전공핵심", "year_semester": "3학년/2학기"},
    {"code": "CEM053", "name": "AI서비스프로젝트", "credit": 3, "category": "전공선택", "year_semester": "3학년/2학기"},
    {"code": "CEM038", "name": "데이터분석과머신러닝", "credit": 3, "category": "전공선택", "year_semester": "3학년/2학기"},
    {"code": "CEM025", "name": "모바일프로그래밍", "credit": 3, "category": "전공선택", "year_semester": "3학년/2학기"},
    {"code": "T00056", "name": "상업정보교육론", "credit": 3, "category": "전공선택", "year_semester": "3학년/2학기", "note": "교과교육영역(교직)"},
    {"code": "CEM023", "name": "소프트웨어프로젝트", "credit": 2, "category": "전공선택", "year_semester": "3학년/2학기"},
    {"code": "CEM030", "name": "컴퓨터비전", "credit": 3, "category": "전공선택", "year_semester": "3학년/2학기", "note": "석사인정"},
    # 4학년/1학기
    {"code": "CEM046", "name": "MIDAS종합설계(1)", "credit": 2, "category": "전공핵심", "year_semester": "4학년/1학기"},
    {"code": "CEM040", "name": "IT디자인융합프로젝트(종합설계)", "credit": 2, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "CEM037", "name": "네트워크보안과블록체인", "credit": 2, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "CEM048", "name": "메타버스프로그래밍", "credit": 2, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "CEM032", "name": "산업체요구문제연구", "credit": 3, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "T00057", "name": "상업정보교재연구및지도법", "credit": 3, "category": "전공선택", "year_semester": "4학년/1학기", "note": "교과교육영역(교직)"},
    {"code": "CEM020", "name": "웹프로그래밍", "credit": 2, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "CEM011", "name": "인공지능", "credit": 3, "category": "전공선택", "year_semester": "4학년/1학기", "note": "석사인정"},
    {"code": "CEM019", "name": "현장실습(컴퓨터)", "credit": 3, "category": "전공선택", "year_semester": "4학년/1학기"},
    {"code": "AME057", "name": "취업/현장기술세미나", "credit": 1, "category": "전공선택",
     "year_semester": "4학년/1학기", "note": "타전공인정 미래자동차공학과"},
    {"code": "RME063", "name": "취업/현장기술세미나", "credit": 1, "category": "전공선택",
     "year_semester": "4학년/1학기", "note": "타전공인정 로봇공학과"},
    # 4학년/2학기
    {"code": "CEM035", "name": "4차산업혁명과취업/현장기술세미나", "credit": 1, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "CEM045", "name": "AI게임프로그래밍", "credit": 2, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "CEM051", "name": "AI기반빅데이터분석을통한지역재생정보활용", "credit": 3, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "CEM029", "name": "ICT기술과지식재산권", "credit": 2, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "CEM047", "name": "MIDAS종합설계(2)", "credit": 2, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "CEM042", "name": "데이터베이스응용", "credit": 3, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "CEM018", "name": "멀티미디어시스템", "credit": 2, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "CEM043", "name": "빅데이터분석및응용", "credit": 3, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "CEM033", "name": "산학과제공동연구", "credit": 2, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "CEM044", "name": "산학연계PBL", "credit": 3, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "T00157", "name": "상업정보논리및논술", "credit": 2, "category": "전공선택", "year_semester": "4학년/2학기", "note": "교과교육영역(교직)"},
    {"code": "CEM041", "name": "웹프레임워크", "credit": 2, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "CEM027", "name": "임베디드운영체제", "credit": 3, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "CEM013", "name": "컴파일러", "credit": 2, "category": "전공선택", "year_semester": "4학년/2학기"},
    {"code": "CEM039", "name": "클라우드컴퓨팅", "credit": 2, "category": "전공선택", "year_semester": "4학년/2학기", "note": "석사인정"},
]

CURRICULUM: dict[str, list[dict]] = {
    "미래자동차공학과": 미래자동차공학과,
    "컴퓨터공학전공": 컴퓨터공학전공,
}

# 학생이 자연어로 부를 법한 이름 -> 정식 학과명
MAJOR_ALIASES = {
    "미래자동차공학과": "미래자동차공학과",
    "미래자동차공학": "미래자동차공학과",
    "미래자동차": "미래자동차공학과",
    "미래차": "미래자동차공학과",
    "컴퓨터공학전공": "컴퓨터공학전공",
    "컴퓨터공학과": "컴퓨터공학전공",
    "컴퓨터공학": "컴퓨터공학전공",
    "컴공": "컴퓨터공학전공",
    "컴퓨터학부": "컴퓨터공학전공",
    "cs": "컴퓨터공학전공",
}

# 이 데모에서 실제로 지원하는 학과 조합 (그 외 조합은 "아직 준비 안 됨"으로 안내)
SUPPORTED_PAIRS = {("미래자동차공학과", "컴퓨터공학전공")}


def resolve_major(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    key = name.strip().replace(" ", "").lower()
    for alias, official in MAJOR_ALIASES.items():
        if alias.replace(" ", "").lower() == key:
            return official
    return None


def is_supported_pair(home_major: Optional[str], target_major: Optional[str]) -> bool:
    h = resolve_major(home_major)
    t = resolve_major(target_major)
    return (h, t) in SUPPORTED_PAIRS


def _normalize(name: str) -> str:
    return name.strip().replace(" ", "").lower()


def get_major_courses(major: str, categories: tuple[str, ...] = ("전공핵심", "전공선택", "전공필수")) -> list[dict]:
    official = resolve_major(major) or major
    rows = CURRICULUM.get(official, [])
    return [r for r in rows if r["category"] in categories]


def _target_curriculum_rows(major: str) -> list[dict]:
    """이수과목 인정 대조 전용 — get_major_courses()처럼 전공(전공핵심/전공선택/전공필수)
    카테고리로 좁히지 않고, 목표 학과 교육과정표에 편성된 과목을 전부(교양필수 포함) 대상으로
    한다. 실사용 버그로 확인됨: C프로그래밍/소프트웨어와인공지능처럼 학과가 필수교양으로
    지정해둔 과목도 커리큘럼표에는 분명히 올라있는데, get_major_courses()로 좁히면 이런
    과목들이 애초에 대조 후보에서 빠져서 학생이 정확히 같은 과목명을 말해도 전부 unmatched로
    빠지는 문제가 있었음 — 이수과목 인정은 "전공 과목"만이 아니라 교육과정표에 편성된 과목
    전체가 대상이라 카테고리로 거를 이유가 없음."""
    official = resolve_major(major) or major
    return CURRICULUM.get(official, [])


def find_potential_overlap(
    home_major: Optional[str], target_major: Optional[str], already_mentioned: Optional[list[str]] = None
) -> list[dict]:
    """홈학과·목표학과 교육과정표에 둘 다 편성돼 있는데 학생이 아직 말 안 한 과목을 찾는다
    (실사용자 요청: "학생이 만약에 들었는데 까먹고 이야기 안 했을 수도 있으니까, 비교해서
    겹치는 과목 이미 들은거같으면 미리 물어보고"). 교양필수처럼 두 학과 다 필수로 지정한
    과목은 그 학과 학생이면 사실상 다 이수했을 확률이 높은데, "이수과목 인정용으로 말해야
    하는 과목"이라고는 잘 생각 못 해서 깜빡 빠뜨리기 쉬움 — 그래서 카테고리로 거르지 않고
    _target_curriculum_rows()로 전부 대상에 넣는다(교양필수 매칭 버그를 고칠 때와 같은 이유).

    이것도 최종 판단이 아니라 "물어볼 후보 뽑기"일 뿐 — 실제로 그 과목을 들었는지는 반드시
    학생 본인 확인을 받고 나서만 completed_courses에 추가한다(여기서 자동으로 이수 처리하지
    않음). 매칭 기준은 match_completed_courses와 동일하게 학수번호 우선, 없으면 과목명
    완전일치.
    """
    if not home_major or not target_major:
        return []
    already_norm = {_normalize(n) for n in (already_mentioned or [])}
    home_rows = _target_curriculum_rows(home_major)
    target_rows = _target_curriculum_rows(target_major)
    target_by_code = {r["code"].upper(): r for r in target_rows}
    target_by_name = {_normalize(r["name"]): r for r in target_rows}

    overlap: list[dict] = []
    seen_codes: set[str] = set()
    for r in home_rows:
        if _normalize(r["name"]) in already_norm:
            continue
        match = target_by_code.get(r["code"].upper()) or target_by_name.get(_normalize(r["name"]))
        if not match:
            continue
        code = match["code"].upper()
        if code in seen_codes:
            continue
        seen_codes.add(code)
        overlap.append(
            {
                "code": match["code"],
                "name": match["name"],
                "credit": match["credit"],
                "year_semester": r.get("year_semester"),
            }
        )
    return overlap


def match_completed_courses(
    target_major: str, completed: list[CompletedCourseItem]
) -> tuple[list[MatchedCourse], list[str]]:
    """학생이 이미 이수했다고 말한 과목들을 목표 복수전공 교육과정표(편성된 과목 전체 —
    전공 카테고리로 한정하지 않음, 위 _target_curriculum_rows 참고)와 대조한다.
    매칭 기준: 학수번호 완전일치가 최우선, 학수번호가 없으면 과목명 완전일치.
    유사하지만 완전히 같지는 않은 과목명은 절대 자동 매칭하지 않음 — 그런 애매한
    케이스야말로 사람이 판단해야 할 몫이라 unmatched로 남겨서 직원 확인을 받게 한다.
    """
    target_rows = _target_curriculum_rows(target_major)
    by_code = {r["code"].upper(): r for r in target_rows}
    by_name = {_normalize(r["name"]): r for r in target_rows}

    matched: list[MatchedCourse] = []
    unmatched: list[str] = []

    for item in completed:
        row = None
        if item.course_code:
            row = by_code.get(item.course_code.strip().upper())
        if row is None:
            row = by_name.get(_normalize(item.course_name))
        if row:
            matched.append(
                MatchedCourse(
                    course_code=row["code"],
                    course_name=row["name"],
                    credit=row["credit"],
                    year_semester=row.get("year_semester"),
                    matched_note=row.get("note"),
                )
            )
        else:
            unmatched.append(item.course_name)

    return matched, unmatched
