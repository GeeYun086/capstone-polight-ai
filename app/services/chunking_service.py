from pathlib import Path

from scripts.chunk_policy import DEFAULT_MAPPING_PATH, build_mapping_entries, create_chunks, load_json


# scripts/chunk_policy.py의 청킹 로직을 그대로 재사용.
# pages(추출 결과)를 받아 조항 단위 chunk 리스트를 in-memory로 반환한다.
def chunk_pages(
    pages: list[dict],
    source_file: str,
    mapping_path: Path | None = None,
) -> list[dict]:
    mapping = load_json(mapping_path or DEFAULT_MAPPING_PATH)
    mapping_entries = build_mapping_entries(mapping)
    return create_chunks(pages, source_file, mapping_entries)
