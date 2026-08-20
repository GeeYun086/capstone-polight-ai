import logging

from openai import OpenAI

from app.core.config import get_settings
from app.repositories.base import ChunkHit, SearchScope, VectorRepository
from app.schemas.rag import RagQueryRequest, RagQueryResponse, SourceChunk
from app.services.answer_providers import generate
from app.services.bm25 import reciprocal_rank_fusion
from app.services.embedding_service import embed_query
from app.services.prompt_builder import SYSTEM_PROMPT, build_user_message
from app.services.query_rewriter import rewrite
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
    scope: SearchScope | None,
    top_k: int,
) -> list[ChunkHit]:
    dense = repository.search(query_vector, scope=scope, top_k=top_k)
    sparse = repository.search_text(query, scope=scope, top_k=top_k)

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
# history는 요청에서 받는다. Spring이 chat_messages에서 최근 3턴을 잘라 실어 보낸다.
# contract_info(가입 담보)는 아직 자리만 열려 있다. 증권 정보를 수집하는 화면이 없어
# policies 테이블에 데이터가 없기 때문이며, MVP 범위 밖으로 합의됐다.
def answer_question(
    request: RagQueryRequest,
    repository: VectorRepository,
    client: OpenAI | None = None,
    contract_info: dict | None = None,
    history: list[dict] | None = None,
) -> RagQueryResponse:
    settings = get_settings()

    # 인자로 받은 history가 있으면 그것을 쓰고(테스트용), 없으면 요청에 실린 것을 쓴다
    turns = history if history is not None else [
        {"role": "user" if t.sender == "USER" else "assistant", "content": t.content}
        for t in request.history
    ]

    # 검색에 쓸 질문과 답변에 쓸 질문을 나눈다.
    #
    # "그럼 얼마까지 나와요?"로는 검색이 안 된다. 항공기도 지연도 없는 문장이라
    # 벡터 검색이 엉뚱한 조항을 가져오고, 이력을 프롬프트에 넣어도 근거가 이미 틀렸다.
    # 검색 전에 "항공기 지연 보상 한도는?"처럼 독립적인 문장으로 바꾼다.
    #
    # 다만 LLM에게는 원문을 그대로 준다. 재작성된 문장은 검색용으로 다듬은 것이라
    # 사용자의 말투와 뉘앙스가 지워져 있고, 답변이 질문과 겉도는 느낌을 준다.
    search_query = rewrite(request.question, turns)

    query_vector = embed_query(search_query, client=client)

    # 후보를 top_k보다 넓게 뽑은 뒤 MMR로 줄인다.
    # 좁게 뽑으면 반복되는 표준 조항이 자리를 다 차지해 정답이 밀려난다.
    pool = settings.top_k * settings.mmr_candidate_multiplier
    base_scope = SearchScope(document_id=request.document_id, trip_id=request.trip_id)

    # 증권에서 온 특약명이 있으면 그 조항들로 좁혀서 먼저 찾는다.
    # 요청에 실려 오지 않으면(clause_paths가 없으면) 이 블록은 통째로 건너뛰고
    # 기존과 완전히 동일하게 동작한다.
    candidates: list[ChunkHit] = []
    if request.clause_paths:
        narrowed = SearchScope(
            document_id=request.document_id,
            trip_id=request.trip_id,
            clause_paths=tuple(request.clause_paths),
        )
        candidates = hybrid_search(repository, search_query, query_vector, scope=narrowed, top_k=pool)

        # 좁힌 결과에 "실제 특약 조각"이 하나도 없으면 필터를 버리고 다시 찾는다.
        #
        # 개수로 판정하면 안 된다. 필터는 clause_path가 빈 공통 조항(청구 절차·용어 정의·
        # 일반 면책)을 항상 통과시키는데, db_travel 기준 그것만 34청크다. 그래서 정답이 든
        # 특약이 통째로 배제된 상황에서도 결과 수는 늘 top_k를 넘어 폴백이 영영 발동하지
        # 않는다. 실측에서 25문항 전부 폴백 0건이었고, 최소형 증권에서 Recall@8이
        # 88%->48%로 떨어지는데도 안전장치가 돌지 않았다.
        #
        # 살아남은 특약 조각 수로 판정해야 "이 사용자의 담보에는 근거가 없다"를 감지한다.
        clause_hits = sum(1 for c in candidates if c.clause_path)
        if clause_hits == 0:
            logger.info(
                "특약 필터를 통과한 조항이 없어 전체 검색으로 되돌립니다 (특약 %d개, 후보 %d건)",
                len(request.clause_paths), len(candidates),
            )
            candidates = []

    if not candidates:
        candidates = hybrid_search(repository, search_query, query_vector, scope=base_scope, top_k=pool)

    if not candidates:
        return RagQueryResponse(answer=NO_EVIDENCE_ANSWER, sources=[])

    hits = mmr_select(candidates, top_k=settings.top_k, lambda_=settings.mmr_lambda)
    hits = attach_related_chunks(hits, repository)

    # 증권 담보가 실려 왔으면 프롬프트에 넣는다.
    #
    # 이게 증권 연동의 핵심이다. 약관에는 그 상품이 팔 수 있는 모든 특약이 있어서,
    # 가입하지 않은 담보를 물어도 조항이 검색되고 "보상됩니다"라는 틀린 답이 나간다.
    # 한도 금액도 약관에는 "보험가입금액을 한도로"라고만 적혀 있어 답할 수 없다.
    #
    # 인자로 받은 contract_info가 우선한다(테스트에서 직접 넣는 경로).
    if contract_info is None and request.coverages:
        contract_info = {
            "coverages": [c.model_dump(by_alias=True) for c in request.coverages],
            # 목록이 증권 전체인지. 이게 있어야 "목록에 없는 담보"를 미가입으로
            # 답할지 모른다고 답할지 정해진다.
            "complete": request.coverages_complete,
        }

    user_message = build_user_message(
        request.question,
        hits,
        contract_info=contract_info,
        history=turns,
    )
    answer = _call_llm(user_message, client=client)

    return RagQueryResponse(answer=answer, sources=build_sources(hits))
