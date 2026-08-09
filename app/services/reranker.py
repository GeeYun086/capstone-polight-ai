import logging

import numpy as np

from app.repositories.base import ChunkHit

logger = logging.getLogger(__name__)


# MMR(Maximal Marginal Relevance) 재순위.
#
# 왜 필요한가: 약관은 같은 표준 조항이 보통약관과 여러 특약에 걸쳐 반복된다.
# 유사도만으로 top-k를 뽑으면 그 반복본들이 자리를 다 차지한다.
# 실측(db_travel, "임신·출산 치료비" 질의): top-8이 사실상 3종류였고
# 정답 청크는 15위로 밀려 컨텍스트에 들어가지 못했다. LLM은 받은 근거만 보고
# "언급 없음"이라 답했다 — 검색이 원인이지 생성이 원인이 아니었다.
#
# MMR은 "질문과의 관련성"과 "이미 고른 것들과의 차별성"을 함께 보고 고른다.
#   점수 = lambda * 관련성 - (1 - lambda) * (이미 고른 것 중 최대 유사도)
# lambda가 1에 가까우면 순수 유사도 정렬, 0에 가까우면 다양성만 본다.
def mmr_select(
    hits: list[ChunkHit],
    top_k: int,
    lambda_: float = 0.6,
) -> list[ChunkHit]:
    if len(hits) <= top_k:
        return hits

    # 임베딩이 없으면 청크 간 유사도를 계산할 수 없으므로 원래 순서를 그대로 쓴다.
    # (저장소 구현체가 embedding을 채우지 않는 경우에 대한 안전장치)
    if any(h.embedding is None for h in hits):
        logger.warning("임베딩이 없는 검색 결과가 있어 MMR을 건너뜁니다.")
        return hits[:top_k]

    # 저장소가 정규화된 벡터를 넘겨주므로 내적이 곧 코사인 유사도가 된다.
    matrix = np.asarray([h.embedding for h in hits], dtype=np.float32)
    relevance = np.asarray([h.score for h in hits], dtype=np.float32)

    selected: list[int] = []
    remaining = list(range(len(hits)))

    # 첫 번째는 가장 관련성 높은 것 (비교 대상이 없어 다양성 항이 0)
    first = int(np.argmax(relevance))
    selected.append(first)
    remaining.remove(first)

    while len(selected) < top_k and remaining:
        # 이미 고른 것들과의 유사도 중 최대값이 "얼마나 겹치는가"가 된다
        redundancy = matrix[remaining] @ matrix[selected].T
        max_redundancy = redundancy.max(axis=1)

        scores = lambda_ * relevance[remaining] - (1 - lambda_) * max_redundancy
        best = remaining[int(np.argmax(scores))]

        selected.append(best)
        remaining.remove(best)

    return [hits[i] for i in selected]
