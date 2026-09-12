"""
로그인한 학생이 대화 탭을 껐다 켜거나 서버가 재시작돼도 "다시 채팅칠 수 있게" 하려면,
화면에 보이는 메시지뿐 아니라 진행 중이던 세션 상태(app.py의 UNIFIED_SESSIONS 항목 하나) 그
자체를 JSON으로 저장했다가 그대로 복원할 수 있어야 한다. 이 상태에는 SlotState/Scholarship/
DualMajorState 같은 pydantic 모델이 섞여 있어서 그냥 json.dumps가 안 되므로, 저장 직전에
전부 일반 dict/list로 바꾸고(serialize_session) 복원할 때 다시 모델로 되돌린다
(deserialize_session). 여기서는 어떤 판단도 하지 않음 — 순수한 직렬화/역직렬화 유틸이고,
이 프로젝트의 핵심 설계원칙(장학금 매칭/자격판정/과목인정은 전부 결정론적 코드가 판정)과는
무관함.
"""
from schemas import (
    CompletedCourseItem,
    DualMajorState,
    EligibilityResult,
    MatchedCourse,
    Scholarship,
    SlotState,
)


def serialize_session(sess: dict) -> dict:
    scholarship = sess["scholarship"]
    dual_major = sess["dual_major"]
    return {
        "mode": sess["mode"],
        "history": list(sess["history"]),
        "scholarship": {
            "state": scholarship["state"].model_dump(),
            "stage": scholarship["stage"],
            "matches": [
                {"scholarship": s.model_dump(), "needs_income_check": needs}
                for s, needs in scholarship["matches"]
            ],
            "selected": scholarship["selected"].model_dump() if scholarship["selected"] else None,
            "needs_income_check": scholarship["needs_income_check"],
            "pending_conditions": list(scholarship["pending_conditions"]),
        },
        "dual_major": {
            "state": dual_major["state"].model_dump(),
            "stage": dual_major["stage"],
            "result": dual_major["result"].model_dump() if dual_major["result"] else None,
            "completed_courses": [c.model_dump() for c in dual_major["completed_courses"]],
            "matched": [m.model_dump() for m in dual_major["matched"]],
            "unmatched": list(dual_major["unmatched"]),
            "overlap_candidates": list(dual_major["overlap_candidates"]),
            "last_application_id": dual_major["last_application_id"],
            "student_identity": dict(dual_major["student_identity"]),
        },
    }


def deserialize_session(data: dict) -> dict:
    scholarship_raw = data.get("scholarship") or {}
    dual_major_raw = data.get("dual_major") or {}
    return {
        "mode": data.get("mode"),
        "history": list(data.get("history") or []),
        "scholarship": {
            "state": SlotState.model_validate(scholarship_raw.get("state") or {}),
            "stage": scholarship_raw.get("stage") or "slot_filling",
            "matches": [
                (Scholarship.model_validate(m["scholarship"]), bool(m.get("needs_income_check")))
                for m in (scholarship_raw.get("matches") or [])
            ],
            "selected": (
                Scholarship.model_validate(scholarship_raw["selected"])
                if scholarship_raw.get("selected")
                else None
            ),
            "needs_income_check": bool(scholarship_raw.get("needs_income_check", False)),
            "pending_conditions": list(scholarship_raw.get("pending_conditions") or []),
        },
        "dual_major": {
            "state": DualMajorState.model_validate(dual_major_raw.get("state") or {}),
            "stage": dual_major_raw.get("stage") or "slot_filling",
            "result": (
                EligibilityResult.model_validate(dual_major_raw["result"])
                if dual_major_raw.get("result")
                else None
            ),
            "completed_courses": [
                CompletedCourseItem.model_validate(c)
                for c in (dual_major_raw.get("completed_courses") or [])
            ],
            "matched": [
                MatchedCourse.model_validate(m) for m in (dual_major_raw.get("matched") or [])
            ],
            "unmatched": list(dual_major_raw.get("unmatched") or []),
            "overlap_candidates": list(dual_major_raw.get("overlap_candidates") or []),
            "last_application_id": dual_major_raw.get("last_application_id"),
            "student_identity": dual_major_raw.get("student_identity")
            or {"name": None, "student_id": None, "grade": None, "college": None},
        },
    }
