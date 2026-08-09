import logging

from openai import OpenAI

from app.core.config import get_settings
from app.repositories.base import ChunkHit, VectorRepository
from app.schemas.rag import RagQueryRequest, RagQueryResponse, SourceChunk
from app.services.answer_providers import generate
from app.services.bm25 import reciprocal_rank_fusion
from app.services.embedding_service import embed_query
from app.services.prompt_builder import SYSTEM_PROMPT, build_user_message
from app.services.reranker import mmr_select

logger = logging.getLogger(__name__)

# 출처로 반환할 인용문 길이. 청크는 최대 2,000자라서 통째로 보내면 출처가 아니라 본문 덤프가 된다.
QUOTE_MAX_CHARS = 200

NO_EVIDENCE_ANSWER = "제공된 약관에서 관련 근거를 찾을 수 없습니다. 질문을 조금 더 구체적으로 적어주시거나, 해당 약관이 분석되었는지 확인해 주세요."


# 벡터 검색과 키워드 검색 결과를 합쳐 후보를 만든다.
#
# 임베딩은 "한국으로 이송"과 "구조송환비용"처럼 표현이 다른 경우에 강하고,
# BM25는 "구조송환비용"이라는 단어가 그대로 들어간 조항을 정확히 집는 데 강하다.
# 약관 질의는 일상 어휘와 약관 용어가 멀어서 어느 한쪽만으로는 놓치는 게 생긴다.
#
# 두 점수는 스케일이 달라 그대로 더할 수 없으므로 순위만 쓰는 RRF로 합친다.
def hybrid_search(
    repository: VectorRepository,
    query: str,
    query_vector: list[float],
    policy_id: str | None,
    top_k: int,
) -> list[ChunkHit]:
    dense = repository.search(query_vector, policy_id=policy_id, top_k=top_k)
    sparse = repository.search_text(query, policy_id=policy_id, top_k=top_k)

    if not sparse:
        return dense
    if not dense:
        return sparse

    # RRF는 인덱스 기반이라, 두 결과를 하나의 목록으로 모으고 chunk_id로 순위를 매긴다
    pool: dict[str, ChunkHit] = {}
    for hit in dense + sparse:
        pool.setdefault(hit.chunk_id, hit)

    ids = list(pool)
    position = {chunk_id: i for i, chunk_id in enumerate(ids)}
    rankings = [
        [position[h.chunk_id] for h in dense],
        [position[h.chunk_id] for h in sparse],
    ]

    fused = reciprocal_rank_fusion(rankings)

    # 융합 결과의 score를 RRF 값으로 통일한다.
    # 이걸 하지 않으면 BM25에서 온 항목은 score가 10~27, 벡터에서 온 항목은 0.4~0.5로
    # 스케일이 뒤섞여, 뒤이어 도는 MMR이 관련성 항을 잘못 계산해 BM25 쪽만 골라버린다.
    results = []
    for rank, index in enumerate(fused[:top_k], start=1):
        hit = pool[ids[index]]
        hit.score = 1.0 / rank
        results.append(hit)

    return results


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


# 답변 생성은 벤더 레지스트리를 거친다. .env의 answer_provider 한 줄로
# OpenAI / Claude / Gemini를 바꿔 끼울 수 있어야 비교 실험이 가능하다.
#
# client 인자는 테스트가 OpenAI 호출을 가로채는 용도로 남아 있다.
# 넘어오면 기존 경로를 그대로 쓰고, 없으면 레지스트리로 간다.
def _call_llm(user_message: str, client: OpenAI | None = None, model: str | None = None) -> str:
    if client is None:
        answer, _ = generate(SYSTEM_PROMPT, user_message)
        return answer

    response = client.chat.completions.create(
        model=model or get_settings().llm_model,
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

    # 후보를 top_k보다 넓게 뽑은 뒤 MMR로 줄인다.
    # 좁게 뽑으면 반복되는 표준 조항이 자리를 다 차지해 정답이 밀려난다.
    candidates = hybrid_search(
        repository,
        request.question,
        query_vector,
        policy_id=request.policy_id,
        top_k=settings.top_k * settings.mmr_candidate_multiplier,
    )

    if not candidates:
        return RagQueryResponse(answer=NO_EVIDENCE_ANSWER, sources=[])

    hits = mmr_select(candidates, top_k=settings.top_k, lambda_=settings.mmr_lambda)
    hits = attach_related_chunks(hits, repository)
    user_message = build_user_message(
        request.question,
        hits,
        contract_info=contract_info,
        history=history,
    )
    answer = _call_llm(user_message, client=client)

    return RagQueryResponse(answer=answer, sources=build_sources(hits))
