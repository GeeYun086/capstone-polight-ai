"""증권에 적힌 담보명을 약관의 특약명(clause_path)으로 잇는다.

왜 특약명인가: 증권과 약관은 같은 상품의 문서라 어휘가 일치한다. 증권의
"해외여행중 휴대품손해(분실제외)"는 약관에 "해외여행중 휴대품손해(분실제외) 특별약관"으로
그대로 있다. 8개 표준 카테고리로 번역해 좁히는 것보다 훨씬 세밀하다.

    db_travel 기준
      카테고리 medical_expense 로 좁힘                          76청크
      특약명 "비급여 도수치료·체외충격파치료·증식치료 …" 로 좁힘     7청크

증권에 도수치료만 있는 사용자에게 나머지 69청크는 방해물이다.

3단 폴백을 두는 이유: 이름 매칭은 실패할 수 있다(보험사가 증권에 축약해 적는 경우).
실패한 채로 필터를 걸면 검색 결과가 비어 답을 못 한다. 그래서 매칭이 부실하면
필터를 아예 포기하고 기존 동작으로 돌아간다. 새 기능이 기존 성능을 깎지 못하게 하는 장치다.
"""

import logging
import re
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# 이 정도 닮으면 같은 담보로 본다.
# 증권은 "특별약관"·"추가특별약관" 같은 꼬리표를 떼고 적는 경우가 많아 여유를 둔다.
SIMILARITY_THRESHOLD = 0.55

# 비교 전에 떼어낼 꼬리표. 이게 붙고 안 붙고는 담보의 정체와 무관하다.
SUFFIXES = ("추가특별약관", "특별약관", "보통약관", "특약", "약관")

# 담보를 하나도 못 이었으면 필터를 포기한다.
# 일부만 이어진 경우는 이은 것만으로 좁힌다 - 못 이은 담보는 어차피 근거를 못 찾는다.
MIN_MATCH_RATIO = 0.3


def normalize(text: str) -> str:
    """공백·괄호·구두점을 지워 표기 차이를 흡수한다."""
    text = re.sub(r"[()（）\[\]·・,，.]", "", text or "")
    return re.sub(r"\s+", "", text).lower()


def strip_suffix(text: str) -> str:
    for suffix in SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


# 포함 관계를 근거로 쓰려면 짧은 쪽이 이만큼은 되어야 한다.
#
# 실물 증권에서 "상해"(2글자)가 "해외여행 중 폭력상해피해 변호사선임비용 보장 특별약관"에
# 포함된다는 이유로 1.0을 받아 매칭됐다. 의료비 담보를 물었는데 변호사비 조항이 근거로
# 올라온다. 두세 글자는 어느 특약 이름에나 들어 있어 포함 관계가 근거가 되지 못한다.
#
# 4로 잡으면 "배상책임"·"여권분실" 같은 실제 담보명은 그대로 살고, "상해"·"질병"만 빠진다.
# 빠진 것들은 어차피 특약명이 아니라 분류명이라, 못 이어도 필터를 안 걸 뿐 손해가 없다.
MIN_CONTAINMENT_LENGTH = 4


def similarity(a: str, b: str) -> float:
    na, nb = strip_suffix(normalize(a)), strip_suffix(normalize(b))
    if not na or not nb:
        return 0.0
    # 한쪽이 다른 쪽을 통째로 포함하면 같은 담보로 본다.
    # "휴대품손해분실제외" vs "해외여행중휴대품손해분실제외" 처럼 증권이 앞부분을
    # 생략하는 경우가 흔한데, SequenceMatcher 비율만으로는 짧은 쪽이 불리하다.
    if (na in nb or nb in na) and min(len(na), len(nb)) >= MIN_CONTAINMENT_LENGTH:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def match_one(coverage_name: str, clause_paths: list[str]) -> tuple[str | None, float]:
    """담보명 하나에 가장 가까운 특약명을 찾는다. (특약명, 점수)"""
    best, best_score = None, 0.0
    for path in clause_paths:
        score = similarity(coverage_name, path)
        if score > best_score:
            best, best_score = path, score
    if best_score >= SIMILARITY_THRESHOLD:
        return best, best_score
    return None, best_score


def match_coverages(
    coverage_names: list[str],
    clause_paths: list[str],
) -> tuple[tuple[str, ...] | None, dict]:
    """증권 담보명 목록을 약관 특약명 목록으로 잇는다.

    돌려주는 첫 값이 None이면 "필터를 걸지 말라"는 뜻이다. 호출하는 쪽은 그대로
    SearchScope에 넣으면 되고, None이면 필터가 붙지 않아 기존 동작이 된다.

    두 번째 값은 진단 정보다. 못 이은 담보명이 쌓이면 그게 곧 개선 대상 목록이 된다.
    """
    if not coverage_names or not clause_paths:
        return None, {"reason": "입력 없음", "matched": [], "unmatched": list(coverage_names)}

    matched: dict[str, str] = {}
    unmatched: list[str] = []

    for name in coverage_names:
        path, score = match_one(name, clause_paths)
        if path:
            matched[name] = path
        else:
            unmatched.append(name)
            logger.info("특약명을 못 찾았습니다: %s (최고 유사도 %.2f)", name, score)

    ratio = len(matched) / len(coverage_names)
    report = {
        "matched": matched,
        "unmatched": unmatched,
        "match_ratio": ratio,
    }

    # 너무 적게 이어졌으면 필터가 오히려 해가 된다. 기존 동작으로 돌아간다.
    if ratio < MIN_MATCH_RATIO:
        report["reason"] = f"매칭률 {ratio:.0%} < {MIN_MATCH_RATIO:.0%}, 필터 포기"
        logger.warning("특약명 매칭률이 낮아 필터를 걸지 않습니다: %s", report["reason"])
        return None, report

    # 같은 특약을 가리키는 담보가 여럿일 수 있으므로 중복을 제거한다.
    paths = tuple(dict.fromkeys(matched.values()))
    report["reason"] = f"{len(paths)}개 특약으로 좁힘"
    return paths, report
