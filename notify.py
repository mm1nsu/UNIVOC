"""
이메일 알림 발송 — Resend(https://resend.com) HTTPS API 사용.

실사용자 요청: "반려되는 순간 바로 알림 받고 싶다" + "컴퓨터 꺼도 알림 받을 수 있잖아".
로그인만 만들면(누가 채팅하는지 "확인"은 되지만) 컴퓨터/브라우저가 꺼져있으면 어차피
아무것도 못 받는다 — 그래서 채팅과 완전히 독립된 외부 채널(이메일)이 필요함. Resend를
고른 이유: (1) API 키 하나만 발급받으면 끝(도메인 인증 없이도 onboarding@resend.dev로
바로 발송 가능해서 데모용으로 가장 빠름), (2) HTTPS 기반이라 SMTP 포트(587)를 막아둔
호스팅 환경에서도 안 막힘.

app.py의 decide_recognition_application()에서, 반려/승인이 그 자리에서 확정되는 바로
그 순간에 이 함수를 호출한다 — 학생이 챗봇을 다시 열 필요 없이(반려 알림 자동체크는
챗봇을 다시 열어야만 발동했던 기존 한계 — app.py의 "(0)" 주석 참고) 이메일이 즉시 나간다.

이 파일은 requests 같은 추가 패키지를 새로 안 늘리려고 표준 라이브러리(urllib)만 쓴다 —
이 샌드박스가 PyPI 접근이 막혀있어 새 의존성을 설치 테스트할 수 없기도 하고, Render
배포 쪽에도 새 패키지 설치 실패 리스크를 하나 줄이는 효과가 있다.

실사용자 피드백: "메일이 너무 밋밋하게(맨 텍스트로) 온다" — Resend는 text/html을 같이
보낼 수 있어서, text_body는 그대로 두고(스팸필터·텍스트전용 클라이언트 대비 폴백) html도
같이 만들어서 보낸다. 카드형 레이아웃 + 영남대 공식 컬러(YU블루 #153974)로 staff.html과
톤을 맞췄고, 반려/승인 여부(status)에 따라 포인트색만 빨강/초록으로 바뀐다.
"""
import html as html_lib
import json
import urllib.error
import urllib.request

import config

RESEND_API_URL = "https://api.resend.com/emails"
_TIMEOUT_SECONDS = 10

# status별로 강조색·연한 배경(pill)·아이콘 글자·한글 라벨을 한 번에 묶어둠 — staff.html의
# .status-badge/.stage-chip과 같은 톤(연한 배경 + 진한 글자)을 이메일에도 그대로 재현.
_STATUS_META = {
    "rejected": {"accent": "#c0392b", "tint": "#fdf0ee", "glyph": "&#10005;", "label": "반려"},
    "approved": {"accent": "#0f7a52", "tint": "#eaf7f1", "glyph": "&#10003;", "label": "승인"},
}
_DEFAULT_META = {"accent": "#153974", "tint": "#e6eaf3", "glyph": "&#128276;", "label": "알림"}

# 헤더 위/카드 아래에 까는 얇은 그라데이션 띠 — staff.html 헤더 하단의 브랜드 스트라이프와
# 동일한 4색(YU블루 진한색 -> YU블루 -> YU스카이블루 -> YU베이지)을 그대로 씀.
_BRAND_STRIPE = "linear-gradient(90deg, #0e2850, #153974, #00aaca, #eae3d2)"


def _render_html_body(subject: str, text_body: str, status: str) -> str:
    """text_body(줄바꿈 두 번으로 문단 구분된 평문)를 카드형 HTML 이메일로 감싼다.
    이메일 클라이언트 호환성 때문에 flex/grid 대신 table+인라인 스타일만 씀(네이버메일·
    지메일 등에서도 안 깨지게). 그라데이션/box-shadow는 지원 안 하는 클라이언트에선 그냥
    무시되고 solid 색만 남는 정도라 안전하게 곁들임."""
    meta = _STATUS_META.get(status, _DEFAULT_META)
    accent, tint, glyph, label = meta["accent"], meta["tint"], meta["glyph"], meta["label"]

    paragraphs = "".join(
        f'<p style="margin:0 0 14px 0;font-size:14.5px;line-height:1.75;color:#1c2530;">'
        f'{html_lib.escape(p).replace(chr(10), "<br/>")}</p>'
        for p in text_body.split("\n\n")
        if p.strip()
    )
    safe_subject = html_lib.escape(subject)
    return f"""<!doctype html>
<html lang="ko">
<body style="margin:0;padding:32px 12px;background:#f2f2f1;font-family:'Apple SD Gothic Neo','Malgun Gothic','Noto Sans KR',sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center">
      <table role="presentation" width="480" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:16px;overflow:hidden;max-width:480px;width:100%;
                    box-shadow:0 8px 28px rgba(21,57,116,.14);">
        <tr><td style="height:5px;line-height:5px;font-size:0;background:{_BRAND_STRIPE};">&nbsp;</td></tr>
        <tr>
          <td style="background:#153974;background:linear-gradient(120deg,#0e2850,#153974 65%);padding:22px 28px 20px;">
            <div style="color:#ffffff;font-size:14px;font-weight:800;letter-spacing:.4px;">UNI-VOC</div>
            <div style="color:rgba(255,255,255,.78);font-size:12px;margin-top:3px;">영남대학교 부(복수)전공 이수과목 인정신청</div>
          </td>
        </tr>
        <tr>
          <td style="padding:28px 28px 6px;">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td width="44" valign="top">
                  <table role="presentation" width="40" height="40" cellpadding="0" cellspacing="0"
                         style="background:{accent};border-radius:50%;">
                    <tr><td align="center" valign="middle"
                        style="color:#ffffff;font-size:18px;line-height:40px;height:40px;">{glyph}</td></tr>
                  </table>
                </td>
                <td valign="middle" style="padding-left:12px;">
                  <span style="display:inline-block;background:{tint};color:{accent};font-size:12px;
                        font-weight:700;border-radius:20px;padding:4px 12px;">{label}</span>
                  <div style="font-size:16.5px;font-weight:700;color:#1c2530;margin-top:8px;line-height:1.4;">{safe_subject}</div>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:18px 28px 22px;">
            {paragraphs}
          </td>
        </tr>
        <tr><td style="height:1px;line-height:1px;font-size:0;background:#e7e5df;margin:0 28px;">&nbsp;</td></tr>
        <tr>
          <td style="padding:16px 28px 24px;">
            <div style="font-size:12px;color:#9d9d9c;">이 메일은 Uni-VOC 챗봇에서 자동으로 발송됐어요.</div>
            <div style="font-size:12px;color:#9d9d9c;margin-top:2px;">문의사항은 학교 수업학적팀(053-810-1095)으로 연락해줘.</div>
          </td>
        </tr>
        <tr><td style="height:5px;line-height:5px;font-size:0;background:{_BRAND_STRIPE};">&nbsp;</td></tr>
      </table>
      <div style="font-size:11px;color:#9d9d9c;margin-top:14px;">Uni-VOC · 영남대학교</div>
    </td></tr>
  </table>
</body>
</html>"""


def send_email(to_email: str | None, subject: str, text_body: str, status: str = "info") -> bool:
    """Resend API로 이메일 한 통 발송하고 성공 여부(bool)를 반환한다.

    status는 "rejected"/"approved"만 특별 취급(HTML 카드의 포인트색을 빨강/초록으로
    바꿈)하고, 그 외 값은 전부 기본(YU블루)으로 렌더링한다 — 잘못된 값이 와도 절대
    에러 안 나게.

    실패해도(API 키 미설정, 이메일 주소 없음, 네트워크 오류, Resend 쪽 오류 등) 예외를
    위로 던지지 않는다 — 알림 발송은 부가기능이지 핵심 업무 흐름(반려/승인 처리 자체)이
    아니라서, 여기서 뭔가 실패했다고 신청서 처리까지 실패하면 안 된다. 대신 서버 로그에
    이유를 남겨서(터미널에서 바로 확인 가능) "왜 알림이 안 갔지"를 진단할 수 있게 한다."""
    if not to_email:
        print("[notify] 이메일 주소가 없어서 발송 생략")
        return False

    api_key = config.get_resend_api_key()
    if not api_key:
        print("[notify] RESEND_API_KEY가 설정 안 돼서 발송 생략 (Render 환경변수 확인 필요)")
        return False

    payload = {
        "from": config.get_notify_from_email(),
        "to": [to_email],
        "subject": subject,
        "text": text_body,
        "html": _render_html_body(subject, text_body, status),
    }
    req = urllib.request.Request(
        RESEND_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # urllib 기본 User-Agent("Python-urllib/3.x")를 Resend 앞단 Cloudflare가 봇으로
            # 오인해서 403(에러코드 1010, 브라우저 정합성 검사 실패)으로 막는 경우가 있어서,
            # 일반적인 HTTP 클라이언트처럼 보이는 User-Agent를 직접 지정해줌.
            "User-Agent": "Uni-VOC-Server/1.0 (+https://univoc.onrender.com)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                print(f"[notify] Resend 응답 이상: status={resp.status}")
            else:
                print(f"[notify] 이메일 발송 성공 -> {to_email}")
            return ok
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[notify] Resend HTTPError {e.code}: {body}")
        return False
    except Exception as e:  # noqa: BLE001 — 네트워크 오류 등, 발송 실패는 조용히 로그만
        print(f"[notify] 이메일 발송 실패: {e!r}")
        return False
