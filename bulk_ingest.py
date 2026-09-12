"""
scholar_rag_database/ 폴더에 크롤링해둔 장학금 공지(.md) 전부를 한 번에
Gemini로 구조화해서 data/scholarship_db.json에 채워넣는 스크립트.

관리자 페이지(/admin)에서 파일 하나씩 "AI로 분석하기 → 저장"을 72번 반복하는 게
너무 번거로워서 만든 배치용 CLI. 로직 자체는 admin 페이지가 쓰는 것과 완전히 같은
bot_core.extract_scholarship() 함수를 그대로 재사용함 — 별도 로직 아님.

사용법:
    python3 bulk_ingest.py                          # 기본 폴더(scholar_rag_database/) 전체 처리
    python3 bulk_ingest.py 다른/폴더/경로               # 다른 폴더 지정

주의: 이 스크립트는 반드시 로컬(내 컴퓨터)에서 실행해야 함 — Gemini API 호출이
필요해서 네트워크 제한이 있는 환경(클라우드 샌드박스 등)에서는 안 됨.

이름이 같은 공지가 이미 DB에 있으면: 새로 파싱한 내용이 기존 것과 완전히 같으면
그냥 건너뛰고, 조금이라도 달라졌으면(금액/마감일/조건 등이 바뀐 재크롤링) 그
자리에서 최신 내용으로 갱신함 — id는 그대로 유지. (예전엔 이름이 같으면 무조건
건너뛰기만 해서, 크롤링을 다시 해와도 마감일/금액이 바뀐 걸 못 받아들였음 —
실사용자 요청: "장학금 정보 업데이트 해온 거니까 이걸로 반영해줘".)
한 건 성공할 때마다 바로 저장하니, 중간에 실패해도 그동안 처리된 건 안 날아감.
"""
import json
import sys
import time
import uuid
from pathlib import Path

import bot_core
import config
from schemas import Scholarship

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "scholarship_db.json"
DEFAULT_SOURCE_DIR = BASE_DIR / "scholar_rag_database"


def load_db() -> list[Scholarship]:
    if not DB_PATH.exists():
        return []
    raw = json.loads(DB_PATH.read_text(encoding="utf-8"))
    return [Scholarship.model_validate(r) for r in raw]


def save_db(db: list[Scholarship]) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.write_text(
        json.dumps([s.model_dump() for s in db], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    if not config.has_api_key():
        print("❌ Gemini API 키가 설정 안 됐어요. 먼저 /admin 페이지에서 등록하거나")
        print("   GEMINI_API_KEY 환경변수를 설정해줘.")
        sys.exit(1)

    source_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE_DIR
    if not source_dir.exists():
        print(f"❌ 폴더를 못 찾았어: {source_dir}")
        sys.exit(1)

    files = sorted(source_dir.glob("*.md"))
    if not files:
        print(f"❌ {source_dir} 안에 .md 파일이 없어.")
        sys.exit(1)

    db = load_db()
    by_name: dict[str, int] = {s.name: i for i, s in enumerate(db)}

    print(f"[시작] {len(files)}개 파일 발견, 현재 DB에 {len(db)}건 있음 (모델: {config.get_model()})\n")

    added, updated, unchanged, failed = 0, 0, 0, 0
    failed_files: list[str] = []

    for i, path in enumerate(files, 1):
        raw = path.read_text(encoding="utf-8")
        print(f"[{i}/{len(files)}] {path.name} 분석 중...", end=" ", flush=True)
        try:
            parsed = bot_core.extract_scholarship(raw)
        except Exception as e:  # noqa: BLE001 — 배치 처리 중 하나 실패해도 계속 진행
            print(f"❌ 실패: {e}")
            failed += 1
            failed_files.append(f"{path.name}: {e}")
            continue

        open_mark = "🟢 신청가능" if parsed.is_open_application else "⚪ 마감/결과"
        existing_idx = by_name.get(parsed.name)

        if existing_idx is None:
            # 처음 보는 공지 — 새 id 부여해서 신규 추가
            parsed.id = parsed.id or uuid.uuid4().hex[:12]
            db.append(parsed)
            by_name[parsed.name] = len(db) - 1
            save_db(db)  # 매번 즉시 저장 — 중간에 죽어도 그동안 처리분은 안전
            added += 1
            print(f"✅ 신규 추가 — {parsed.name} ({open_mark})")
        else:
            # 이미 있는 공지 — id는 그대로 유지하고, 내용이 달라졌을 때만 최신 내용으로 갱신
            existing = db[existing_idx]
            merged = parsed.model_copy(update={"id": existing.id})
            if merged.model_dump() == existing.model_dump():
                unchanged += 1
                print(f"⏭️  변경 없음 (동일한 내용): {parsed.name}")
            else:
                db[existing_idx] = merged
                save_db(db)
                updated += 1
                print(f"🔄 업데이트됨 — {parsed.name} ({open_mark})")

        time.sleep(0.5)  # API 레이트리밋 여유

    print(f"\n[완료] 신규 추가 {added}건 / 업데이트 {updated}건 / 변경없음 {unchanged}건 / 실패 {failed}건")
    print(f"현재 DB 총 {len(db)}건 → data/scholarship_db.json")

    if failed_files:
        print("\n실패한 파일 목록 (필요하면 관리자 페이지에서 수동으로 다시 시도해줘):")
        for line in failed_files:
            print(f"  - {line}")


if __name__ == "__main__":
    main()
