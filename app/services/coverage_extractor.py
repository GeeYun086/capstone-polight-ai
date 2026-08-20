import json
import logging
import re
from pathlib import Path

from openai import OpenAI

from app.core.config import get_settings
from app.services.answer_providers import generate_json
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
2. 약관에 없는 값은 null로 두십시오.

   금액(limitAmount)은 약관에 숫자가 명시된 경우에만 채웁니다.
   "보험증권에 기재된 보험가입금액을 한도로" 처럼 증권을 참조하는 표현은 금액을 알 수 없으므로 null입니다.

   반면 한도 문구(limitLabel)는 항상 채우십시오. 이것은 화면에 그대로 표시되는 문구라
   비어 있으면 사용자에게 한도 칸이 빈칸으로 보입니다. 숫자가 없더라도 약관이 한도를
   어떻게 정하는지를 그대로 옮기면 됩니다.

     약관에 숫자가 있음    -> "1개 또는 1조당 20만원 한도"
     증권을 참조함         -> "보험가입금액 한도 (증권 확인 필요)"
     조건에 따라 다름      -> "입원 5천만원 / 통원 1회 30만원 한도"
     자기부담금이 있음      -> "보험가입금액 한도, 자기부담금 공제 후 지급"
3. exclusions의 sourceText와 sources의 quoteText는 반드시 제공된 조각의 원문을 그대로 옮기십시오.
   문장을 새로 만들지 마십시오.
4. sources에는 실제로 근거가 된 조각의 chunkId만 넣으십시오.
   sourceRole은 보장 근거면 COVERAGE, 면책 근거면 EXCLUSION,
   한도 근거면 LIMIT, 청구서류 근거면 DOCUMENT입니다.
5. 반드시 지정된 JSON 형식 하나만 출력하십시오. 설명을 덧붙이지 마십시오.

coverageStatus는 아래 기준으로 정하십시오. 조각에 붙은 [보장]/[면책] 라벨이 아니라
조각의 실제 내용을 읽고 판단하십시오. 라벨이 틀린 경우가 있습니다.

- COVERED    이 담보를 보상하는 조항이 있습니다.
- PARTIAL    보상하지만 범위가 제한적입니다. 일부 항목만 보상하거나
             조건을 충족해야만 보상하는 경우입니다.
- EXCLUDED   약관이 이 담보를 명시적으로 보상하지 않는다고 밝혔습니다.
             "보상하지 않습니다", "보상하여 드리지 않습니다"처럼 배제를 선언한
             조항이 있고, 이를 보상하는 조항은 없는 경우입니다.
- NOT_COVERED 관련 조항이 조각에 아예 없습니다. 보상한다는 말도, 보상하지
             않는다는 말도 없는 경우입니다.
- UNKNOWN    조항은 있으나 표현이 모호해 보상 여부를 단정할 수 없습니다.

EXCLUDED와 NOT_COVERED를 구분하는 것이 중요합니다. 전자는 사용자에게
"이 약관은 그것을 보상하지 않습니다"라고 알려줄 수 있지만, 후자는
"관련 내용을 찾지 못했습니다"까지만 말할 수 있습니다."""

OUTPUT_SCHEMA = """{
  "title": "담보 이름 (예: 해외 상해 의료비)",
  "subtitle": "한 줄 요약 또는 null",
  "limitLabel": "화면에 그대로 표시할 한도 문구. 반드시 채우십시오. 100자 이내. 숫자가 없으면 '보험가입금액 한도 (증권 확인 필요)'처럼 약관이 한도를 정하는 방식을 옮깁니다",
  "isCovered": true,
  "coverageStatus": "COVERED | PARTIAL | EXCLUDED | NOT_COVERED | UNKNOWN",
  "limitAmount": 10000000,
  "limitCurrency": "KRW",
  "conditions": "보장 조건 요약 또는 null",
  "detailItems": [{"title": "이 담보에 포함되는 개별 보장 항목 (예: 입원의료비, 통원의료비)", "subtitle": "한 줄 설명 또는 null", "isCovered": true}],
  "subLimits": [{"label": "무엇에 대한 한도인지 (예: 1개당, 입원 1일당, 자기부담금)", "value": "화면에 표시할 값 (예: 20만원, 10%). 200자 이내", "limitAmount": "숫자로 쓸 수 있으면 정수, 아니면 null", "limitCurrency": "KRW 또는 USD", "description": "부연 설명 또는 null"}],
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
# 한 번에 보낼 조각의 최대 글자 수.
#
# 실측에서 medical_expense가 48,276토큰이라 OpenAI의 분당 토큰 한도(30,000)를 넘겨
# 통째로 실패했다. 가장 중요한 의료비 담보가 빠지는 상황이라 그냥 둘 수 없다.
# 한국어는 대략 1.5자당 1토큰이므로 30,000자면 약 20,000토큰이고, 출력과
# 시스템 프롬프트를 더해도 한도 안에 들어온다.
MAX_CONTEXT_CHARS = 30_000


def _split_by_budget(chunks: list[dict], budget: int = MAX_CONTEXT_CHARS) -> list[list[dict]]:
    """조각을 글자 수 예산에 맞춰 나눈다. 조각 하나는 쪼개지 않는다."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0

    for chunk in chunks:
        length = len(chunk["text"]) + len(chunk.get("section_title") or "") + 100
        if current and size + length > budget:
            batches.append(current)
            current, size = [], 0
        current.append(chunk)
        size += length

    if current:
        batches.append(current)
    return batches


def _merge_items(items: list[CoverageItem]) -> CoverageItem:
    """배치별 추출 결과를 담보 하나로 합친다.

    목록형 필드는 이어붙이고 중복만 없앤다. 배치를 나눴다고 면책 조건이 줄면
    나누는 의미가 없기 때문이다. 단일 값은 처음 채워진 것을 쓴다.
    앞 배치가 비워둔 값을 뒤 배치가 채우는 경우를 살리기 위해서다.
    """
    base = items[0]

    def first(attr):
        return next((getattr(i, attr) for i in items if getattr(i, attr) is not None), None)

    def merge(attr, key):
        seen, merged = set(), []
        for item in items:
            for entry in getattr(item, attr):
                marker = key(entry)
                if marker not in seen:
                    seen.add(marker)
                    merged.append(entry)
        return merged

    base.limit_amount = first("limit_amount")
    base.limit_label = first("limit_label")
    base.limit_currency = first("limit_currency")
    base.conditions = first("conditions")
    base.detail_items = merge("detail_items", lambda e: e.title)
    base.sub_limits = merge("sub_limits", lambda e: (e.label, e.value))
    base.required_documents = merge("required_documents", lambda e: e.document_name)
    base.exclusions = merge("exclusions", lambda e: e.title)
    base.sources = merge("sources", lambda e: (e.chunk_id, e.quote_text))
    return base


def extract_coverage_item(
    category: str,
    display_name: str,
    chunks: list[dict],
    client: OpenAI | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> tuple[CoverageItem | None, list[str]]:
    if not chunks:
        return None, [f"{category}: 해당 조각이 없습니다"]

    # 조각이 많으면 나눠 보내고 결과를 합친다. 나누지 않으면 큰 카테고리가
    # 토큰 한도에 걸려 통째로 실패한다.
    batches = _split_by_budget(chunks)
    if len(batches) > 1:
        logger.info("%s: 조각이 많아 %d개 배치로 나눕니다", category, len(batches))
        items, warnings = [], []
        for batch in batches:
            item, batch_warnings = _extract_single(
                category, display_name, batch, client, model, provider
            )
            if item:
                items.append(item)
            warnings.extend(batch_warnings)
        if not items:
            return None, warnings
        return _merge_items(items), warnings

    return _extract_single(category, display_name, chunks, client, model, provider)


def _extract_single(
    category: str,
    display_name: str,
    chunks: list[dict],
    client: OpenAI | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> tuple[CoverageItem | None, list[str]]:
    settings = get_settings()

    user_message = (
        f"[담보 카테고리]\n{category} ({display_name})\n\n"
        f"[약관 조각]\n{build_context(chunks)}\n\n"
        f"[출력 형식]\n{OUTPUT_SCHEMA}"
    )

    # client를 직접 넘기면 옛 경로(OpenAI 고정)를 쓰고, 없으면 벤더 레지스트리를 탄다.
    # 테스트가 호출을 가로챌 수 있도록 남겨둔 통로다.
    if client is not None:
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
    else:
        # provider를 안 넘기면 추출 전용 기본값을 쓴다. generate_json의 기본값은
        # answer_provider(답변용)라, 그대로 두면 추출이 답변 모델로 돌아간다.
        raw, _ = generate_json(
            SYSTEM_PROMPT, user_message,
            provider_name=provider or settings.extraction_provider,
        )

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
    provider: str | None = None,
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
                category, meta["display_name"], matched,
                client=client, model=model, provider=provider
            )
        except Exception as e:
            warnings.append(f"{category}: 추출 실패 ({e})")
            logger.warning("%s 추출 실패: %s", category, e)
            continue

        if item:
            items.append(item)
            warnings.extend(item_warnings)

    return items, warnings
