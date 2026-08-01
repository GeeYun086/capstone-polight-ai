from openai import OpenAI

from app.core.config import get_settings
from scripts.embed_chunks import BATCH_SIZE, build_embed_text, embed_batch


# scripts/embed_chunks.py의 임베딩 로직을 그대로 재사용.
# 파일 입출력 없이 chunk 리스트를 받아 {chunk_id: vector} 딕셔너리로 바로 반환한다.
def embed_chunks(
    chunks: list[dict],
    client: OpenAI | None = None,
    model: str | None = None,
) -> dict[str, list[float]]:
    settings = get_settings()

    if client is None:
        if not settings.openai_api_key:
            raise ValueError(".env에 OPENAI_API_KEY가 설정되지 않았습니다.")
        client = OpenAI(api_key=settings.openai_api_key)

    model = model or settings.embedding_model

    results: dict[str, list[float]] = {}
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [build_embed_text(c) for c in batch]
        vectors = embed_batch(client, texts, model)
        for chunk, vector in zip(batch, vectors):
            results[chunk["chunk_id"]] = vector

    return results
