"""MMR 재순위 회귀 테스트.

실측 배경: db_travel에 "임신·출산 치료비" 질의를 했을 때 top-8이 사실상 3종류의
반복 조항으로 채워지고 정답이 15위로 밀려, LLM이 "언급 없음"이라고 답했다.
약관은 같은 표준 조항이 보통약관과 여러 특약에 반복되기 때문에 유사도 정렬만으로는
top-k가 중복본에 잠식된다.
"""

import math

from app.repositories.base import ChunkHit
from app.services.reranker import mmr_select


def make_hit(chunk_id: str, score: float, embedding: list[float] | None) -> ChunkHit:
    return ChunkHit(
        chunk_id=chunk_id,
        document_id="doc-1",
        page_start=1,
        page_end=1,
        section_title=chunk_id,
        coverage_type="included",
        text="본문",
        matched_category=None,
        related_chunk_id=None,
        score=score,
        embedding=embedding,
    )


def unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


# 핵심: 점수가 높아도 서로 거의 같은 청크로 top-k를 채우면 안 된다.
def test_mmr_breaks_near_duplicate_cluster():
    # dup1~dup3는 사실상 같은 방향(중복 조항), unique는 다른 방향이지만 점수가 낮다
    hits = [
        make_hit("dup1", 0.90, unit([1.0, 0.0, 0.0])),
        make_hit("dup2", 0.89, unit([0.99, 0.01, 0.0])),
        make_hit("dup3", 0.88, unit([0.98, 0.02, 0.0])),
        make_hit("unique", 0.60, unit([0.0, 0.0, 1.0])),
    ]

    selected = [h.chunk_id for h in mmr_select(hits, top_k=2, lambda_=0.6)]

    assert selected[0] == "dup1", "가장 관련성 높은 것이 먼저 와야 한다"
    assert "unique" in selected, "중복본 대신 다른 내용이 들어와야 한다"


# lambda=1이면 다양성을 무시하므로 순수 유사도 정렬과 같아야 한다.
def test_lambda_one_falls_back_to_pure_relevance():
    hits = [
        make_hit("a", 0.90, unit([1.0, 0.0])),
        make_hit("b", 0.85, unit([0.99, 0.01])),
        make_hit("c", 0.50, unit([0.0, 1.0])),
    ]

    selected = [h.chunk_id for h in mmr_select(hits, top_k=2, lambda_=1.0)]

    assert selected == ["a", "b"]


def test_returns_input_when_not_enough_candidates():
    hits = [make_hit("a", 0.9, unit([1.0, 0.0])), make_hit("b", 0.8, unit([0.0, 1.0]))]

    assert mmr_select(hits, top_k=5) == hits


# 임베딩을 채우지 않는 저장소 구현체(테스트용 fake 등)에서도 죽지 않아야 한다.
def test_missing_embedding_degrades_gracefully():
    hits = [
        make_hit("a", 0.9, None),
        make_hit("b", 0.8, None),
        make_hit("c", 0.7, None),
    ]

    selected = mmr_select(hits, top_k=2)

    assert [h.chunk_id for h in selected] == ["a", "b"]
