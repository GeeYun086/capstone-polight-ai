from functools import lru_cache

from app.core.config import get_settings
from app.repositories.base import ChunkHit, ChunkScope, VectorRepository
from app.repositories.file_repository import FileVectorRepository

__all__ = ["ChunkHit", "ChunkScope", "VectorRepository", "get_vector_repository"]


# 저장소 구현체를 고르는 단일 지점. 라우터는 Depends(get_vector_repository)로만 받아가므로,
# pgvector가 붙으면 이 함수 안에 분기 하나를 추가하는 것으로 전환이 끝난다.
#
# lru_cache로 인스턴스를 재사용한다. FileVectorRepository는 첫 검색 때 임베딩 전체를
# numpy 행렬로 올리기 때문에, 요청마다 새로 만들면 매번 파일을 다시 읽게 된다.
@lru_cache
def get_vector_repository() -> VectorRepository:
    settings = get_settings()

    if settings.database_url:
        # TODO(pgvector): Spring이 policy_chunks 마이그레이션을 올리면 PgVectorRepository로 교체.
        # 인터페이스가 같으므로 이 위쪽 코드(rag_service, analysis_service, 라우터)는 수정 불필요.
        raise NotImplementedError(
            "DATABASE_URL이 설정됐지만 PgVectorRepository가 아직 구현되지 않았습니다. "
            "로컬 파일 저장소를 쓰려면 DATABASE_URL을 비워두세요."
        )

    return FileVectorRepository()
