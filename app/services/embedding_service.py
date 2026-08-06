from openai import OpenAI

from app.core.config import get_settings
from scripts.embed_chunks import BATCH_SIZE, build_embed_text, embed_batch


def _resolve_client(client: OpenAI | None) -> OpenAI:
    if client is not None:
        return client

    settings = get_settings()
    if not settings.openai_api_key:
        raise ValueError(".env에 OPENAI_API_KEY가 설정되지 않았습니다.")
    return OpenAI(api_key=settings.openai_api_key)


# scripts/embed_chunks.py의 임베딩 로직을 그대로 재사용.
# 파일 입출력 없이 chunk 리스트를 받아 {chunk_id: vector} 딕셔너리로 바로 반환한다.
def embed_chunks(
    chunks: list[dict],
    client: OpenAI | None = None,
    model: str | None = None,
) -> dict[str, list[float]]:
    settings = get_settings()
    client = _resolve_client(client)
    model = model or settings.embedding_model

    results: dict[str, list[float]] = {}
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [build_embed_text(c) for c in batch]
        vectors = embed_batch(client, texts, model)
        for chunk, vector in zip(batch, vectors):
            results[chunk["chunk_id"]] = vector

    return results


# 질문 하나를 벡터로 변환한다 (파이프라인 B의 ⑤).
#
# 청크를 임베딩할 때와 반드시 같은 모델을 써야 한다. 모델이 다르면 벡터 공간이 달라져
# 코사인 유사도가 의미를 잃고 검색 결과가 무작위에 가까워진다.
# 그래서 기본값을 embed_chunks와 같은 settings.embedding_model로 둔다.
def embed_query(
    question: str,
    client: OpenAI | None = None,
    model: str | None = None,
) -> list[float]:
    settings = get_settings()
    client = _resolve_client(client)
    model = model or settings.embedding_model

    return embed_batch(client, [question], model)[0]
