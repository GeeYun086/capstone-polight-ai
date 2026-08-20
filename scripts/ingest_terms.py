"""약관 PDF를 색인해 검색 가능한 상태로 만든다.

증권은 사용자가 올리지만 약관은 우리가 미리 넣는다. 같은 보험 상품의 약관은
사용자가 달라도 내용이 같으므로, 한 번 색인해 모두가 공유한다.

지금까지는 두 단계로 나뉘어 있었고(run_upstage_pipeline.py -> embed_chunks.py)
레지스트리 등록은 손으로 했다. 한 번에 하지 않으면 임베딩을 빠뜨린 채로 넘어가는데,
그러면 검색 결과가 0건이 되고 원인이 "약관이 없다"로만 보인다.

    python scripts/ingest_terms.py                        # raw_pdfs 전체
    python scripts/ingest_terms.py --pdf carrot_travel_2025.pdf
    python scripts/ingest_terms.py --pdf 새약관.pdf --insurer 캐롯손해보험 --product "캐롯 해외여행보험"

저장 위치는 DATABASE_URL 유무로 갈린다.

    비어 있음   data/chunks + data/embeddings (파일 저장소)
    설정됨      policy_chunks -- 다만 이 테이블은 user_id/analysis_result_id가
                NOT NULL이라 주인 없는 공유 약관을 넣을 수 없다. 백엔드에 요청한
                policy_terms 테이블이 생기기 전까지는 파일 저장소를 쓴다.
                docs/BACKEND_INTERFACE.md 3-2 참고.
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services.embedding_providers import build_client, get_provider  # noqa: E402
from app.services.upstage_parser import parse_pdf  # noqa: E402
from scripts.chunk_policy import (  # noqa: E402
    DEFAULT_MAPPING_PATH,
    build_mapping_entries,
    create_chunks_from_elements,
    load_json,
    save_json,
)
from scripts.embed_chunks import embed_chunks_file  # noqa: E402

RAW_PDF_DIR = PROJECT_ROOT / "data" / "raw_pdfs"
CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"
EMBEDDINGS_DIR = PROJECT_ROOT / "data" / "embeddings"
REGISTRY_PATH = PROJECT_ROOT / "config" / "terms_registry.json"


def registered(terms_id: str) -> dict | None:
    with REGISTRY_PATH.open("r", encoding="utf-8") as f:
        for entry in json.load(f)["terms"]:
            if entry["id"] == terms_id:
                return entry
    return None


def ingest(pdf_path: Path, client, force: bool) -> dict:
    terms_id = pdf_path.stem
    chunks_path = CHUNKS_DIR / f"{terms_id}_chunks.json"
    embeddings_path = EMBEDDINGS_DIR / f"{terms_id}_embeddings.json"

    # 이미 있으면 건너뛴다. 파싱은 페이지 단위 과금이고 임베딩도 토큰 과금이라,
    # 무심코 전체를 다시 돌리면 돈이 나간다. 파싱 캐시가 있어 재파싱은 막히지만
    # 임베딩은 그대로 다시 청구된다.
    if not force and chunks_path.exists() and embeddings_path.exists():
        with chunks_path.open("r", encoding="utf-8") as f:
            existing = json.load(f)
        print(f"  이미 색인됨 (청크 {len(existing)}개). 다시 하려면 --force")
        return {"terms_id": terms_id, "chunks": len(existing), "skipped": True}

    elements = parse_pdf(pdf_path)
    mapping = build_mapping_entries(load_json(DEFAULT_MAPPING_PATH))
    chunks = create_chunks_from_elements(elements, pdf_path.name, mapping)
    save_json(chunks, chunks_path)

    # embed_chunks_file은 이미 배치 처리·중간 저장·이어하기를 한다.
    # 여기서 다시 짜면 임베딩 텍스트 구성이 어긋날 위험이 있다. 구성이 다르면
    # 색인과 질의가 다른 공간에 놓여 검색이 조용히 나빠진다.
    EMBEDDINGS_DIR.mkdir(parents=True, exist_ok=True)
    vectors = embed_chunks_file(chunks_path, embeddings_path, client, None)

    labeled = sum(1 for c in chunks if c.get("matched_category"))
    with_path = sum(1 for c in chunks if c.get("clause_path"))
    return {
        "terms_id": terms_id,
        "elements": len(elements),
        "chunks": len(chunks),
        "labeled": labeled,
        "with_path": with_path,
        "embeddings": len(vectors),
        "skipped": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="약관 색인 (파싱 -> 청킹 -> 임베딩 -> 저장)")
    parser.add_argument("--pdf", type=str, default=None, help="특정 파일만. 생략하면 raw_pdfs 전체")
    parser.add_argument("--force", action="store_true", help="이미 색인된 것도 다시 한다 (과금 발생)")
    parser.add_argument("--insurer", type=str, default=None, help="레지스트리에 없을 때 쓸 보험사명")
    parser.add_argument("--product", type=str, default=None, help="레지스트리에 없을 때 쓸 상품명")
    args = parser.parse_args()

    settings = get_settings()
    if settings.database_url:
        print(
            "경고: DATABASE_URL이 설정돼 있지만 이 스크립트는 파일 저장소에 넣습니다.\n"
            "      policy_chunks는 user_id/analysis_result_id가 NOT NULL이라 주인 없는\n"
            "      공유 약관을 넣을 수 없습니다. docs/BACKEND_INTERFACE.md 3-2 참고.\n"
        )

    pdfs = [RAW_PDF_DIR / args.pdf] if args.pdf else sorted(RAW_PDF_DIR.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"처리할 PDF가 없습니다: {RAW_PDF_DIR}")

    provider = get_provider(settings.embedding_provider)
    client = build_client(provider)
    print(f"임베딩 벤더: {provider.name} (문서 모델 {provider.doc_model})\n")

    results = []
    missing_registry = []
    for pdf in pdfs:
        if not pdf.exists():
            raise SystemExit(f"PDF를 찾을 수 없습니다: {pdf}")
        print(f"=== {pdf.name}")
        results.append(ingest(pdf, client, args.force))

        # 레지스트리에 없으면 검색은 되지만 증권에서 이 약관을 찾을 수 없다.
        # 색인해놓고 연결이 빠지는 것이 가장 흔한 실수라 여기서 짚어준다.
        if registered(pdf.stem) is None:
            missing_registry.append(pdf.stem)
        print()

    print(f'{"약관":26} {"청크":>6} {"라벨":>10} {"특약명":>10} {"임베딩":>7}')
    print("-" * 64)
    for r in results:
        if r["skipped"]:
            print(f'{r["terms_id"][:26]:26} {r["chunks"]:>6}   (건너뜀)')
            continue
        print(
            f'{r["terms_id"][:26]:26} {r["chunks"]:>6} '
            f'{r["labeled"]:>5}({r["labeled"] / r["chunks"]:>4.0%}) '
            f'{r["with_path"]:>5}({r["with_path"] / r["chunks"]:>4.0%}) {r["embeddings"]:>7}'
        )

    if missing_registry:
        print("\n레지스트리에 없는 약관이 있습니다. 이대로면 증권에서 이 약관을 찾지 못합니다.")
        print(f"config/terms_registry.json 의 terms 배열에 추가하십시오.\n")
        for terms_id in missing_registry:
            print(json.dumps({
                "id": terms_id,
                "insurer": args.insurer or "보험사명",
                "product": args.product or "상품명",
                "aliases": [],
                "revision": None,
            }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
