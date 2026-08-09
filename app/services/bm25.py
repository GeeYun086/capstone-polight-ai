import math
import re
from collections import Counter

# BM25 표준 파라미터
K1 = 1.5
B = 0.75

# 한국어는 조사가 붙어 어절이 매번 달라진다("휴대품을/휴대품의/휴대품이").
# 형태소 분석기를 쓰면 정확하지만 의존성과 설치 부담이 생긴다.
#
# 처음에는 공백을 없애고 글자 bigram을 만들었는데, 어절 경계를 넘는 조각("에서 크게" -> "서크")이
# 대량으로 생겨 순위가 무너졌다(Recall@8이 83%에서 25%로 떨어졌다).
#
# 그래서 어절 안에서만, 그것도 앞에서부터 자르는 접두 n-gram을 쓴다.
# 한국어는 어간이 앞, 조사가 뒤에 붙으므로 접두를 잡으면 조사 변화에 자연히 강해진다.
#   "구조송환비용"   -> 구조 / 구조송 / 구조송환 / 구조송환비
#   "구조송환비용을" -> 같은 접두들이 나와 서로 매칭된다
WORD_RE = re.compile(r"[0-9A-Za-z가-힣]+")
MAX_PREFIX = 5


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for word in WORD_RE.findall(text):
        tokens.append(word)
        if word.isascii():
            continue
        for length in range(2, min(len(word), MAX_PREFIX) + 1):
            tokens.append(word[:length])
    return tokens


# 벡터 검색이 놓치는 것을 메우기 위한 키워드 검색.
#
# 임베딩은 의미가 비슷하면 잡지만, 약관처럼 고유 표현이 중요한 문서에서는
# "구조송환비용" 같은 단어가 그대로 들어있는 조항을 정확히 집어내는 능력이 따로 필요하다.
# (실측: "한국으로 이송" 질의가 벡터 검색만으로는 top-8에 들지 못했다)
class BM25Index:
    def __init__(self, documents: list[str]) -> None:
        self._doc_tokens = [tokenize(d) for d in documents]
        self._doc_len = [len(t) for t in self._doc_tokens]
        self._avg_len = (sum(self._doc_len) / len(self._doc_len)) if self._doc_len else 0.0

        self._term_freqs: list[Counter] = [Counter(t) for t in self._doc_tokens]

        # 각 토큰이 몇 개 문서에 나타나는지 (역문서빈도 계산용)
        doc_freq: Counter = Counter()
        for freqs in self._term_freqs:
            doc_freq.update(freqs.keys())

        total = len(documents)
        self._idf = {
            term: math.log((total - df + 0.5) / (df + 0.5) + 1.0) for term, df in doc_freq.items()
        }

    def scores(self, query: str) -> list[float]:
        query_terms = set(tokenize(query))
        scores = [0.0] * len(self._doc_tokens)

        for term in query_terms:
            idf = self._idf.get(term)
            if idf is None:
                continue
            for index, freqs in enumerate(self._term_freqs):
                freq = freqs.get(term)
                if not freq:
                    continue
                length_norm = 1 - B + B * (self._doc_len[index] / self._avg_len if self._avg_len else 1)
                scores[index] += idf * (freq * (K1 + 1)) / (freq + K1 * length_norm)

        return scores


# Reciprocal Rank Fusion.
#
# 벡터 점수와 BM25 점수는 스케일이 전혀 달라 그대로 더할 수 없다(코사인은 0~1, BM25는 상한 없음).
# RRF는 점수 대신 순위만 쓰기 때문에 스케일 정규화나 가중치 튜닝 없이 두 결과를 합칠 수 있다.
# k는 상위권 쏠림을 완화하는 상수로 60이 관례적으로 쓰인다.
def reciprocal_rank_fusion(rankings: list[list[int]], k: int = 60) -> list[int]:
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_index in enumerate(ranking, start=1):
            fused[doc_index] = fused.get(doc_index, 0.0) + 1.0 / (k + rank)

    return [doc for doc, _ in sorted(fused.items(), key=lambda item: -item[1])]
