"""증권 분석 결과(Upstage 스튜디오 출력)를 우리 스키마로 옮긴다.

약관에서 담보를 발굴하던 것을 증권에서 읽어오는 것으로 바꾸면 작업의 성격이 달라진다.

  약관에서 뽑을 때   126페이지 법률 문서에서 담보를 발굴한다 (생성)
  증권에서 뽑을 때   1~2페이지 표에 인쇄된 값을 옮긴다 (전사)

특히 금액이 결정적이다. 약관에는 "보험가입금액을 한도로"라고만 적혀 있어 숫자가
아예 없다. 실측에서 약관 추출 30건 중 limitAmount가 채워진 것은 3건뿐이었고
모델 5종 전부 같았다. 개인 계약 조건은 약관이 아니라 증권에 있기 때문이다.

실제 증권(한화손보/마이뱅크)에서 확인한 구조:

  coverage_by_age_table       담보명 + 2단 분류 + 연령대별 금액 문자열
  coverage_description_table  담보 설명 (보상 조건·자기부담금·한도가 문장으로)

금액이 연령대별로 두 컬럼이라 피보험자 나이가 있어야 어느 쪽을 쓸지 정해진다.
"-"는 그 연령대에서 보장하지 않는다는 뜻이다.
"""

import logging
import re

from app.schemas.analysis import CoverageItemPayload
from app.services.bm25 import tokenize
from app.schemas.rag import CertificateCoverage

logger = logging.getLogger(__name__)

# 성인 기준. 실제 서비스에서는 백엔드가 생년월일로 계산해 넘긴다.
DEFAULT_AGE = 30

# 금액이 없다는 표시. 그 연령대에서 보장하지 않는다는 뜻이다.
NOT_COVERED_MARKS = {"-", "", "미보장", "해당없음", "없음", "x", "X"}

# 한국어 수 단위. 큰 것부터 처리해야 "3억5천만"이 제대로 풀린다.
UNITS = (("억", 100_000_000), ("만", 10_000), ("천", 1_000))

# 통화 판정. 달러 표기가 섞여 나온다("US 5만달러").
USD_MARKS = ("달러", "USD", "US$", "$")


def parse_amount(text: str | None) -> tuple[int | None, str | None]:
    """증권의 금액 문자열을 (정수, 통화)로 바꾼다.

    limitAmount가 BIGINT라 정수만 들어간다. 화면에 그대로 띄울 문구는 원문을
    limitLabel로 따로 보낸다.

        "US 5만달러"     -> (50000, "USD")
        "5,000만원"      -> (50000000, "KRW")
        "3억원"          -> (300000000, "KRW")
        "(정액) 50만원"   -> (500000, "KRW")
        "-"             -> (None, None)
    """
    if text is None:
        return None, None

    raw = text.strip()
    if raw in NOT_COVERED_MARKS:
        return None, None

    currency = "USD" if any(m in raw for m in USD_MARKS) else "KRW"

    # 괄호 주석("(정액)")과 통화 표기를 걷어낸다. 숫자와 단위만 남긴다.
    cleaned = re.sub(r"\([^)]*\)", "", raw)
    cleaned = re.sub(r"(US|USD|\$|달러|원|미불)", "", cleaned)
    cleaned = cleaned.replace(",", "").strip()

    if not re.search(r"\d", cleaned):
        logger.info("금액을 읽지 못했습니다: %r", text)
        return None, currency

    # "3억5천만" 처럼 단위가 겹쳐 나올 수 있어 앞에서부터 누적한다.
    total = 0
    remainder = cleaned
    for unit, multiplier in UNITS:
        if unit not in remainder:
            continue
        head, remainder = remainder.split(unit, 1)
        head = head.strip()
        if not head:
            head = "1"
        try:
            total += float(head) * multiplier
        except ValueError:
            logger.info("금액 단위를 읽지 못했습니다: %r", text)
            return None, currency

    tail = remainder.strip()
    if tail:
        try:
            total += float(tail)
        except ValueError:
            # 단위 뒤에 설명이 붙은 경우("100만원 한도")는 이미 읽은 값을 쓴다
            if total == 0:
                logger.info("금액을 읽지 못했습니다: %r", text)
                return None, currency

    return int(total), currency


def _amount_column(age: int) -> str:
    """연령대 컬럼 이름. 증권이 두 구간으로 나눠 적는다."""
    return "coverage_amount_age_1_14" if age <= 14 else "coverage_amount_age_15_80"


def _clean(text: str | None) -> str:
    """PDF에서 뽑힌 문자열은 줄바꿈 때문에 어절 사이가 벌어져 있다."""
    return " ".join((text or "").split())


def _descriptions(certificate: dict) -> dict[str, tuple[set, str]]:
    """담보 설명 표를 이름 -> (토큰, 설명)으로 만든다.

    설명 표의 이름과 금액 표의 이름이 정확히 일치하지 않는다("상해" vs
    "상해/질병 해외 의료비"). 토큰을 미리 만들어두고 겹침으로 잇는다.
    """
    table: dict[str, tuple[set, str]] = {}
    for row in certificate.get("coverage_description_table", []):
        name = _clean(row.get("benefit_name"))
        if not name:
            continue
        table[name] = (set(tokenize(name)), _clean(row.get("benefit_description")))
    return table


# 설명 표의 이름이 이만큼은 담겨야 같은 담보로 본다.
#
# 문자열 포함으로 이으려 했더니 21건 중 9건만 붙었다. 어순이 달라서다.
#   금액 표  "해외의료비 보장 / 상해"
#   설명 표  "상해/질병 해외 의료비"
# 토큰 겹침으로 바꾸니 20건이 붙었다. 나머지 1건("주사료")은 설명 쪽이 PDF에서
# "주 사료"로 벌어져 나와 토큰이 겹치지 않는다. 억지로 이으면 엉뚱한 설명이
# 붙으므로(실제로 "국내 입원의료비"가 붙었다) 임계값 아래는 설명 없이 둔다.
DESCRIPTION_MATCH_THRESHOLD = 0.3


def _find_description(row: dict, table: dict[str, set]) -> str | None:
    """담보 행에 붙일 설명. 분류명까지 합쳐서 찾아야 "상해"처럼 짧은 이름이 구분된다."""
    key = " ".join(
        part
        for part in (
            _clean(row.get("coverage_category_level_1")),
            _clean(row.get("coverage_category_level_2")),
            _clean(row.get("coverage_item_name")),
        )
        if part
    )
    key_tokens = set(tokenize(key))

    best: str | None = None
    best_score = 0.0
    for description, (tokens, text) in table.items():
        if not tokens:
            continue
        score = len(key_tokens & tokens) / len(tokens)
        if score > best_score:
            best, best_score = text, score

    return best if best_score >= DESCRIPTION_MATCH_THRESHOLD else None


def _title(row: dict) -> str:
    """화면에 뜰 담보명.

    같은 이름이 분류를 달리해 여러 번 나온다("상해"가 해외의료비에도 국내실손에도 있다).
    그대로 두면 화면에 "상해" 카드가 네 개 뜨므로 분류를 앞에 붙인다.
    """
    name = _clean(row.get("coverage_item_name"))
    level1 = _clean(row.get("coverage_category_level_1"))
    level2 = _clean(row.get("coverage_category_level_2"))

    if not name:
        return level2 or level1
    if not level1 or re.sub(r"\s+", "", level1) in re.sub(r"\s+", "", name):
        return name
    prefix = f"{level1} {level2}".strip()
    return f"{prefix} {name}".strip()


def to_coverages(certificate: dict, age: int = DEFAULT_AGE) -> list[CertificateCoverage]:
    """챗봇 프롬프트에 넣을 가입 담보 목록.

    RagQueryRequest.coverages로 그대로 들어간다. 여기서 "미가입"이 정확해야
    가입하지 않은 담보를 물었을 때 "보상됩니다"라는 틀린 답을 막을 수 있다.
    """
    column = _amount_column(age)
    coverages: list[CertificateCoverage] = []

    for row in certificate.get("coverage_by_age_table", []):
        title = _title(row)
        if not title:
            continue
        amount, currency = parse_amount(row.get(column))
        coverages.append(
            CertificateCoverage(
                name=title,
                subscribed=amount is not None,
                limitAmount=amount,
                limitCurrency=currency,
            )
        )

    return coverages


def to_payloads(certificate: dict, age: int = DEFAULT_AGE) -> list[CoverageItemPayload]:
    """화면 보장 카드. 완료 콜백의 coverageItems로 나간다.

    보장하지 않는 담보(연령대 컬럼이 "-")도 카드로 만든다. 화면에서 "미보장"으로
    보여주는 편이, 목록에서 빼서 사용자가 그 담보의 존재 자체를 모르는 것보다 낫다.
    """
    column = _amount_column(age)
    descriptions = _descriptions(certificate)
    payloads: list[CoverageItemPayload] = []

    for row in certificate.get("coverage_by_age_table", []):
        title = _title(row)
        if not title:
            continue

        label = _clean(row.get(column))
        amount, currency = parse_amount(label)
        category = _clean(row.get("coverage_category_level_1"))

        payloads.append(
            CoverageItemPayload(
                title=title,
                coverageStatus="COVERED" if amount is not None else "NOT_COVERED",
                subtitle=_clean(row.get("coverage_category_level_2")) or None,
                category=category or None,
                # 원문을 그대로 둔다. "US 5만달러", "(정액) 50만원"처럼 정수로는
                # 표현할 수 없는 정보가 담겨 있고, 화면에는 이 문구가 나간다.
                limitLabel=label if label not in NOT_COVERED_MARKS else "보장하지 않음",
                limitAmount=amount,
                limitCurrency=currency,
                conditions=_find_description(row, descriptions),
            )
        )

    return payloads


def coverage_names(certificate: dict) -> list[str]:
    """clause_matcher에 넘길 담보명. 약관 특약을 찾는 키가 된다."""
    return [
        name
        for name in (_clean(row.get("coverage_item_name")) for row in certificate.get("coverage_by_age_table", []))
        if name
    ]
