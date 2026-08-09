import json
import logging
import re
from pathlib import Path

from openai import OpenAI

from app.core.config import get_settings
from app.schemas.coverage import CoverageItem

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STANDARD_CATEGORIES_PATH = PROJECT_ROOT / "config" / "standard_categories.json"

# 조각을 프롬프트에 넣을 때의 라벨. 조항 성격을 명시해야 LLM이
# 보장 조건과 면책 사유를 섞지 않는다.
CLAUSE_TYPE_LABELS = {
    "included": "보장",
    "excluded": "면책",
    "procedure": "청구절차",
    "definition": "용어정의",
}

SYSTEM_PROMPT = """당신은 여행자보험 약관에서 보장 항목 정보를 추출하는 도구입니다.

규칙:
1. 제공된 약관 조각에 실제로 적혀 있는 내용만 추출하십시오. 추측하거나 일반 상식으로 채우지 마십시오.
2. 약관에 없는 값은 null로 두십시오. 특히 금액은 약관에 숫자가 명시된 경우에만 채웁니다.
   "보험증권에 기재된 보험가입금액을 한도로" 처럼 증권을 참조하는 표현은 금액을 알 수 없으므로 null입니다.
3. exclusions의 sourceText와 sources의 quoteText는 반드시 제공된 조각의 원문을 그대로 옮기십시오.
   문장을 새로 만들지 마십시오.
4. sources에는 실제로 근거가 된 조각의 chunkId만 넣으십시오.
   sourceRole은 보장 근거면 COVERAGE, 면책 근거면 EXCLUSION,
   한도 근거면 LIMIT, 청구서류 근거면 DOCUMENT입니다.
5. 반드시 지정된 JSON 형식 하나만 출력하십시오. 설명을 덧붙이지 마십시오."""

OUTPUT_SCHEMA = """{
  "title": "담보 이름 (예: 해외 상해 의료비)",
  "subtitle": "한 줄 요약 또는 null",
  "limitLabel": "화면 표시용 한도 문구 (예: 최대 1,000만원) 또는 null",
  "isCovered": true,
  "coverageStatus": "COVERED | NOT_COVERED | PARTIAL | UNKNOWN",
  "limitAmount": 10000000,
  "limitCurrency": "KRW",
  "conditions": "보장 조건 요약 또는 null",
  "detailItems": [{"title": "세부 항목", "subtitle": null, "isCovered": true}],
  "subLimits": [{"label": "구분", "value": "표시값", "limitAmount": null, "limitCurrency": null, "description": null}],
  "requiredDocuments": [{"documentName": "서류명", "isMandatory": true}],
  "exclusions": [{"title": "면책 사유", "description": null, "sourceText": "약관 원문 그대로", "severity": "HIGH | MEDIUM | LOW"}],
  "sources": [{"chunkId": "청크 id", "sourceRole": "COVERAGE | EXCLUSION | LIMIT | DOCUMENT", "quoteText": "약관 원문 그대로"}]
}"""


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


# 카테고리에 속한 조각들을 프롬프트용 텍스트로 만든다.
# chunkId를 함께 적어야 LLM이 근거를 지목할 수 있고, 그래야 coverage_item_sources를 채운다.
def build_context(chunks: list[dict]) -> str:
    blocks = []
    for chunk in chunks:
        label = CLAUSE_TYPE_LABELS.get(chunk["coverage_type"], chunk["coverage_type"])
        path = chunk.get("clause_path") or ""
        header = f"[chunkId: {chunk['chunk_id']}] [{label}]"
        if path:
            header += f" {path} >"
        blocks.append(f"{header} {chunk['section_title']}\n{chunk['text']}")
    return "\n\n".join(blocks)


# 인용문이 실제로 들어있는 조각을 찾아 chunkId를 바로잡는다.
#
# 실측해보니 LLM은 인용문 자체는 약관에서 제대로 가져오는데, 그게 어느 조각에서
# 나왔는지를 자주 헷갈린다(8개 카테고리 중 4개에서 오지정). 조각을 여러 개 한꺼번에
# 보여주니 ID를 정확히 추적하기 어려운 것이다.
# coverage_item_sources.policy_chunk_id는 FK라 틀린 ID가 들어가면 저장이 실패하거나
# 엉뚱한 조항이 근거로 표시된다. 그래서 LLM에게 맡기지 않고 문자열 매칭으로 교정한다.
def repair_source_ids(item: CoverageItem, chunks: list[dict]) -> int:
    by_id = {c["chunk_id"]: _normalize(c["text"]) for c in chunks}
    repaired = 0

    for source in item.sources:
        quote = source.quote_text
        # LLM이 JSON null 대신 문자열 "null"을 넣는 경우가 있다
        if not quote or quote.strip().lower() == "null":
            source.quote_text = None
            continue

        needle = _normalize(quote)
        if source.chunk_id in by_id and needle in by_id[source.chunk_id]:
            continue

        owner = next((cid for cid, text in by_id.items() if needle in text), None)
        if owner:
            source.chunk_id = owner
            repaired += 1

    return repaired


# 추출 결과가 실제 원문에 근거하는지 검증한다.
#
# LLM이 그럴듯한 문장을 지어내면 보험 서비스에서는 치명적이다. 금액이 틀리면
# 사용자가 잘못된 기대를 갖게 되고, 없는 조항을 인용하면 근거가 조작된 셈이 된다.
# 그래서 (1) 인용문이 원문에 실제로 있는지, (2) chunkId가 입력에 있던 것인지 확인한다.
def validate_against_source(item: CoverageItem, chunks: list[dict]) -> list[str]:
    warnings: list[str] = []
    by_id = {c["chunk_id"]: _normalize(c["text"]) for c in chunks}
    corpus = "".join(by_id.values())

    for source in item.sources:
        if source.chunk_id not in by_id:
            warnings.append(f"입력에 없는 chunkId를 인용: {source.chunk_id}")
        elif source.quote_text and _normalize(source.quote_text) not in by_id[source.chunk_id]:
            warnings.append(f"인용문이 해당 조각 원문에 없음: {source.chunk_id}")

    for exclusion in item.exclusions:
        if exclusion.source_text and _normalize(exclusion.source_text) not in corpus:
            warnings.append(f"면책 원문이 약관에 없음: {exclusion.title}")

    # 금액은 원문에 숫자가 있어야 한다. 없으면 LLM이 만들어낸 값일 가능성이 크다.
    if item.limit_amount is not None:
        digits = str(item.limit_amount)
        if digits not in _normalize(corpus) and f"{item.limit_amount:,}" not in corpus:
            warnings.append(f"한도 금액 {item.limit_amount}이 원문에서 확인되지 않음")

    return warnings


# 카테고리 하나에 대해 보장 항목을 추출한다.
#
# 약관 전체를 한 번에 넣지 않고 카테고리별로 나눠 넣는 이유:
# 250페이지를 통째로 주면 느리고 불안정할 뿐 아니라, 결과가 약관 어디에서 나왔는지
# 알 수 없어 coverage_item_sources(근거 연결)를 채울 수 없다.
# 조각을 직접 넣으면 chunkId가 그대로 따라온다.
def extract_coverage_item(
    category: str,
    display_name: str,
    chunks: list[dict],
    client: OpenAI | None = None,
    model: str | None = None,
) -> tuple[CoverageItem | None, list[str]]:
    if not chunks:
        return None, [f"{category}: 해당 조각이 없습니다"]

    settings = get_settings()
    if client is None:
        if not settings.openai_api_key:
            raise ValueError(".env에 OPENAI_API_KEY가 설정되지 않았습니다.")
        client = OpenAI(api_key=settings.openai_api_key)

    user_message = (
        f"[담보 카테고리]\n{category} ({display_name})\n\n"
        f"[약관 조각]\n{build_context(chunks)}\n\n"
        f"[출력 형식]\n{OUTPUT_SCHEMA}"
    )

    response = client.chat.completions.create(
        model=model or settings.extraction_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )

    raw = response.choices[0].message.content or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, [f"{category}: JSON 파싱 실패"]

    payload["category"] = category
    item = CoverageItem.model_validate(payload)

    repaired = repair_source_ids(item, chunks)
    if repaired:
        logger.info("%s: 근거 chunkId %d건 교정", category, repaired)

    return item, validate_against_source(item, chunks)


def load_categories() -> dict:
    with STANDARD_CATEGORIES_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


# 청크 전체에서 카테고리별로 보장 항목을 뽑는다.
#
# 카테고리 하나가 실패해도 나머지는 살린다. 분석 전체를 실패로 돌리면 어렵게 만든
# policy_chunks까지 무의미해지고, 보장 항목 일부가 비는 것이 전부 없는 것보다 낫다.
def extract_all(
    chunks: list[dict],
    client: OpenAI | None = None,
    model: str | None = None,
) -> tuple[list[CoverageItem], list[str]]:
    categories = load_categories()
    items: list[CoverageItem] = []
    warnings: list[str] = []

    # ui_priority 순으로 만들어 coverage_items.sort_order에 그대로 쓴다
    for category, meta in sorted(categories.items(), key=lambda kv: kv[1]["ui_priority"]):
        matched = [c for c in chunks if c.get("matched_category") == category]
        if not matched:
            logger.info("%s: 해당 조각이 없어 건너뜁니다", category)
            continue

        try:
            item, item_warnings = extract_coverage_item(
                category, meta["display_name"], matched, client=client, model=model
            )
        except Exception as e:
            warnings.append(f"{category}: 추출 실패 ({e})")
            logger.warning("%s 추출 실패: %s", category, e)
            continue

        if item:
            items.append(item)
            warnings.extend(item_warnings)

    return items, warnings
