"""
'부(복수)전공 이수과목 인정신청서' 공식 양식(영남대 수업학적팀 배포본, .hwp -> .docx 변환)에
맞춰 RecognitionApplication을 채워넣어 실제 다운로드 가능한 .docx로 만들어주는 모듈.

주의(설계 원칙 동일 적용):
- 이 과목이 인정되는지 여부는 여기서 판단하지 않음. curriculum.match_completed_courses()가
  이미 결정론적으로 판단해둔 matched_courses만 표에 채워넣는다(불인정 과목/unmatched는
  이 공식 서류에는 올리지 않고, 참고용 각주로만 남긴다).
- 성명/학번/학년/소속 단과대학은 학생이 제출 직전 student_info 단계에서 직접 말하고,
  student_info_confirm 단계에서 한 번 더 확인(맞으면 제출, 틀리면 다시입력)받은 값만
  채운다(app.py — 지어내지 않음). "전공"(세부전공) 칸처럼 대부분의 학과엔 해당 사항이
  없는 항목만 빈칸으로 남긴다.
- "그냥 표만 채워주는 서류는 직원한테 올려도 의미가 없다, 전산화한다는데 의의가 있어야지"
  라는 실사용자 피드백 이후로, 종이 서명(직인) 대신 "언제 누가 전산으로 제출/승인했는지"를
  문서 하단에 감사기록(audit trail)으로 남겨서 이 문서 자체가 디지털 승인 기록으로도
  의미를 갖게 함(위조된 서명을 흉내내는 게 아니라, 실제로 시스템에 남은 사실을 문서에
  그대로 적어주는 것 — RecognitionApplication.status/decided_by/decided_at은 /staff에서
  사람이 실제로 승인/반려한 기록이라 이 부분은 지어내는 게 아니라 사실을 옮기는 것뿐).
"""
import io
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from docx import Document
from docx.enum.table import WD_ROW_HEIGHT_RULE

from schemas import RecognitionApplication

TEMPLATE_PATH = Path(__file__).parent / "templates" / "이수과목인정신청서_양식.docx"

KST = timezone(timedelta(hours=9))


def _find_para(doc, marker: str):
    for p in doc.paragraphs:
        if marker in p.text:
            return p
    return None


def _find_run_index(paragraph, marker: str):
    """paragraph.runs는 호출할 때마다 새 Run 래퍼 객체를 만들어서 반환하기 때문에
    (한 번 꺼낸 run 객체로 나중에 .index()를 하면 ValueError가 남), 인덱스만 찾아서
    돌려주고 호출부에서 같은 runs 리스트 하나를 계속 재사용하게 한다."""
    runs = paragraph.runs
    for i, r in enumerate(runs):
        if marker in r.text:
            return i
    return None


def _fill_home_dept(doc, home_major: str | None, student_college: str | None):
    """소속 라인 "소 속 : ___대학 ___학부(과) ___전공" 채우기 — "대학"(단과대학) 칸엔
    student_college(학생이 직접 말한 값), "학부(과)" 칸엔 home_major(앞선 복수전공
    자격판정 단계에서 이미 확보한 학과명)를 채운다. "전공" 칸은 세부전공이 없는 학과가
    대부분이라 비워둔다."""
    p = _find_para(doc, "소  속")
    if not p:
        return
    if student_college:
        idx = _find_run_index(p, "소  속")
        if idx is not None and idx + 1 < len(p.runs):
            p.runs[idx + 1].text = ": " + student_college + "  "
    if home_major:
        idx = _find_run_index(p, "학부")
        if idx is not None:
            r = p.runs[idx]
            r.text = re.sub(r"대\s*학\s+학부", f"대 학   {home_major}   학부", r.text, count=1)


def _fill_student_info(
    doc, student_name: str | None, student_id: str | None, student_grade: str | None
):
    if not student_name and not student_id and not student_grade:
        return
    p = _find_para(doc, "성  명")
    if not p:
        return
    if student_name:
        idx = _find_run_index(p, "성  명")
        if idx is not None and idx + 1 < len(p.runs):
            p.runs[idx + 1].text = ": " + student_name + "   "
    if student_id:
        idx = _find_run_index(p, "학 번")
        if idx is not None and idx + 1 < len(p.runs):
            p.runs[idx + 1].text = ": " + student_id + "   "
    if student_grade:
        idx = _find_run_index(p, "학 년")
        if idx is not None and idx + 1 < len(p.runs):
            p.runs[idx + 1].text = ": " + student_grade + "   "


def _mark_recognition_type(doc):
    """부/1복수/2복수 중 어떤 유형인지 — 현재 시스템은 SUPPORTED_PAIRS가 딱 1개
    조합만 지원하므로(미래자동차공학과 -> 컴퓨터공학전공), '1복수'에 표시를 남긴다.
    조합이 늘어나면 이 함수에 인자를 추가해서 분기하면 됨."""
    p = _find_para(doc, "기 이수한 과목을")
    if not p:
        return
    idx = _find_run_index(p, ", 1")
    if idx is None or idx + 1 >= len(p.runs):
        return
    r1, r2 = p.runs[idx], p.runs[idx + 1]
    r1.text = r1.text.replace(", 1", ", 【1")
    r2.text = r2.text + "】"
    r1.bold = True
    r2.bold = True


def _fill_date(doc, created_at_iso: str):
    p = _find_para(doc, "년       월        일")
    if not p or len(p.runs) < 2:
        return
    try:
        dt = datetime.fromisoformat(created_at_iso).astimezone(KST)
    except Exception:
        dt = datetime.now(KST)
    runs = p.runs
    year_run, rest_run = runs[0], runs[1]
    year_run.text = year_run.text.rstrip() + f"  {dt.year}"
    text = rest_run.text
    text = re.sub(r"년\s+월", f"년  {dt.month}월", text, count=1)
    text = re.sub(r"월\s+일", f"월  {dt.day}일", text, count=1)
    rest_run.text = text


def _fill_course_table(doc, app: RecognitionApplication):
    table = doc.tables[0]
    data_rows = table.rows[2:]  # 앞 2행은 헤더(번호/전공명/과목명/이수학기(연도,학기)/비고)
    for i, course in enumerate(app.matched_courses):
        if i < len(data_rows):
            row = data_rows[i]
        else:
            row = table.add_row()
        # 원본 양식은 행높이가 EXACTLY(고정)라 비고란처럼 긴 텍스트가 들어가면 잘려 보임 —
        # 실제로 채워 넣는 행만 AUTO(내용에 맞춰 자동 확장)로 바꿔서 잘림 방지.
        row.height_rule = WD_ROW_HEIGHT_RULE.AUTO
        cells = row.cells
        cells[0].text = str(i + 1)
        cells[1].text = app.target_major
        cells[2].text = course.course_name
        cells[3].text = ""  # 실제 이수 연도 — 챗봇이 수집하지 않는 정보라 빈칸(직접 기재)
        cells[4].text = ""  # 실제 이수 학기 — 위와 동일
        note = course.matched_note or "교육과정표 동일과목"
        if course.credit:
            credit_str = str(int(course.credit)) if course.credit == int(course.credit) else str(course.credit)
            note = f"{note} ({credit_str}학점)"
        cells[5].text = note


def _fmt_kst(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).astimezone(KST).strftime("%Y.%m.%d %H:%M")
    except Exception:
        return iso


def _add_footer_notes(doc, app: RecognitionApplication):
    doc.add_paragraph()
    p = doc.add_paragraph(
        "※ 본 신청서는 Uni-VOC 챗봇이 학생 확인을 거쳐 자동 작성했습니다. "
        "세부전공 등 별도로 확인하지 않은 항목이 있다면 제출 전 다시 확인해주세요."
    )
    p.runs[0].italic = True
    if app.unmatched_course_names:
        names = ", ".join(app.unmatched_course_names)
        p2 = doc.add_paragraph(
            f"※ 학생이 언급했지만 교육과정표와 자동 매칭되지 않아 본 신청서에는 포함하지 않은 과목"
            f"(직원 확인 필요 시 참고): {names}"
        )
        p2.runs[0].italic = True

    # --- 전산 처리 확인(감사기록) ---
    # "표만 채워주는 용도면 의미없다, 전산화 의의가 있어야지"라는 피드백에 대한 핵심 대응.
    # 직인/서명 칸은 실물 결재가 필요하면 그대로 쓰면 되고, 그와 별개로 이 시스템 안에서
    # 실제로 벌어진 일(누가 언제 제출/승인/반려했는지)을 문서에 그대로 남겨서, 이 파일 자체가
    # "전산 처리된 신청 기록"으로서 의미를 갖게 한다 — 지어내는 정보가 아니라 RecognitionApplication에
    # 실제로 저장된 사실(제출 시각, 학과장/복수전공학과장/행정처 각 단계의 승인·반려 처리자/시각)을
    # 그대로 옮기는 것뿐. 실제 서명 이미지를 위조하지 않고 텍스트 기록으로만 남기는 이유도 동일함
    # (지어낸 서명은 오히려 문서 위조에 가까움 — 실제로 시스템에 있었던 사실만 적는다).
    doc.add_paragraph()
    doc.add_paragraph().add_run("전산 처리 기록").bold = True
    if app.student_name:
        submit_note = doc.add_paragraph(
            f"· 신청 제출: {app.student_name}"
            + (f"({app.student_id})" if app.student_id else "")
            + f" 본인이 Uni-VOC 챗봇을 통해 {_fmt_kst(app.created_at)} 전산 제출 확인함."
        )
        submit_note.runs[0].italic = True

    # 3단계 승인(학과장/복수전공학과장/행정처) 기록 — 셋 중 하나라도 실제로 처리된 적이
    # 있으면 신형 기록이라고 보고 단계별로 풀어서 적는다. 셋 다 pending인데 레거시
    # status(app.decided_by 등 구버전 1단계 필드)만 최종 처리돼 있으면, 그건 3단계 승인
    # 기능이 생기기 전에 이미 승인/반려된 옛날 신청서라는 뜻이라 예전 방식 그대로 보여준다
    # (없던 단계별 기록을 지어내지 않기 위함).
    stages = [
        ("학과장", app.home_chair_approval),
        ("복수전공학과장", app.dual_chair_approval),
        ("행정처", app.admin_approval),
    ]
    has_staged_record = any(step.status != "pending" for _, step in stages)

    if has_staged_record:
        for label, step in stages:
            if step.status == "approved":
                p = doc.add_paragraph(
                    f"· {label} 승인: {step.decided_by or '담당자'}이(가) {_fmt_kst(step.decided_at)} "
                    f"Uni-VOC 승인 화면에서 전산 승인함."
                    + (f" (메모: {step.note})" if step.note else "")
                )
            elif step.status == "rejected":
                p = doc.add_paragraph(
                    f"· {label} 반려: {step.decided_by or '담당자'}이(가) {_fmt_kst(step.decided_at)} "
                    f"Uni-VOC 승인 화면에서 반려함."
                    + (f" (사유: {step.note})" if step.note else "")
                )
            else:
                p = doc.add_paragraph(f"· {label} 승인 대기 중.")
            p.runs[0].italic = True
        if app.status == "approved":
            done_note = doc.add_paragraph("· 학과장·복수전공학과장·행정처 승인이 전부 완료돼 전산 반영됨.")
            done_note.runs[0].italic = True
            done_note.runs[0].bold = True
    elif app.status == "approved":
        decide_note = doc.add_paragraph(
            f"· 승인 처리: {app.decided_by or '담당 직원'}이(가) {_fmt_kst(app.decided_at)} "
            f"Uni-VOC 승인 화면에서 전산 승인함."
            + (f" (메모: {app.decision_note})" if app.decision_note else "")
        )
        decide_note.runs[0].italic = True
    elif app.status == "rejected":
        decide_note = doc.add_paragraph(
            f"· 반려 처리: {app.decided_by or '담당 직원'}이(가) {_fmt_kst(app.decided_at)} "
            f"Uni-VOC 승인 화면에서 반려함."
            + (f" (사유: {app.decision_note})" if app.decision_note else "")
        )
        decide_note.runs[0].italic = True
    else:
        pending_note = doc.add_paragraph("· 현재 학과장·복수전공학과장·행정처 승인 대기 중(status: pending).")
        pending_note.runs[0].italic = True


def build_recognition_docx(app: RecognitionApplication) -> bytes:
    """공식 양식에 맞춰 채운 .docx를 bytes로 반환."""
    doc = Document(str(TEMPLATE_PATH))
    _fill_home_dept(doc, app.home_major, app.student_college)
    _fill_student_info(doc, app.student_name, app.student_id, app.student_grade)
    _mark_recognition_type(doc)
    _fill_date(doc, app.created_at)
    _fill_course_table(doc, app)
    _add_footer_notes(doc, app)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
