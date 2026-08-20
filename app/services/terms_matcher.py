"""증권에 적힌 보험사·상품명으로 약관을 찾는다.

⚠️ 이 모듈은 요청 경로에서 호출하지 않는다. 증권과 약관을 잇는 것은 백엔드 몫이다.
약관 메타데이터가 백엔드 DB에 있으므로, 조회는 DB를 아는 쪽이 하는 것이 맞다.
연동 계약은 docs/BACKEND_INTERFACE.md의 3-1을 참고.

그런데도 이 코드가 있는 이유는 두 가지다.

  로컬 개발·평가용   백엔드 DB 없이 "이 증권이 어느 약관인가"를 알 방법이 없으면
                    실제 증권으로 챗봇을 돌려볼 수도, 평가를 돌릴 수도 없다.
  백엔드 참조 구현    아래 4단계 폴백 규칙을 말로 설명하는 것보다 도는 코드를 보여주는
                    편이 빠르다. 실제로 단순 문자열 일치로는 첫 증권부터 실패한다는
                    것을 이 코드로 발견했다.

백엔드가 구현하면 이 모듈의 역할은 끝난다. 그때 지우든 평가용으로 남기든 무해하다.

--

증권 분석 결과가 담보와 금액을 주지만, 보상 조건·면책·청구 절차는 약관에만 있다.
그래서 "이 증권이 어느 약관인가"를 정해야 챗봇이 근거를 댈 수 있다.

3단으로 내려가는 이유는, 매칭 실패가 서비스 중단이 되면 안 되기 때문이다.
보장 카드는 증권에서 나오므로 약관이 없어도 화면은 뜬다. 약관이 필요한 것은
상세 내역과 챗봇뿐이라, 기능을 줄이더라도 동작은 유지하는 편이 낫다.

  EXACT      보험사 + 상품명이 맞았다. 개정판까지 맞으면 가장 좋다
  REVISION   같은 상품인데 개정판이 다르다. 조항이 바뀌었을 수 있어 화면에 알린다
  INSURER    같은 보험사의 다른 상품뿐이다. 표준 조항은 대개 비슷하지만 특약은 다르다
  NONE       그 보험사 약관이 아예 없다. 증권 정보만으로 답하고 원문 확인 불가를 알린다

수집 대상을 정하는 데도 쓴다. NONE이 쌓인 보험사가 곧 다음에 모아야 할 약관이다.
"""

import json
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "config" / "terms_registry.json"

# 보험사명은 표기 흔들림이 심하다. "DB손해보험(주)", "디비손해보험", "DB손보" 모두 같은 회사다.
# 이름이 짧아 유사도만으로는 다른 회사와 헷갈릴 수 있어 기준을 높게 둔다.
INSURER_THRESHOLD = 0.7

# 상품명 기준.
#
# 0.55로 뒀다가 "우리아이 여행보험"이 "해외여행보험"으로 잡혔다. 여행자보험 상품명은
# 대부분 "여행보험"으로 끝나서, 공통 접미사만으로도 유사도가 0.57까지 올라간다.
# 다른 상품을 같은 것으로 보면 엉뚱한 특약을 근거로 답하게 되므로 기준을 올렸다.
#
# 축약 표기는 이 값과 무관하게 통과한다. 한쪽이 다른 쪽을 포함하면(similarity가
# 1.0을 주는 경로) 임계값을 타지 않기 때문이다.
#   "프로미 해외여행보험"      in "프로미 해외여행보험Ⅰ"      -> 1.0
#   "프로미 해외여행보험Ⅰ"     in "프로미해외여행보험 Ⅰ형"    -> 1.0
PRODUCT_THRESHOLD = 0.65

# 보험사명에서 떼어낼 접미사. 회사의 정체와 무관하다.
INSURER_SUFFIXES = ("주식회사", "해상보험", "손해보험", "화재해상", "손보", "화재", "(주)")


@dataclass(frozen=True)
class TermsMatch:
    terms_id: str
    insurer: str
    product: str
    revision: str | None
    # EXACT / REVISION / INSURER / NONE
    level: str
    score: float

    @property
    def is_usable(self) -> bool:
        return self.level != "NONE"

    # 화면에 띄울 안내. 근거의 신뢰도를 사용자가 알아야 한다.
    @property
    def notice(self) -> str | None:
        if self.level == "EXACT":
            return None
        if self.level == "REVISION":
            return f"가입하신 시점과 다른 개정판({self.revision}) 약관을 참고했습니다. 조항이 다를 수 있습니다."
        if self.level == "INSURER":
            return f"동일 상품 약관이 없어 같은 보험사의 다른 상품({self.product}) 약관을 참고했습니다."
        return "해당 약관을 보유하고 있지 않아 증권에 적힌 내용만으로 안내했습니다."


NONE_MATCH = TermsMatch("", "", "", None, "NONE", 0.0)


def normalize(text: str | None) -> str:
    text = re.sub(r"[()（）\[\]·・,，.\-]", "", text or "")
    return re.sub(r"\s+", "", text).lower()


def _strip_insurer_suffix(text: str) -> str:
    for suffix in INSURER_SUFFIXES:
        normalized = normalize(suffix)
        if normalized and text.endswith(normalized):
            text = text[: -len(normalized)]
    return text


def similarity(a: str, b: str, strip_insurer: bool = False) -> float:
    na, nb = normalize(a), normalize(b)
    if strip_insurer:
        na, nb = _strip_insurer_suffix(na), _strip_insurer_suffix(nb)
    if not na or not nb:
        return 0.0
    # 완전히 같은 것과 한쪽이 포함된 것을 구분한다.
    #
    # 둘 다 1.0을 주면 증권의 "해외여행보험"이 "해외여행보험"과 "다이렉트 해외여행보험"에
    # 동점이 되어, 어느 약관이 뽑히는지가 레지스트리에 적은 순서로 결정된다.
    # 상품을 하나 추가했을 뿐인데 기존 증권의 매칭이 바뀌는 일이 생긴다.
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.95
    return SequenceMatcher(None, na, nb).ratio()


def load_registry(path: Path | None = None) -> list[dict]:
    """수집해둔 약관 목록. 지금은 파일이지만 policy_terms 테이블로 옮겨가면 여기만 바꾼다."""
    with (path or DEFAULT_REGISTRY_PATH).open("r", encoding="utf-8") as f:
        return json.load(f)["terms"]


def _product_score(product: str, entry: dict) -> float:
    """상품명 유사도. aliases 중 가장 잘 맞는 것을 쓴다."""
    candidates = [entry["product"], *entry.get("aliases", [])]
    return max(similarity(product, c) for c in candidates)


def find_terms(
    insurer: str,
    product: str,
    revision: str | None = None,
    registry: list[dict] | None = None,
) -> TermsMatch:
    """증권의 보험사·상품명으로 약관을 고른다. 못 찾아도 예외를 내지 않고 NONE을 돌려준다."""
    entries = registry if registry is not None else load_registry()

    # 증권에 적힌 회사와 약관을 낸 회사가 다를 수 있다.
    #
    # 제휴 판매에서는 증권에 인수사가 따로 표기된다. 실제로 마이뱅크가 파는 상품의
    # 증권에는 "한화손해보험(주)"가 적혀 있는데 약관은 캐롯 해외여행보험이었다.
    # 회사명만 비교하면 NONE이 나와, 담보가 다 대응하는 약관을 두고도 못 쓴다.
    def matches_insurer(entry: dict) -> bool:
        names = [entry["insurer"], *entry.get("underwriter_aliases", [])]
        return any(similarity(insurer, n, strip_insurer=True) >= INSURER_THRESHOLD for n in names)

    same_insurer = [e for e in entries if matches_insurer(e)]
    if not same_insurer:
        logger.info("약관을 보유하지 않은 보험사입니다: %s (%s)", insurer, product)
        return NONE_MATCH

    scored = sorted(
        ((e, _product_score(product, e)) for e in same_insurer),
        key=lambda pair: -pair[1],
    )
    best, score = scored[0]

    if score < PRODUCT_THRESHOLD:
        # 상품은 못 찾았지만 보험사는 맞다. 표준 조항이라도 있는 편이 낫다.
        # 최신 개정판을 고른다.
        fallback = max(same_insurer, key=lambda e: e.get("revision") or "")
        logger.info(
            "상품 약관을 못 찾아 같은 보험사의 다른 약관을 씁니다: %s %s -> %s",
            insurer, product, fallback["product"],
        )
        return TermsMatch(
            fallback["id"], fallback["insurer"], fallback["product"],
            fallback.get("revision"), "INSURER", score,
        )

    # 상품이 맞았다. 개정판까지 맞는지 본다.
    #
    # revision을 안 주면 EXACT로 본다. 증권에 개정일이 없는 경우가 흔한데,
    # 그때마다 "개정판이 다를 수 있다"고 알리면 경고가 무뎌진다.
    if revision and best.get("revision") and revision != best["revision"]:
        same_product = [
            e for e in same_insurer if _product_score(product, e) >= PRODUCT_THRESHOLD
        ]
        exact = next((e for e in same_product if e.get("revision") == revision), None)
        if exact:
            return TermsMatch(
                exact["id"], exact["insurer"], exact["product"],
                exact.get("revision"), "EXACT", 1.0,
            )
        logger.info(
            "개정판이 다릅니다: 증권 %s / 보유 %s (%s)", revision, best.get("revision"), best["product"]
        )
        return TermsMatch(
            best["id"], best["insurer"], best["product"], best.get("revision"), "REVISION", score
        )

    return TermsMatch(
        best["id"], best["insurer"], best["product"], best.get("revision"), "EXACT", score
    )
