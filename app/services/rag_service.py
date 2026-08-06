import logging

from openai import OpenAI

from app.core.config import get_settings
from app.repositories.base import ChunkHit, VectorRepository
from app.schemas.rag import RagQueryRequest, RagQueryResponse, SourceChunk
from app.services.embedding_service import embed_query
from app.services.prompt_builder import SYSTEM_PROMPT, build_user_message

logger = logging.getLogger(__name__)

# 출처로 반환할 인용문 길이. 청크는 최대 2,000자라서 통째로 보내면 출처가 아니라 본문 덤프가 된다.
QUOTE_MAX_CHARS = 200

NO_EVIDENCE_ANSWER = "제공된 약관에서 관련 근거를 찾을 수 없습니다. 질문을 조금 더 구체적으로 적어주시거나, 해당 약관이 분석되었는지 확인해 주세요."


# 검색된 보장 조항에 짝지어진 면책 조항을 끌어와 붙인다.
#
# 이 프로젝트 RAG의 핵심이다. "항공기 지연되면 보상되나요?" 같은 질문에서 유사도 검색은
# 보상 조항만 상위에 올린다. 면책 조항을 함께 주지 않으면 LLM이 예외를 모른 채
# "보상됩니다"라고 답한다. chunk_policy.py가 related_chunk_id로 미리 연결해둔 짝을 여기서 쓴다.
def attach_related_chunks(hits: list[ChunkHit], repository: VectorRepository) -> list[ChunkHit]:
    present = {hit.chunk_id for hit in hits}
    wanted = [
        hit.related_chunk_id
        for hit in hits
        if hit.related_chunk_id and hit.related_chunk_id not in present
    ]
    if not wanted:
        return hits

    # 중복 제거하되 순서는 유지
    unique = list(dict.fromkeys(wanted))
    related = repository.get_by_ids(unique)
    if related:
        logger.info("면책 조항 %d개를 근거에 추가했습니다.", len(related))

    return hits + related


# 출처는 LLM 출력에서 뽑지 않고 실제 검색된 청크에서 만든다.
# LLM에게 인용문을 생성시키면 원문에 없는 문장을 만들어낼 수 있고, 보험 답변에서
# 근거가 조작되면 서비스 신뢰가 무너진다. 원문을 그대로 잘라 쓰면 인용의 진위가 보장된다.
def build_sources(hits: list[ChunkHit]) -> list[SourceChunk]:
    sources = []
    for hit in hits:
        quote = hit.text[:QUOTE_MAX_CHARS]
        if len(hit.text) > QUOTE_MAX_CHARS:
            quote += "..."
        sources.append(
            SourceChunk(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                # SourceChunk.page는 단일 int이므로 조항이 시작되는 페이지를 보낸다.
                # 사용자가 약관에서 조항을 찾을 때 기준이 되는 페이지다.
                page=hit.page_start,
                quote=quote,
            )
        )
    return sources


def _call_llm(user_message: str, client: OpenAI | None = None, model: str | None = None) -> str:
    settings = get_settings()

    if client is None:
        if not settings.openai_api_key:
            raise ValueError(".env에 OPENAI_API_KEY가 설정되지 않았습니다.")
        client = OpenAI(api_key=settings.openai_api_key)

    response = client.chat.completions.create(
        model=model or settings.llm_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        # 약관 해석은 표현이 흔들리면 안 되므로 결정적으로 생성한다
        temperature=0,
    )
    return (response.choices[0].message.content or "").strip()


# POST /internal/rag/query 진입점 (파이프라인 B의 ⑤~⑩).
#
# contract_info(가입 담보)와 history(대화 맥락)는 아직 RagQueryRequest에 필드가 없어
# 항상 None으로 전달된다. Spring과 스키마 합의가 끝나면 값만 채우면 된다.
def answer_question(
    request: RagQueryRequest,
    repository: VectorRepository,
    client: OpenAI | None = None,
    contract_info: dict | None = None,
    history: list[dict] | None = None,
) -> RagQueryResponse:
    settings = get_settings()

    query_vector = embed_query(request.question, client=client)
    hits = repository.search(
        query_vector,
        policy_id=request.policy_id,
        top_k=settings.top_k,
    )

    if not hits:
        return RagQueryResponse(answer=NO_EVIDENCE_ANSWER, sources=[])

    hits = attach_related_chunks(hits, repository)
    user_message = build_user_message(
        request.question,
        hits,
        contract_info=contract_info,
        history=history,
    )
    answer = _call_llm(user_message, client=client)

    return RagQueryResponse(answer=answer, sources=build_sources(hits))
