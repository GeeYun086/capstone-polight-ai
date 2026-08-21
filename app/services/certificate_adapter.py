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
from datetime import date

from app.schemas.analysis import CoverageItemPayload
from app.schemas.db_limits import cut
from app.services.bm25 import tokenize
from app.schemas.rag import CertificateCoverage


# 증권 담보를 약관 규칙과 잇는 표준 카테고리(medical_expense 등)를 담보명에서 뽑는다.
#
# 백엔드가 coverage_items.terms_coverage_id를 채워 보장상세를 연결하는데,
# 증권 담보명("해외의료실비보장")과 약관 규칙 title("기본형 해외여행 실손의료비")은
# 표기가 전혀 달라 이름으로는 못 잇는다. 양쪽을 같은 표준 카테고리로 환산해 그 키로
# 잇는다. 약관 청킹이 쓰는 category_mapping.json/ match_category를 그대로 재사용해
# 두 경로가 같은 사전을 보게 한다.
def _load_category_matcher():
    import json
    from pathlib import Path
    from scripts.chunk_policy import build_mapping_entries, match_category

    config_dir = Path(__file__).resolve().parents[2] / "config"
    with (config_dir / "category_mapping.json").open(encoding="utf-8") as f:
        entries = build_mapping_entries(json.load(f))
    with (config_dir / "standard_categories.json").open(encoding="utf-8") as f:
        vocab = set(json.load(f).keys())
    return entries, match_category, vocab


try:
    _CATEGORY_ENTRIES, _match_category, _STANDARD_VOCAB = _load_category_matcher()
except Exception as exc:  # 사전이 없거나 깨져도 분석은 계속돼야 한다(category만 빈다)
    logging.getLogger(__name__).warning("category 매핑 사전 로드 실패: %s", exc)
    _CATEGORY_ENTRIES, _match_category, _STANDARD_VOCAB = [], None, set()


def _standard_category(title: str, agent_value: str | None) -> str | None:
    """담보명을 표준 카테고리로 환산한다.

    category는 백엔드가 담보<->규칙 연결의 fallback 키로 쓰므로, 닫힌 어휘
    (standard_categories.json) 안의 값이거나 None이어야 한다. 어휘 밖 문자열을
    실어 보내면 계약이 깨지고 백엔드에 알 수 없는 값이 쌓인다.

    매핑에 걸리면 그 표준 카테고리를, 안 걸리면 에이전트 원값이 표준 어휘일
    때만 채택한다. 에이전트 원값(coverage_category_level_1)은 보통 한글 분류라
    어휘 밖이므로 그 경우 None으로 비운다 - 연결은 title 단계에서 시도되고,
    category가 비어도 미연결로 안전하게 남는다.
    """
    if _match_category is not None and title:
        hit = _match_category(title, "", _CATEGORY_ENTRIES)
        if hit.get("matched_category"):
            return hit["matched_category"]
    return agent_value if agent_value in _STANDARD_VOCAB else None

logger = logging.getLogger(__name__)

# 성인 기준. 실제 서비스에서는 백엔드가 생년월일로 계산해 넘긴다.
DEFAULT_AGE = 30

# 금액이 없다는 표시. 그 연령대에서 보장하지 않는다는 뜻이다.
NOT_COVERED_MARKS = {"-", "", "미보장", "해당없음", "없음", "x", "X"}

# 한국어 수 단위. 큰 것부터 처리해야 "3억5천만"이 제대로 풀린다.
UNITS = (("억", 100_000_000), ("만", 10_000), ("천", 1_000))

# 통화 판정. 달러 표기가 섞여 나온다("US 5만달러").
USD_MARKS = ("달러", "USD", "US$", "$")

# 단가·비율 표현. 총 한도를 이 문자열만으로 알 수 없다.
AMBIGUOUS_MARKS = ("1일당", "일당", "1회당", "회당", "%", "퍼센트", "가입금액의")

# 금액이 채워진 비율이 이보다 낮으면 경고한다.
#
# 실측 기준선은 한화손보 증권의 21건/21건(100%)이다. 일부 담보가 "-"(미보장)인
# 증권도 있으니 여유를 두되, 절반 이하로 떨어지면 표 추출이 깨진 것으로 본다.
# 조용한 부분 실패를 잡는 것이 목적이다 - 담보 11건 중 4건만 금액이 붙어도
# 화면은 정상으로 보인다.
AMOUNT_FILL_WARN_RATIO = 0.5


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
    if text is None or isinstance(text, bool):
        return None, None

    # 에이전트가 금액을 숫자로 주는 경우. 통화는 알 수 없어 원화로 본다.
    if isinstance(text, (int, float)):
        return int(text), "KRW"

    if not isinstance(text, str):
        return None, None

    raw = text.strip()
    if raw in NOT_COVERED_MARKS:
        return None, None

    currency = "USD" if any(m in raw for m in USD_MARKS) else "KRW"

    # 단일 금액으로 환산할 수 없는 표현은 비운다.
    #
    # "1일당 10만원, 최대 30일"을 그대로 파싱하면 100000이 나온다. 실제 한도는
    # 300만원인데 화면과 챗봇에 10만원으로 나간다. 자신 있게 틀리는 것이 비우는
    # 것보다 나쁘다 - limitLabel에 원문이 남으므로 화면 표시는 그대로고,
    # 챗봇은 금액을 단정하지 않고 증권 원문을 보라고 답한다.
    if any(mark in raw for mark in AMBIGUOUS_MARKS):
        logger.info("단일 한도로 환산할 수 없는 표현입니다: %r", text)
        return None, currency

    # 괄호 주석("(정액)")과 통화 표기를 걷어낸다. 숫자와 단위만 남긴다.
    cleaned = re.sub(r"\([^)]*\)", "", raw)
    # 긴 표기를 먼저 지운다. "US"가 앞에 오면 "USD"에서 "US"만 지워져 "D"가 남고,
    # 그 뒤 숫자 변환이 실패해 금액을 통째로 놓친다("50,000 USD" -> None).
    cleaned = re.sub(r"(USD|US\$|US|\$|₩|달러|원|미불)", "", cleaned)
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


def _clean(text: object) -> str:
    """PDF에서 뽑힌 문자열은 줄바꿈 때문에 어절 사이가 벌어져 있다.

    문자열이 아닌 값도 받는다. 에이전트가 금액을 숫자로 주는 일이 있는데,
    여기서 막지 않으면 이 함수를 부르는 곳 전부가 AttributeError로 죽는다.
    프롬프트로 "문자열로 주라"고 지시해도 LLM 출력이라 보장되지 않는다.
    """
    if text is None or isinstance(text, bool):
        return ""
    if not isinstance(text, str):
        if isinstance(text, (int, float)):
            text = str(text)
        else:
            return ""
    return " ".join(text.split())


def _rows(certificate: dict, key: str) -> list[dict]:
    """표를 행 목록으로 꺼낸다. LLM 출력이라 타입을 믿을 수 없다.

    배열 자리에 객체 하나가 오거나 null이 오는 일이 있다. 그대로 순회하면
    문자열을 행으로 취급해 AttributeError로 죽고, 그러면 담보를 하나도 못
    건진다. 죽는 대신 걸러내고 로그를 남긴다.
    """
    value = certificate.get(key)

    if isinstance(value, dict):
        logger.warning("%s가 배열이 아니라 객체입니다. 행 1건으로 처리합니다.", key)
        return [value]

    if not isinstance(value, list):
        if value is not None:
            logger.warning(
                "%s가 배열이 아닙니다 (%s). 비어 있는 것으로 봅니다.",
                key, type(value).__name__,
            )
        return []

    rows = [row for row in value if isinstance(row, dict)]
    if len(rows) != len(value):
        logger.warning("%s에서 행이 아닌 항목 %d건을 걸렀습니다.", key, len(value) - len(rows))
    return rows


# 에이전트 스키마가 바뀌어도 양쪽을 다 읽는다.
#
# 처음 스키마는 한화손보 증권 1건을 보고 만들어져 "연령대 2컬럼 표"를 전제했다.
# 현대해상 증권은 단일 한도 컬럼이라 표가 통째로 비었고, 그래서 스키마를
# 일반형으로 고쳤다(coverage_table / coverage_name / coverage_amount).
#
# 옛 이름을 남겨두는 이유는 두 가지다. 이미 분석한 증권의 raw_result_json이
# 옛 형태로 저장돼 있고, Studio 설정은 되돌릴 수 있다(UPSTAGE_AGENT_CONFIG_ID를
# 비워 최신 설정을 쓴다). 한쪽만 읽으면 그때 다시 0건이 된다.
#
# 앞에 있는 이름이 우선한다.
TABLE_KEYS = ("coverage_table", "coverage_by_age_table")
NAME_KEYS = ("coverage_name", "coverage_item_name")
CONDITION_KEYS = ("coverage_conditions",)
# 연령 구분이 없는 증권의 단일 한도. 대부분의 증권이 이 형태다.
AMOUNT_KEYS = ("coverage_amount",)
# 통화 컬럼. 새 스키마의 금액에는 단위가 없다("100,000,000").
CURRENCY_KEYS = ("coverage_currency", "currency", "화폐")


def _first(row: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _clean(row.get(key))
        if value:
            return value
    return ""


def _coverage_rows(certificate: dict) -> list[dict]:
    """담보 표. 이름이 다른 두 스키마를 모두 받는다."""
    for key in TABLE_KEYS:
        rows = _rows(certificate, key)
        if rows:
            return rows
    return []


def _amount_label(row: dict, age: int) -> str:
    """이 행의 한도 문자열.

    연령대별 컬럼이 채워져 있으면 그것이 더 정확하다(그 나이에 보장하지 않으면
    "-"로 구분된다). 비어 있으면 단일 한도 컬럼을 쓴다.
    """
    by_age = _clean(row.get(_amount_column(age)))
    if by_age:
        return by_age
    return _first(row, AMOUNT_KEYS)


def _row_currency(row: dict) -> str | None:
    """행에 통화 컬럼이 있으면 읽는다.

    새 스키마의 금액 문자열에는 단위가 없다("100,000,000"). 숫자만 있으면
    원화로 보는데, 달러 담보가 그렇게 오면 5만달러가 5만원이 된다.
    금액 문자열에 통화 표기가 있으면 그것이 우선이다 - 인쇄된 값이기 때문이다.
    """
    text = _first(row, CURRENCY_KEYS)
    if not text:
        return None
    if any(mark in text for mark in USD_MARKS):
        return "USD"
    return "KRW" if "원" in text else None


def _resolve_currency(row: dict, label: str, parsed: str | None) -> str | None:
    """금액 문자열에 통화 표기가 없을 때만 행의 통화 컬럼을 쓴다."""
    if parsed is None or any(mark in label for mark in USD_MARKS):
        return parsed
    return _row_currency(row) or parsed


def subscriber_age(certificate: dict) -> int:
    """피보험자 나이. 연령대별 금액 표에서 어느 컬럼을 쓸지 정한다.

    없으면 성인으로 본다. 아동 증권에서 성인 컬럼을 읽으면 보장하지 않는 담보를
    보장된다고 답하게 되지만, 나이를 모르는 채로 아동이라 가정하면 반대 사고가
    난다. 대부분의 계약이 성인이라 그쪽을 기본으로 둔다.
    """
    value = certificate.get("insured_person_age")
    age = None
    if isinstance(value, bool):
        age = None
    elif isinstance(value, int):
        age = value
    elif isinstance(value, str):
        match = re.search(r"\d+", value)
        age = int(match.group()) if match else None

    # 0이나 음수는 나이가 아니라 "못 뽑았다"는 뜻이다. 실측에서 한화 증권이
    # age=0으로 왔는데, 이걸 그대로 쓰면 1~14세 컬럼을 읽어 성인 담보를
    # 미보장으로 만든다. 미상일 때 성인으로 보는 이유는 _amount_column 참고.
    if age is None or age <= 0:
        return DEFAULT_AGE
    return age


def coverages_complete(certificate: dict) -> bool:
    """담보 목록이 증권 보장내용 표 전체인지.

    콜백의 coveragesComplete로 나가고, 백엔드에서 "목록에 없는 담보를 미가입으로
    단정하지 않는다"는 화면·답변 동작으로 이어진다. 에이전트가 스스로 판단한
    값이라 확신할 수 없으므로, 명시적으로 true일 때만 true로 둔다.

    담보 표가 비면 false다. 그때 카드는 설명 표 폴백으로 만들어지는데, 에이전트가
    "표 전체를 읽었다"고 한 것은 금액 표에 대한 판단이라 근거가 다르다.
    """
    if not _coverage_rows(certificate):
        return False
    return certificate.get("coverage_table_complete") is True


def _descriptions(certificate: dict) -> dict[str, tuple[set, str]]:
    """담보 설명 표를 이름 -> (토큰, 설명)으로 만든다.

    설명 표의 이름과 금액 표의 이름이 정확히 일치하지 않는다("상해" vs
    "상해/질병 해외 의료비"). 토큰을 미리 만들어두고 겹침으로 잇는다.
    """
    table: dict[str, tuple[set, str]] = {}
    for row in _rows(certificate, "coverage_description_table"):
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
    name = _first(row, NAME_KEYS)
    level1 = _clean(row.get("coverage_category_level_1"))
    level2 = _clean(row.get("coverage_category_level_2"))

    if not name:
        return level2 or level1
    if not level1 or re.sub(r"\s+", "", level1) in re.sub(r"\s+", "", name):
        return name
    prefix = f"{level1} {level2}".strip()
    return f"{prefix} {name}".strip()


def to_coverages(certificate: dict, age: int | None = None) -> list[CertificateCoverage]:
    """챗봇 프롬프트에 넣을 가입 담보 목록.

    RagQueryRequest.coverages로 그대로 들어간다. 여기서 "미가입"이 정확해야
    가입하지 않은 담보를 물었을 때 "보상됩니다"라는 틀린 답을 막을 수 있다.
    """
    age = age if age is not None else subscriber_age(certificate)
    coverages: list[CertificateCoverage] = []

    for row in _coverage_rows(certificate):
        title = _title(row)
        if not title:
            continue
        label = _amount_label(row, age)
        amount, currency = parse_amount(label)
        currency = _resolve_currency(row, label, currency)
        coverages.append(
            CertificateCoverage(
                name=title,
                # 금액 파싱 성공 여부로 가입 여부를 정하지 않는다. "실손 80%"처럼
                # 금액으로 환산할 수 없는 담보를 미가입으로 답하면, 보장되는 것을
                # 안 된다고 말하는 셈이다. 그쪽이 더 나쁜 오답이다.
                subscribed=label not in NOT_COVERED_MARKS,
                limitAmount=amount,
                limitCurrency=currency,
            )
        )

    if coverages:
        return coverages

    # 금액 표가 비면 설명 표에서 담보명만 가져온다. 한도는 모르지만 가입 여부는
    # 알려줄 수 있고, 챗봇 오답의 대부분이 가입 여부에서 난다.
    return [
        CertificateCoverage(name=name, subscribed=True)
        for name in (
            _clean(row.get("benefit_name"))
            for row in _rows(certificate, "coverage_description_table")
        )
        if name
    ]


def to_payloads(certificate: dict, age: int | None = None) -> list[CoverageItemPayload]:
    """화면 보장 카드. 완료 콜백의 coverageItems로 나간다.

    보장하지 않는 담보(연령대 컬럼이 "-")도 카드로 만든다. 화면에서 "미보장"으로
    보여주는 편이, 목록에서 빼서 사용자가 그 담보의 존재 자체를 모르는 것보다 낫다.
    """
    age = age if age is not None else subscriber_age(certificate)
    descriptions = _descriptions(certificate)
    payloads: list[CoverageItemPayload] = []

    for row in _coverage_rows(certificate):
        title = _title(row)
        if not title:
            continue

        label = _amount_label(row, age)
        amount, currency = parse_amount(label)
        currency = _resolve_currency(row, label, currency)
        # 담보명을 표준 카테고리로 환산해 약관 규칙과 이을 키로 쓴다.
        # 매핑이 안 되면 에이전트 원값(coverage_category_level_1)으로 폴백.
        category = _standard_category(title, _clean(row.get("coverage_category_level_1")))

        # 길이 컷을 거치는 이유는 약관 경로와 같다(db_limits 참고). 다만 증권
        # 쪽이 더 위험하다 - 이 값들은 에이전트가 뽑은 문자열이라 길이를 우리가
        # 통제하지 못한다. 한 글자 넘치면 콜백이 500으로 세 번 튕기고 분석이
        # PROCESSING에 영구히 남는다.
        # 금액을 못 읽은 것과 미보장은 다르다. 파싱 실패로 NOT_COVERED를 찍으면
        # 가입한 담보가 화면에서 "미보장"으로 뜬다.
        covered = label not in NOT_COVERED_MARKS

        payloads.append(
            CoverageItemPayload(
                title=cut(title, "title"),
                coverageStatus="COVERED" if covered else "NOT_COVERED",
                subtitle=cut(_clean(row.get("coverage_category_level_2")) or None, "subtitle"),
                category=cut(category or None, "category"),
                # 원문을 그대로 둔다. "US 5만달러", "(정액) 50만원"처럼 정수로는
                # 표현할 수 없는 정보가 담겨 있고, 화면에는 이 문구가 나간다.
                limitLabel=cut(
                    label if label not in NOT_COVERED_MARKS else "보장하지 않음",
                    "limit_label",
                ),
                limitAmount=amount,
                limitCurrency=cut(currency, "limit_currency"),
                # conditions는 TEXT 컬럼이라 길이 제한이 없다.
                #
                # 행에 조건이 실려 오면(새 스키마의 coverage_conditions) 그것을
                # 쓴다. 설명 표를 토큰 겹침으로 잇는 것보다 정확하다 - 같은 행에
                # 인쇄된 값이라 짝이 틀릴 여지가 없다.
                conditions=_first(row, CONDITION_KEYS) or _find_description(row, descriptions),
            )
        )

    if payloads:
        _log_amount_fill(payloads)
        return payloads

    return _payloads_from_descriptions(certificate)


def _log_amount_fill(payloads: list[CoverageItemPayload]) -> None:
    """금액이 몇 건 채워졌는지 남긴다.

    표를 통째로 못 읽으면 담보 0건으로 실패해 눈에 띈다. 그런데 절반만 읽히면
    화면에 카드가 뜨고 아무도 빈 금액을 모른다. 조용한 부분 실패가 시끄러운
    전체 실패보다 나쁘므로 비율을 기록한다.
    """
    total = len(payloads)
    filled = sum(1 for p in payloads if p.limit_amount is not None)
    ratio = filled / total

    if ratio < AMOUNT_FILL_WARN_RATIO:
        logger.warning(
            "증권 담보 %d건 중 금액이 %d건(%.0f%%)뿐입니다. 에이전트의 금액 표 "
            "추출을 확인하십시오.",
            total, filled, ratio * 100,
        )
    else:
        logger.info("증권 담보 %d건, 금액 %d건(%.0f%%)", total, filled, ratio * 100)


# 금액 표가 비었을 때 설명 표로 카드를 만든다.
#
# 에이전트 스키마가 한화손보 양식 전용이라, 다른 보험사 증권에서는 금액 표가
# 통째로 빈 배열로 나온다. 현대해상 증권이 그랬다 - 금액은 "1인당 보상한도액"
# 단일 컬럼인데 스키마는 연령대별 2컬럼을 찾는다. 그래서 0건이 됐다.
#
# 그때 설명 표는 살아 있다(담보 11건 정상). 그것마저 버리면 화면에 아무것도
# 못 띄우고 "분석 실패"로 끝나는데, 담보 목록만이라도 띄우는 편이 낫다.
# 챗봇도 가입 담보를 알아야 "미가입인데 보상된다"는 오답을 막을 수 있다.
#
# 한도는 비운다. 설명문에 섞인 숫자는 자기부담금·물품당 한도라 가입금액이 아니다.
# 실측에서 "1만원", "200,000원"이 그런 값이었다. 그걸 한도로 올리면 1억원짜리
# 담보가 화면에 1만원으로 뜬다.
#
# coverageStatus는 COVERED로 둔다. 금액을 모르는 것과 미보장은 다르다.
# 여기서 NOT_COVERED로 두면 가입한 담보를 "미보장"으로 표시하게 되는데,
# 그쪽이 더 나쁜 오답이다(BACKEND_INTERFACE 3-2와 같은 판단).
FALLBACK_LIMIT_LABEL = "한도 확인 필요"


def _payloads_from_descriptions(certificate: dict) -> list[CoverageItemPayload]:
    rows = _rows(certificate, "coverage_description_table")
    payloads: list[CoverageItemPayload] = []

    for row in rows:
        title = _clean(row.get("benefit_name"))
        if not title:
            continue
        payloads.append(
            CoverageItemPayload(
                title=cut(title, "title"),
                coverageStatus="COVERED",
                limitLabel=FALLBACK_LIMIT_LABEL,
                conditions=_clean(row.get("benefit_description")) or None,
            )
        )

    if payloads:
        logger.warning(
            "금액 표가 비어 설명 표로 카드를 만들었습니다 (담보 %d건, 한도 없음). "
            "에이전트의 금액 표 추출을 확인하십시오.",
            len(payloads),
        )
    return payloads


def coverage_names(certificate: dict) -> list[str]:
    """clause_matcher에 넘길 담보명. 약관 특약을 찾는 키가 된다."""
    return [
        name
        for name in (_first(row, NAME_KEYS) for row in _coverage_rows(certificate))
        if name
    ]


# 보험기간. policies.start_date/end_date가 NOT NULL이라 백엔드가 요청한 값이다.
#
# 에이전트가 이미 뽑고 있었다("2026-07-30 20:00"). 저희가 읽는 코드만 없어서
# "확인 중"으로 답하고 있었다.
#
# 형식이 보험사마다 다를 수 있어(2026.07.30, 2026/07/30) 구분자를 가리지 않는다.
# 백엔드에 보내는 형식은 YYYY-MM-DD로 고정한다.
PERIOD_KEYS = (
    ("insurance_period_start_datetime", "insurance_period_end_datetime"),
    ("insurance_start_date", "insurance_end_date"),
    ("policy_period_start", "policy_period_end"),
)

DATE_PATTERN = re.compile(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})")


def _as_date(value: object) -> str | None:
    """앞에서 처음 만나는 날짜를 YYYY-MM-DD로 돌려준다. 시각은 버린다."""
    if not isinstance(value, str):
        return None
    m = DATE_PATTERN.search(value)
    if not m:
        return None
    year, month, day = (int(g) for g in m.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        # 2026-13-45 같은 값. 틀린 날짜를 NOT NULL 컬럼에 넣는 것보다 비우는 편이 낫다.
        logger.info("보험기간을 날짜로 읽지 못했습니다: %r", value)
        return None


def insurance_period(certificate: dict) -> tuple[str | None, str | None]:
    """(시작일, 종료일). 못 읽으면 (None, None)."""
    for start_key, end_key in PERIOD_KEYS:
        start = _as_date(certificate.get(start_key))
        end = _as_date(certificate.get(end_key))
        if start or end:
            return start, end
    return None, None


# 증권으로 보이는지. 담보를 못 찾았을 때 "증권이 아니다"와 "증권인데 표를 못 읽었다"를
# 가른다.
#
# 처음에는 페이지 수로 갈랐는데 틀렸다. 현대해상은 증권 2장과 약관 153장을 한 파일로
# 발급한다(실물 157페이지). 페이지 수로 보면 약관인데 실제로는 정상 증권이다.
# 그래서 두께가 아니라 에이전트가 증권 고유 항목을 뽑았는지로 판단한다.
CERTIFICATE_MARKERS = (
    "policy_number",
    "insurance_period_start_datetime",
    "insurer_name",
    "document_title",
)


def looks_like_certificate(certificate: dict) -> bool:
    return any(certificate.get(key) for key in CERTIFICATE_MARKERS)


# 실패했을 때 남는 것이 이 한 줄뿐이다.
#
# 에이전트 출력 원본을 남길 수는 없다. 증권에는 피보험자 이름·생년월일·증권번호가
# 들어 있고, 로그는 지우기 어렵다. 그런데 원인을 가르는 데 필요한 것은 값이 아니라
# 모양이다 - 키 이름이 바뀐 것인지, 표가 비어 있는 것인지만 알면 된다.
#
# 그래서 키 이름과 개수만 만든다. 값은 어떤 경우에도 넣지 않는다.
def describe_structure(certificate: dict) -> str:
    """에이전트 출력의 모양. "coverage_by_age_table[21](name,amount) insurer_name" 형태."""
    parts: list[str] = []

    for key, value in certificate.items():
        if isinstance(value, list):
            first = value[0] if value else None
            row_keys = ",".join(first.keys()) if isinstance(first, dict) else "-"
            parts.append(f"{key}[{len(value)}]({row_keys})")
        elif isinstance(value, dict):
            parts.append(f"{key}{{{','.join(value.keys())}}}")
        else:
            # 스칼라는 키 이름만. 값에 이름·증권번호가 들어 있을 수 있다.
            parts.append(key)

    return " ".join(parts) or "(빈 객체)"
