from fastapi import APIRouter, Depends

from app.repositories import VectorRepository, get_vector_repository
from app.schemas.rag import RagQueryRequest, RagQueryResponse
from app.services.rag_service import answer_question

router = APIRouter(tags=["rag"])


# 저장소는 Depends로만 받는다. pgvector로 갈아탈 때 get_vector_repository() 안에서
# 구현체만 바꾸면 되고, 이 라우터와 rag_service는 수정할 필요가 없다.
@router.post(
    "/rag/query",
    response_model=RagQueryResponse,
    summary="약관 질의응답",
    description="질문을 임베딩해 해당 계약(policyId)의 약관 청크를 검색하고, "
    "보장 조항에 짝지어진 면책 조항을 함께 근거로 넣어 답변을 생성한다. "
    "응답의 sources는 LLM이 생성한 문장이 아니라 검색된 원문에서 잘라낸 인용이다.",
)
def query_policy(
    request: RagQueryRequest,
    repository: VectorRepository = Depends(get_vector_repository),
) -> RagQueryResponse:
    return answer_question(request, repository)
