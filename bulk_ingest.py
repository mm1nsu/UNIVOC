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

이미 DB에 있는(이름이 같은) 공지는 자동으로 건너뜀 — 여러 번 실행해도 중복 안 생김.
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
    existing_names = {s.name for s in db}

    print(f"[시작] {len(files)}개 파일 발견, 현재 DB에 {len(db)}건 있음 (모델: {config.get_model()})\n")

    added, skipped, failed = 0, 0, 0
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

        if parsed.name in existing_names:
            print(f"⏭️  건너뜀 (이미 있음: {parsed.name})")
            skipped += 1
            continue

        parsed.id = parsed.id or uuid.uuid4().hex[:12]
        db.append(parsed)
        existing_names.add(parsed.name)
        save_db(db)  # 매번 즉시 저장 — 중간에 죽어도 그동안 처리분은 안전
        added += 1
        open_mark = "🟢 신청가능" if parsed.is_open_application else "⚪ 마감/결과"
        print(f"✅ 추가됨 — {parsed.name} ({open_mark})")

        time.sleep(0.5)  # API 레이트리밋 여유

    print(f"\n[완료] 신규 추가 {added}건 / 건너뜀(중복) {skipped}건 / 실패 {failed}건")
    print(f"현재 DB 총 {len(db)}건 → data/scholarship_db.json")

    if failed_files:
        print("\n실패한 파일 목록 (필요하면 관리자 페이지에서 수동으로 다시 시도해줘):")
        for line in failed_files:
            print(f"  - {line}")


if __name__ == "__main__":
    main()
