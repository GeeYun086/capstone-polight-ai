"""하이브리드 검색(BM25 + 벡터, RRF) 회귀 테스트.

두 번 크게 틀렸던 부분이라 테스트로 고정한다.

1. 어절 경계를 넘는 글자 bigram("에서 크게" -> "서크")이 대량 생성돼 순위가 무너졌다.
   Recall@8이 83%에서 25%로 떨어졌다.
2. 융합 후 score를 통일하지 않아 BM25 점수(10~27)와 코사인(0.4~0.5)이 섞였고,
   뒤이어 도는 MMR이 관련성 항을 잘못 계산해 BM25 결과만 골랐다.
"""

from app.repositories.base import ChunkHit
from app.services.bm25 import BM25Index, reciprocal_rank_fusion, tokenize
from app.services.rag_service import hybrid_search
from tests.conftest import FakeVectorRepository, make_hit


# 결함 1: 어절 경계를 넘는 조각이 나오면 안 된다.
def test_tokenize_does_not_cross_word_boundaries():
    tokens = tokenize("해외에서 크게")

    assert "서크" not in tokens, "어절 경계를 넘는 bigram이 생성됐다"
    assert "해외" in tokens


# 한국어는 조사가 뒤에 붙으므로 접두를 잡으면 조사 변화에 강해진다.
def test_tokenize_survives_korean_particles():
    base = set(tokenize("구조송환비용"))
    inflected = set(tokenize("구조송환비용을"))

    assert base & inflected, "조사가 붙으면 공통 토큰이 하나도 없다"
    assert "구조송환" in base & inflected


def test_bm25_ranks_exact_term_higher():
    index = BM25Index(
        [
            "회사는 중대사고 구조송환비용을 보상합니다",
            "회사는 휴대품 도난 손해를 보상합니다",
            "이 특별약관에 정하지 않은 사항은 보통약관을 따릅니다",
        ]
    )

    scores = index.scores("구조송환비용")

    assert scores[0] == max(scores)
    assert scores[2] == 0, "질의어가 없는 문서는 0점이어야 한다"


def test_rrf_prefers_documents_ranked_well_by_both():
    # 0번은 양쪽에서 상위, 1번과 2번은 한쪽에서만 상위
    fused = reciprocal_rank_fusion([[0, 1, 2], [0, 2, 1]])

    assert fused[0] == 0


# 결함 2: 융합 후에는 score 스케일이 하나로 통일돼야 한다.
def test_hybrid_normalizes_scores_after_fusion():
    repo = FakeVectorRepository()
    dense = make_hit("dense-1")
    dense.score = 0.42
    sparse = make_hit("sparse-1")
    sparse.score = 27.3  # BM25 점수는 상한이 없다
    repo.hits = [dense]
    repo.text_hits = [sparse]

    results = hybrid_search(repo, "질의", [0.1] * 8, policy_id=None, top_k=8)

    assert len(results) == 2
    for hit in results:
        assert 0 < hit.score <= 1.0, f"융합 후 score가 통일되지 않았다: {hit.score}"


# 키워드 검색이 비면 벡터 결과를 그대로 쓴다 (저장소가 전문검색을 지원하지 않는 경우).
def test_hybrid_falls_back_to_dense_when_no_keyword_hits() -> None:
    repo = FakeVectorRepository()
    repo.hits = [make_hit("a"), make_hit("b")]
    repo.text_hits = []

    results = hybrid_search(repo, "질의", [0.1] * 8, policy_id=None, top_k=8)

    assert [h.chunk_id for h in results] == ["a", "b"]
