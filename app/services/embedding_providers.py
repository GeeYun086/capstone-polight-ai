"""임베딩 벤더 추상화.

세 벤더를 같은 평가셋으로 비교하기 위한 얇은 어댑터다.
셋 다 OpenAI 호환 엔드포인트를 제공하므로 base_url만 바꿔 끼운다.

한 가지 주의할 점은 Upstage가 비대칭 임베딩을 쓴다는 것이다.
질문은 embedding-query로, 문서는 embedding-passage로 임베딩해야 한다.
같은 모델로 양쪽을 넣으면 벡터 공간이 어긋나 검색이 무의미해진다.
"""

from dataclasses import dataclass

from openai import OpenAI

from app.core.config import get_settings


@dataclass(frozen=True)
class EmbeddingProvider:
    name: str
    base_url: str | None
    api_key_field: str
    doc_model: str
    query_model: str
    # OpenAI v3 계열은 dimensions로 출력 차원을 줄일 수 있다.
    # 백엔드 DDL이 vector(1536)이라, 3-large를 1536으로 맞추면 스키마 변경 없이 쓸 수 있다.
    dimensions: int | None = None


PROVIDERS: dict[str, EmbeddingProvider] = {
    "openai-small": EmbeddingProvider(
        name="openai-small",
        base_url=None,
        api_key_field="openai_api_key",
        doc_model="text-embedding-3-small",
        query_model="text-embedding-3-small",
    ),
    # 3-large를 1536으로 축소한 것. 기본 3072보다 차원이 작으면서
    # 3-small보다 성능이 나은 경우가 있어 후보에 넣는다.
    "openai-large-1536": EmbeddingProvider(
        name="openai-large-1536",
        base_url=None,
        api_key_field="openai_api_key",
        doc_model="text-embedding-3-large",
        query_model="text-embedding-3-large",
        dimensions=1536,
    ),
    "upstage": EmbeddingProvider(
        name="upstage",
        base_url="https://api.upstage.ai/v1",
        api_key_field="upstage_api_key",
        doc_model="embedding-passage",
        query_model="embedding-query",
    ),
    # 기본 4096차원은 pgvector 인덱스로 만들 수 없다.
    # HNSW/IVFFlat은 vector 2,000 / halfvec 4,000까지만 지원한다.
    # 축소본이 품질을 유지하면 백엔드 DDL(vector(1536))을 그대로 쓸 수 있다.
    "upstage-1536": EmbeddingProvider(
        name="upstage-1536",
        base_url="https://api.upstage.ai/v1",
        api_key_field="upstage_api_key",
        doc_model="embedding-passage",
        query_model="embedding-query",
        dimensions=1536,
    ),
    "upstage-2000": EmbeddingProvider(
        name="upstage-2000",
        base_url="https://api.upstage.ai/v1",
        api_key_field="upstage_api_key",
        doc_model="embedding-passage",
        query_model="embedding-query",
        dimensions=2000,
    ),
    "qwen": EmbeddingProvider(
        name="qwen",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key_field="qwen_api_key",
        doc_model="text-embedding-v3",
        query_model="text-embedding-v3",
    ),
}


def get_provider(name: str) -> EmbeddingProvider:
    if name not in PROVIDERS:
        raise ValueError(f"알 수 없는 벤더: {name} (가능: {', '.join(PROVIDERS)})")
    return PROVIDERS[name]


def has_api_key(provider: EmbeddingProvider) -> bool:
    return bool(getattr(get_settings(), provider.api_key_field, ""))


def build_client(provider: EmbeddingProvider) -> OpenAI:
    api_key = getattr(get_settings(), provider.api_key_field, "")
    if not api_key:
        raise ValueError(f".env에 {provider.api_key_field.upper()}가 설정되지 않았습니다.")
    return OpenAI(api_key=api_key, base_url=provider.base_url)


def embed(
    provider: EmbeddingProvider,
    texts: list[str],
    is_query: bool,
    client: OpenAI | None = None,
) -> list[list[float]]:
    client = client or build_client(provider)
    model = provider.query_model if is_query else provider.doc_model

    kwargs: dict = {"input": texts, "model": model}
    if provider.dimensions:
        kwargs["dimensions"] = provider.dimensions

    response = client.embeddings.create(**kwargs)
    return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]
