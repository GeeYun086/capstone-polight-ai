"""에이전트 스키마 2판(일반형)을 읽는지, 1판도 계속 읽는지에 대한 회귀 테스트.

1판은 한화손보 증권 1건을 보고 만들어져 "연령대 2컬럼 표"를 전제했다. 현대해상
증권은 단일 한도 컬럼이라 표가 통째로 비었고, 새 보험사마다 같은 실패가 났다.
그래서 스키마를 레이아웃이 아니라 의미 기준으로 고쳤다.

  1판  coverage_by_age_table[].coverage_item_name / coverage_amount_age_1_14|15_80
  2판  coverage_table[].coverage_name / coverage_amount / coverage_conditions

양쪽을 다 읽어야 한다. 이미 분석한 증권의 raw_result_json이 1판 형태로 저장돼
있고, Studio 설정은 되돌릴 수 있다(UPSTAGE_AGENT_CONFIG_ID를 비워 최신 설정을
쓴다). 한쪽만 읽으면 그때 다시 담보 0건이 된다.
"""

from app.services.certificate_adapter import (
    coverages_complete,
    insurance_period,
    subscriber_age,
    to_coverages,
    to_payloads,
)

# 현대해상 증권에서 실제로 받은 2판 출력. 개인정보 필드는 스키마에서 빠졌다.
V2 = {
    "document_title": "증권",
    "product_name": "해외여행보험(플랫폼 전용)",
    "insurer_name": "현대해상화재보험",
    "policy_number": "F-26PA-0120186",
    "insured_person_age": 24,
    "coverage_table_complete": True,
    "insurance_period_start_datetime": "2026-07-30 20:00",
    "insurance_period_end_datetime": "2026-07-31 23:00",
    "coverage_table": [
        {
            "coverage_name": "상해사망·후유장해 (해외여행중)",
            # 단위가 없다. 숫자만 있으면 원화로 본다.
            "coverage_amount": "100,000,000",
            "coverage_conditions": None,
            "coverage_amount_age_1_14": None,
            "coverage_amount_age_15_80": None,
        },
        {
            "coverage_name": "휴대품손해(분실제외)",
            "coverage_amount": "500,000",
            "coverage_conditions": "* 자기부담금 10,000 * 물품당 최대 20만원 한도",
        },
    ],
}

# 1판. 아직 이 형태로 저장된 결과가 있다.
V1 = {
    "insurer_name": "한화손해보험(주)",
    "document_title": "해외여행자보험 가입증명서",
    "coverage_by_age_table": [
        {
            "coverage_category_level_1": "해외의료비 보장",
            "coverage_category_level_2": "",
            "coverage_item_name": "상해",
            "coverage_amount_age_15_80": "US 5만달러",
            "coverage_amount_age_1_14": "-",
        }
    ],
    "coverage_description_table": [
        {"benefit_name": "상해/질병 해외 의료비", "benefit_description": "치료비를 보상합니다."}
    ],
}


# ── 2판 ──────────────────────────────────────────────────────


def test_reads_v2_table():
    payloads = to_payloads(V2)

    assert [p.title for p in payloads] == [
        "상해사망·후유장해 (해외여행중)",
        "휴대품손해(분실제외)",
    ]
    assert payloads[0].limit_amount == 100_000_000
    assert payloads[0].limit_currency == "KRW"
    assert payloads[0].coverage_status == "COVERED"


# 조건이 행에 실려 오면 설명 표 토큰 매칭보다 정확하다. 같은 행에 인쇄된 값이라
# 짝이 틀릴 여지가 없다.
def test_row_conditions_are_used():
    baggage = {p.title: p for p in to_payloads(V2)}["휴대품손해(분실제외)"]

    assert "자기부담금" in baggage.conditions
    assert "물품당" in baggage.conditions


def test_reads_age_and_completeness():
    assert subscriber_age(V2) == 24
    assert coverages_complete(V2) is True
    assert insurance_period(V2) == ("2026-07-30", "2026-07-31")


# 나이를 안 주면 성인으로 본다. 아동이라고 가정하면 보장되는 담보를 미보장으로 답한다.
def test_missing_age_defaults_to_adult():
    assert subscriber_age({}) == 30
    assert subscriber_age({"insured_person_age": "만 24세"}) == 24
    assert subscriber_age({"insured_person_age": True}) == 30


# 담보 표가 비면 카드가 설명 표 폴백으로 만들어진다. 그때는 근거가 달라
# "표 전체"라고 말할 수 없다.
def test_completeness_is_off_when_table_is_empty():
    assert coverages_complete({"coverage_table": [], "coverage_table_complete": True}) is False


# ── 1판 (하위 호환) ──────────────────────────────────────────


def test_still_reads_v1_table():
    payloads = to_payloads(V1)

    assert len(payloads) == 1
    assert payloads[0].limit_amount == 50_000
    assert payloads[0].limit_currency == "USD"
    # 분류 접두어를 붙이는 동작도 유지된다
    assert payloads[0].title == "해외의료비 보장 상해"
    # 설명 표 토큰 매칭도 유지된다
    assert payloads[0].conditions == "치료비를 보상합니다."


# 연령대 컬럼이 채워져 있으면 그것이 단일 한도보다 정확하다.
# 그 나이에 보장하지 않으면 "-"로 구분되기 때문이다.
def test_age_column_wins_over_single_amount():
    certificate = {
        "insured_person_age": 10,
        "coverage_table": [
            {
                "coverage_name": "상해사망",
                "coverage_amount": "100,000,000",
                "coverage_amount_age_1_14": "-",
                "coverage_amount_age_15_80": "100,000,000",
            }
        ],
    }

    item = to_payloads(certificate)[0]

    assert item.coverage_status == "NOT_COVERED"
    assert item.limit_amount is None


def test_v1_chatbot_context_unchanged():
    coverages = to_coverages(V1)

    assert len(coverages) == 1
    assert coverages[0].subscribed is True
    assert coverages[0].limit_amount == 50_000


# ── 통화 ─────────────────────────────────────────────────────


# 2판 금액에는 단위가 없다. 통화 컬럼이 있으면 읽어야 5만달러가 5만원이 되지 않는다.
def test_currency_column_is_honored():
    certificate = {
        "coverage_table": [
            {"coverage_name": "해외의료비", "coverage_amount": "50,000", "coverage_currency": "달러"}
        ]
    }

    item = to_payloads(certificate)[0]

    assert item.limit_amount == 50_000
    assert item.limit_currency == "USD"


# 금액 문자열에 통화가 적혀 있으면 그것이 우선이다. 인쇄된 값이기 때문이다.
def test_amount_string_currency_wins():
    certificate = {
        "coverage_table": [
            {"coverage_name": "해외의료비", "coverage_amount": "US 5만달러", "coverage_currency": "원"}
        ]
    }

    assert to_payloads(certificate)[0].limit_currency == "USD"
