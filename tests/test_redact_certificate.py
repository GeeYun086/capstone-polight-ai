"""증권 마스킹 회귀 테스트.

두 방향을 다 검증해야 한다.

  개인정보가 남으면      공유했을 때 문제가 된다
  상품 정보가 지워지면    어댑터를 짤 수 없는데, "개인정보를 지웠다"고 생각하고 넘어간다

실제로 후자가 났다. "age"를 부분 일치로 뒀더니 "coverages"("cover-age-s")가 통째로
지워졌고, 담보 목록이 사라진 채로 저장됐다.
"""

import pytest

from scripts.redact_certificate import is_sensitive, redact, scrub_text

SAMPLE = {
    "피보험자명": "홍길동",
    "insured_person_name": "홍길동",
    "payer_name": "홍길동",
    "group_policyholder_name": "마이뱅크(주)",
    "issuer_representative_name": "나채범",
    "생년월일": "1995-03-12",
    "증권번호": "DB-2026-000123",
    "insurerName": "DB손해보험",
    "productName": "프로미 해외여행보험Ⅰ",
    "contact": {"phone": "010-1234-5678", "email": "hong@example.com", "address": "서울시 강남구"},
    "coverages": [
        {"coverageName": "해외여행중 휴대품손해(분실제외)", "limitAmount": 2000000, "subscribed": True},
        {"coverageName": "기본형 실손의료비", "limitAmount": 30000000, "subscribed": True},
    ],
}


def test_removes_personal_fields():
    cleaned = redact(SAMPLE, [])

    assert cleaned["피보험자명"] == "***"
    assert cleaned["생년월일"] == "***"
    assert cleaned["증권번호"] == "***"
    assert cleaned["contact"]["phone"] == "***"
    assert cleaned["contact"]["email"] == "***"
    assert cleaned["contact"]["address"] == "***"


# 어댑터에 필요한 것은 남아야 한다. 이게 지워지면 마스킹한 파일이 쓸모없어진다.
def test_keeps_product_and_coverage_data():
    cleaned = redact(SAMPLE, [])

    assert cleaned["insurerName"] == "DB손해보험"
    assert cleaned["productName"] == "프로미 해외여행보험Ⅰ"
    assert isinstance(cleaned["coverages"], list), "담보 목록이 통째로 지워졌다"
    assert cleaned["coverages"][0]["coverageName"] == "해외여행중 휴대품손해(분실제외)"
    assert cleaned["coverages"][0]["limitAmount"] == 2000000


# "coverages"에는 "age"가 들어 있다. 짧은 낱말을 부분 일치로 쓰면 안 되는 이유다.
def test_short_words_do_not_match_by_substring():
    assert is_sensitive("coverages") is False
    assert is_sensitive("coverageList") is False
    assert is_sensitive("age") is True


# 실제 증권을 처리하다 노출됐다. "name"을 키 전체 일치로만 검사해서
# insured_person_name, payer_name이 그대로 남았다. 사람을 가리키는 낱말이 앞에
# 붙는 형태가 흔하므로 접두어 자체를 잡아야 한다.
@pytest.mark.parametrize("key", [
    "insured_person_name", "payer_name", "group_policyholder_name",
    "issuer_representative_name", "applicantName", "beneficiary_name",
])
def test_person_name_fields_are_masked(key):
    assert is_sensitive(key) is True, f"{key}에 실명이 남는다"


# insured(피보험자)와 insurer(보험사)는 다른 문자열이다. 보험사명은 어댑터에 필요하다.
def test_insurer_is_not_confused_with_insured():
    assert is_sensitive("insurer_name") is False
    assert is_sensitive("insured_name") is True


# productName·coverageName은 "name"을 포함하지만 상품 정보다.
def test_name_suffix_on_product_fields_is_kept():
    assert is_sensitive("productName") is False
    assert is_sensitive("insurerName") is False
    assert is_sensitive("coverageName") is False
    assert is_sensitive("name") is True


# 스튜디오가 원문 텍스트를 함께 실어줄 수 있다. 키로는 못 거르므로 본문도 훑는다.
def test_scrubs_patterns_inside_free_text():
    text = scrub_text("피보험자 홍길동(950312-1234567) 010-1234-5678 hong@example.com")

    assert "950312-1234567" not in text
    assert "010-1234-5678" not in text
    assert "hong@example.com" not in text


def test_reports_what_was_removed():
    removed: list[str] = []
    redact(SAMPLE, removed)

    assert "피보험자명" in removed
    assert "contact.phone" in removed
    assert not any(r.startswith("coverages") for r in removed)
