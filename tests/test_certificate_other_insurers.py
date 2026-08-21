"""검증한 것과 다른 양식의 증권에서도 동작하는지에 대한 회귀 테스트.

에이전트 스키마를 한화손보 증권 1건을 보고 만들었다. 그 뒤 현대해상 증권을
넣어보니 담보가 0건으로 나왔다. 원인은 금액 컬럼이었다.

  한화손보   coverage_amount_age_1_14 / coverage_amount_age_15_80  (연령대 2컬럼)
  현대해상   1인당 보상한도액                                       (단일 컬럼)

에이전트가 연령대 2컬럼 표를 찾도록 되어 있어 매칭 대상이 없었고,
coverage_by_age_table이 빈 배열로 나왔다. 실패도 아니고 조용히 0건이었다.

담보 목록 자체는 coverage_description_table에 11건 살아 있었다. 그것마저
버리고 "분석 실패"로 끝내던 것이 이 파일이 막는 회귀다.

근본 수정은 Studio에서 에이전트 스키마를 넓히는 것이다. 여기서 고정하는 것은
"그 수정이 되기 전에도 화면이 비지 않는다"까지다.
"""

from app.services.certificate_adapter import (
    FALLBACK_LIMIT_LABEL,
    insurance_period,
    looks_like_certificate,
    to_coverages,
    to_payloads,
)

# 현대해상 증권에서 실제로 받은 에이전트 출력의 모양.
# 개인정보 필드는 값을 비워 두었다.
HYUNDAI = {
    "document_title": "해외여행보험 가입증명서",
    "insurer_name": "현대해상화재보험",
    "policy_number": "F-26PA-0120186",
    "insurance_period_start_datetime": "2026-07-30 20:00",
    "insurance_period_end_datetime": "2026-07-31 23:00",
    # 스키마가 연령대 2컬럼을 찾아 매칭에 실패한 자리
    "coverage_by_age_table": [],
    "coverage_description_table": [
        {
            "benefit_name": "상해사망·후유장해 (해외여행중)",
            "benefit_description": "여행 중 상해로 사망하거나 후유장해가 남은 경우 보상합니다.",
        },
        {
            "benefit_name": "휴대품손해(분실제외)",
            # 설명문에 숫자가 섞여 있다. 자기부담금·물품당 한도이고 가입금액이 아니다.
            "benefit_description": "자기부담금 1만원, 물품당 최대 200,000원 한도로 보상합니다.",
        },
        {"benefit_name": "항공기납치위로금", "benefit_description": "70,000원 지급"},
    ],
}


# 금액 표가 비어도 담보 카드는 나와야 한다. 여기서 빈 목록을 돌려주면
# 분석 전체가 실패로 끝나고 화면에 아무것도 뜨지 않는다.
def test_falls_back_to_description_table():
    payloads = to_payloads(HYUNDAI)

    assert [p.title for p in payloads] == [
        "상해사망·후유장해 (해외여행중)",
        "휴대품손해(분실제외)",
        "항공기납치위로금",
    ]


# 금액을 모르는 것과 미보장은 다르다. NOT_COVERED로 두면 가입한 담보가
# 화면에 "미보장"으로 뜨고, 그쪽이 더 나쁜 오답이다.
def test_fallback_marks_covered_with_unknown_limit():
    item = to_payloads(HYUNDAI)[0]

    assert item.coverage_status == "COVERED"
    assert item.limit_amount is None
    assert item.limit_label == FALLBACK_LIMIT_LABEL


# 설명문에 섞인 숫자를 한도로 올리면 1억원짜리 담보가 1만원으로 뜬다.
def test_fallback_never_guesses_amount_from_description():
    items = {p.title: p for p in to_payloads(HYUNDAI)}
    baggage = items["휴대품손해(분실제외)"]

    assert baggage.limit_amount is None
    assert baggage.limit_label == FALLBACK_LIMIT_LABEL
    # 설명은 살린다. 자기부담금 조건은 화면에 필요한 정보다.
    assert "자기부담금" in baggage.conditions


# 금액 표가 정상이면 폴백이 끼어들지 않아야 한다. 한화손보 경로가 그대로 돌아야 한다.
def test_amount_table_wins_when_present():
    certificate = {
        **HYUNDAI,
        "coverage_by_age_table": [
            {
                "coverage_category_level_1": "해외의료비 보장",
                "coverage_item_name": "상해",
                "coverage_amount_age_15_80": "US 5만달러",
            }
        ],
    }

    payloads = to_payloads(certificate)

    assert len(payloads) == 1
    assert payloads[0].limit_amount == 50000
    assert payloads[0].limit_label == "US 5만달러"


# 챗봇 오답의 대부분이 가입 여부에서 난다. 금액을 몰라도 가입 담보는 알려줘야 한다.
def test_chatbot_context_also_falls_back():
    coverages = to_coverages(HYUNDAI)

    assert len(coverages) == 3
    assert all(c.subscribed for c in coverages)
    assert all(c.limit_amount is None for c in coverages)


# policies.start_date/end_date가 NOT NULL이다. 비면 백엔드가 행을 만들 수 없다.
def test_reads_insurance_period():
    assert insurance_period(HYUNDAI) == ("2026-07-30", "2026-07-31")


# 두께가 아니라 증권 고유 항목으로 판단한다. 157페이지 합본 증권이 실재한다.
def test_recognized_as_certificate():
    assert looks_like_certificate(HYUNDAI)
    assert not looks_like_certificate({"content": "...", "elements": []})
