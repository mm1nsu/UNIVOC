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
"""
import json
import urllib.error
import urllib.request

import config

RESEND_API_URL = "https://api.resend.com/emails"
_TIMEOUT_SECONDS = 10


def send_email(to_email: str | None, subject: str, text_body: str) -> bool:
    """Resend API로 이메일 한 통 발송하고 성공 여부(bool)를 반환한다.

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
    }
    req = urllib.request.Request(
        RESEND_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
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
