from openai import OpenAI

from app.core.config import get_settings
from app.services.embedding_providers import build_client, embed, get_provider
from scripts.embed_chunks import BATCH_SIZE, build_embed_text


# 벤더는 settings.embedding_provider로 고른다.
#
# 비교 실험(db_travel, 12문항) 결과 upstage-1536을 채택했다.
#   openai-small       MRR 0.5147
#   openai-large-1536  MRR 0.5744
#   upstage-1536       MRR 0.7875
# Upstage 기본 출력은 4096차원인데 pgvector 인덱스 한계(vector 2000/halfvec 4000)를
# 넘어서므로 dimensions=1536으로 줄였다. 품질은 4096과 동일했고,
# 백엔드 policy_chunks.embedding vector(1536)을 그대로 쓸 수 있다.
def _provider():
    return get_provider(get_settings().embedding_provider)


# 청크를 임베딩한다 (문서 쪽).
#
# Upstage는 질의와 문서에 서로 다른 모델을 쓰는 비대칭 임베딩이라,
# 여기서는 반드시 is_query=False로 호출해야 한다. 섞으면 벡터 공간이 어긋나
# 검색이 사실상 무작위가 된다.
def embed_chunks(
    chunks: list[dict],
    client: OpenAI | None = None,
    model: str | None = None,
) -> dict[str, list[float]]:
    provider = _provider()
    client = client or build_client(provider)

    results: dict[str, list[float]] = {}
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [build_embed_text(c) for c in batch]
        vectors = embed(provider, texts, is_query=False, client=client)
        for chunk, vector in zip(batch, vectors):
            results[chunk["chunk_id"]] = vector

    return results


# 질문 하나를 벡터로 변환한다 (질의 쪽).
def embed_query(
    question: str,
    client: OpenAI | None = None,
    model: str | None = None,
) -> list[float]:
    provider = _provider()
    client = client or build_client(provider)
    return embed(provider, [question], is_query=True, client=client)[0]
