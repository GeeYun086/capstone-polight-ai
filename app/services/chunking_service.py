from pathlib import Path

from app.repositories.base import ChunkScope
from app.services.upstage_parser import parse_pdf
from scripts.chunk_policy import (
    DEFAULT_MAPPING_PATH,
    build_mapping_entries,
    create_chunks,
    create_chunks_from_elements,
    load_json,
)


# API로 들어온 분석 요청의 청킹 진입점.
#
# Upstage Document Parse로 파싱한 뒤 요소를 조항 단위로 재조립한다.
# pymupdf 경로(chunk_pages)보다 조항 제목이 정확하고 표 구조가 살아 있으며,
# policy_chunks의 source_content_type(NOT NULL)과 clause_path를 채울 수 있다.
# 실측(db_travel, 12문항): Recall@8 66.7% -> 83.3%, Matched 34% -> 63%.
#
# 파싱 응답은 캐시되므로 같은 문서를 다시 분석해도 API 비용이 다시 들지 않는다.
def parse_and_chunk(
    pdf_path: Path,
    scope: ChunkScope | None = None,
    mapping_path: Path | None = None,
) -> list[dict]:
    elements = parse_pdf(pdf_path)
    mapping = load_json(mapping_path or DEFAULT_MAPPING_PATH)
    chunks = create_chunks_from_elements(elements, pdf_path.name, build_mapping_entries(mapping))

    if scope is None:
        return chunks

    fields = scope.as_fields()
    return [{**chunk, **fields} for chunk in chunks]


# scripts/chunk_policy.py의 청킹 로직을 그대로 재사용.
# pages(추출 결과)를 받아 조항 단위 chunk 리스트를 in-memory로 반환한다.
#
# scope가 주어지면 각 청크에 user_id/trip_id/policy_id/document_id를 실어준다.
# 이 값들은 AnalysisStartRequest로 이미 들어와 있는데 지금까지 쓰이지 않고 버려졌다.
# 청크에 실리지 않으면 검색 시 "이 계약의 약관만" 필터링할 방법이 없어 챗봇이 성립하지 않는다.
# 스코프 주입을 chunk_policy.py가 아니라 여기서 하는 이유는, chunk_policy.py는 CLI로도 쓰이는
# 순수 청킹 모듈이라 요청 컨텍스트를 알 필요가 없기 때문이다.
def chunk_pages(
    pages: list[dict],
    source_file: str,
    mapping_path: Path | None = None,
    scope: ChunkScope | None = None,
) -> list[dict]:
    mapping = load_json(mapping_path or DEFAULT_MAPPING_PATH)
    mapping_entries = build_mapping_entries(mapping)
    chunks = create_chunks(pages, source_file, mapping_entries)

    if scope is None:
        return chunks

    fields = scope.as_fields()
    return [{**chunk, **fields} for chunk in chunks]
