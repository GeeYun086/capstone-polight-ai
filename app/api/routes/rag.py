from fastapi import APIRouter

from app.schemas.rag import RagQueryRequest, RagQueryResponse, SourceChunk

router = APIRouter(tags=["rag"])


# TODO(pgvector 연결 후): 실제로는 pgvector 검색 + LLM 호출로 대체. 지금은 계약 검증용 더미 응답.
@router.post(
    "/rag/query",
    response_model=RagQueryResponse,
    summary="약관 질의응답 (stub)",
    description="사용자 질문에 대해 약관 근거와 함께 답변한다. "
    "pgvector 연결 전까지는 고정된 stub 응답을 반환한다.",
)
def query_policy(request: RagQueryRequest) -> RagQueryResponse:
    return RagQueryResponse(
        answer=f"[stub] '{request.question}'에 대한 답변은 아직 구현되지 않았습니다.",
        sources=[
            SourceChunk(
                chunk_id="stub-chunk-1",
                document_id="stub-document-1",
                page=1,
                quote="stub source quote",
            )
        ],
    )
