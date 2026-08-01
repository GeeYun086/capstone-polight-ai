from app.schemas.base import CamelModel


class SourceChunk(CamelModel):
    chunk_id: str
    document_id: str
    page: int
    quote: str


# POST /internal/rag/query 요청 바디
class RagQueryRequest(CamelModel):
    user_id: str
    trip_id: str
    policy_id: str
    question: str


# POST /internal/rag/query 응답 바디
class RagQueryResponse(CamelModel):
    answer: str
    sources: list[SourceChunk]
