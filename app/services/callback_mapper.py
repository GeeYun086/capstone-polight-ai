"""추출 결과(CoverageItem)를 Spring 콜백 payload로 변환한다.

DB 제약에 맞추는 일이 전부 여기 모여 있다. 변환 로직을 analysis_service에 흩어놓으면
제약이 바뀔 때 어디를 고쳐야 할지 알기 어려워진다.

여기서 하는 일 네 가지:

  chunk_id 치환   내부 문자열 id -> policy_chunks.id(UUID). FK로 연결되므로 필수
  enum 번역       CHECK 제약값으로 변환
  길이 컷         VARCHAR 한도 초과 시 INSERT가 실패한다
  중복 제거       coverage_item_sources UNIQUE(item, chunk, role) 위반 방지
"""

import logging

from app.schemas import db_enums
from app.schemas.db_limits import cut
from app.schemas.analysis import (
    CoverageItemPayload,
    DetailItemPayload,
    ExclusionPayload,
    RequiredDocumentPayload,
    SourcePayload,
    SubLimitPayload,
)
from app.schemas.coverage import CoverageItem

logger = logging.getLogger(__name__)


def _dedupe_sources(
    item: CoverageItem, chunk_id_map: dict[str, str]
) -> list[SourcePayload]:
    """근거를 (chunk, role) 기준으로 중복 제거한다.

    coverage_item_sources에 UNIQUE(coverage_item_id, policy_chunk_id, source_role)이
    걸려 있어, 같은 담보에 같은 조각을 같은 역할로 두 번 실으면 저장이 실패한다.
    LLM은 같은 조각을 여러 번 인용하는 경우가 흔하므로 보내기 전에 걸러야 한다.

    첫 등장을 남긴다. 뒤에 온 것이 quoteText가 더 길더라도, 순서를 흔들면
    화면에 보이는 근거 순서가 매번 달라진다.
    """
    seen: set[tuple[str, str]] = set()
    sources: list[SourcePayload] = []
    dropped = 0

    for source in item.sources:
        # 저장 시 생성한 UUID로 바꿔야 FK가 연결된다.
        # 매핑이 없으면(파일 저장소 등) 원래 id를 그대로 둔다.
        chunk_id = chunk_id_map.get(source.chunk_id, source.chunk_id)
        role = db_enums.source_role(source.source_role)

        key = (chunk_id, role)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)

        sources.append(
            SourcePayload(chunkId=chunk_id, sourceRole=role, quoteText=source.quote_text)
        )

    if dropped:
        logger.info("%s: 중복 근거 %d건 제거", item.category, dropped)
    return sources


def to_payload(item: CoverageItem, chunk_id_map: dict[str, str]) -> CoverageItemPayload:
    return CoverageItemPayload(
        title=cut(item.title, "title"),
        coverageStatus=db_enums.coverage_status(item.coverage_status),
        subtitle=cut(item.subtitle, "subtitle"),
        category=cut(item.category, "category"),
        limitLabel=cut(item.limit_label, "limit_label"),
        limitAmount=item.limit_amount,
        limitCurrency=cut(item.limit_currency, "limit_currency"),
        # conditions는 TEXT 컬럼이라 길이 제한이 없다
        conditions=item.conditions,
        detailItems=[
            DetailItemPayload(
                title=cut(d.title, "title"),
                subtitle=cut(d.subtitle, "subtitle"),
                isCovered=d.is_covered,
            )
            for d in item.detail_items
        ],
        subLimits=[
            SubLimitPayload(
                label=cut(s.label, "sub_limit_label"),
                value=cut(s.value, "sub_limit_value"),
                limitAmount=s.limit_amount,
                limitCurrency=cut(s.limit_currency, "limit_currency"),
                description=cut(s.description, "description"),
            )
            for s in item.sub_limits
        ],
        requiredDocuments=[
            RequiredDocumentPayload(
                documentName=cut(r.document_name, "document_name"),
                isMandatory=r.is_mandatory,
            )
            for r in item.required_documents
        ],
        exclusions=[
            ExclusionPayload(
                title=cut(e.title, "title"),
                description=e.description,
                sourceText=e.source_text,
                severity=db_enums.severity(e.severity),
            )
            for e in item.exclusions
        ],
        sources=_dedupe_sources(item, chunk_id_map),
    )
