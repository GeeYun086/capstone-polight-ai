import json
import logging
from pathlib import Path

import fitz
import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = PROJECT_ROOT / "data" / "parsed_results"

API_URL = "https://api.upstage.ai/v1/document-digitization"

# 한 요청에 보낼 페이지 수. 약관은 100페이지를 훌쩍 넘는 경우가 많아 통째로 보내면
# 타임아웃이나 용량 제한에 걸릴 수 있어서 나눠 보낸다.
PAGES_PER_REQUEST = 50

REQUEST_TIMEOUT = 600.0


# Upstage Document Parse는 기본적으로 HTML만 돌려준다.
# text를 명시적으로 요청해야 청킹에 쓸 평문이 나온다.
OUTPUT_FORMATS = '["text","html"]'


def _post_pdf(pdf_bytes: bytes, api_key: str) -> dict:
    response = httpx.post(
        API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        data={
            "model": "document-parse",
            "output_formats": OUTPUT_FORMATS,
            "coordinates": "false",
        },
        files={"document": ("document.pdf", pdf_bytes, "application/pdf")},
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Upstage 파싱 실패 ({response.status_code}): {response.text[:300]}")
    return response.json()


# PDF를 페이지 구간으로 잘라 순차 요청하고, 요소들을 하나로 합친다.
# 잘라 보내면 각 응답의 page 번호가 1부터 다시 시작하므로 원본 기준으로 보정한다.
def _parse_in_batches(pdf_path: Path, api_key: str) -> list[dict]:
    with fitz.open(pdf_path) as doc:
        total_pages = doc.page_count

        elements: list[dict] = []
        for start in range(0, total_pages, PAGES_PER_REQUEST):
            end = min(start + PAGES_PER_REQUEST, total_pages) - 1

            with fitz.open() as part:
                part.insert_pdf(doc, from_page=start, to_page=end)
                pdf_bytes = part.tobytes()

            logger.info("Upstage 파싱 %s: %d~%d 페이지", pdf_path.name, start + 1, end + 1)
            result = _post_pdf(pdf_bytes, api_key)

            for element in result.get("elements", []):
                elements.append(
                    {
                        "page": element.get("page", 1) + start,
                        "category": element.get("category", "paragraph"),
                        "text": (element.get("content") or {}).get("text", "") or "",
                        "html": (element.get("content") or {}).get("html", "") or "",
                    }
                )

    return elements


# PDF 하나를 파싱해 요소 리스트를 반환한다.
#
# 응답을 캐시하는 이유: Upstage는 페이지 단위 과금이라 재실행할 때마다 비용이 든다.
# 청킹 로직을 고치면서 파싱을 반복하게 되므로, 원본이 바뀌지 않는 한 캐시를 쓴다.
def parse_pdf(pdf_path: Path, api_key: str | None = None, use_cache: bool = True) -> list[dict]:
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF 파일을 찾을 수 없습니다: {pdf_path}")

    cache_path = CACHE_DIR / f"{pdf_path.stem}_upstage.json"
    if use_cache and cache_path.exists():
        logger.info("캐시된 파싱 결과 사용: %s", cache_path.name)
        with cache_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    api_key = api_key or get_settings().upstage_api_key
    if not api_key:
        raise ValueError(".env에 UPSTAGE_API_KEY가 설정되지 않았습니다.")

    elements = _parse_in_batches(pdf_path, api_key)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(elements, f, ensure_ascii=False)
    logger.info("파싱 결과 저장: %s (요소 %d개)", cache_path.name, len(elements))

    return elements


# 요소를 pymupdf 경로와 같은 [{page, text}] 형태로 접는다.
# 기존 도구(table_eval 등)나 비교용으로 필요할 때 쓴다. 청킹에는 요소 구조를
# 그대로 쓰는 편이 나으므로 create_chunks_from_elements를 사용한다.
def elements_to_pages(elements: list[dict]) -> list[dict]:
    by_page: dict[int, list[str]] = {}
    for element in elements:
        text = element["text"].strip()
        if text:
            by_page.setdefault(element["page"], []).append(text)

    return [{"page": page, "text": "\n".join(lines)} for page, lines in sorted(by_page.items())]
