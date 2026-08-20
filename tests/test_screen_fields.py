"""보장요약 화면이 실제로 쓰는 필드 검증.

프론트에 물어본 결과 화면에 나가는 것은 다섯 개다.
    담보명 / 한도문구 / 한도금액 / 세부항목 / 세부한도
면책 사유는 화면에 띄우지 않기로 했다(챗봇의 면책 동반 조회와는 별개다).

한도금액은 약관이 "보험증권에 기재된 금액을 한도로"라고만 쓰는 경우가 대부분이라
7개 담보 중 1개에서만 나온다. 그래서 한도문구가 실제로 화면을 채우는 값이고,
비어 있으면 사용자에게 한도 칸이 빈칸으로 보인다.
"""

import pytest

from app.services.callback_mapper import MAX_LENGTHS, to_payload
from app.schemas.coverage import CoverageItem
from app.services.coverage_extractor import OUTPUT_SCHEMA, SYSTEM_PROMPT


def make_item(**overrides) -> CoverageItem:
    payload = {
        "title": "해외여행중 휴대품손해",
        "category": "baggage",
        "isCovered": True,
        "coverageStatus": "COVERED",
    }
    payload.update(overrides)
    return CoverageItem.model_validate(payload)


# ── 프롬프트가 한도문구를 요구하는가 ────────────────────────


# 이 지시가 빠지면 모델이 숫자가 있을 때만 채운다. 실측에서 7개 중 2개만 채워졌고,
# 나머지 5개는 화면에 빈칸으로 나갔다.
def test_prompt_requires_limit_label_even_without_number():
    combined = SYSTEM_PROMPT + OUTPUT_SCHEMA

    assert "limitLabel" in combined
    assert "항상 채우" in combined or "반드시 채우" in combined
    # 숫자가 없을 때 무엇을 쓸지 예시가 있어야 모델이 따라 쓴다
    assert "보험가입금액 한도" in combined


# 금액과 문구의 규칙이 달라야 한다. 금액까지 항상 채우라고 하면 없는 숫자를 지어낸다.
def test_prompt_keeps_limit_amount_conditional():
    assert "숫자가 명시된 경우에만" in SYSTEM_PROMPT


# ── 길이 ─────────────────────────────────────────────────────


# 문구를 풍부하게 쓰라고 지시했으므로 상한에 걸릴 위험이 생겼다.
# 화면에 나가는 문구가 중간에서 잘리면 오히려 안 보이느니만 못하다.
def test_long_limit_label_is_truncated_not_rejected():
    payload = to_payload(make_item(limitLabel="가" * 300), {})

    assert len(payload.limit_label) == MAX_LENGTHS["limit_label"]


def test_realistic_limit_labels_fit_within_limit():
    # 실제 추출에서 나온 가장 긴 문구(48자)를 기준으로, 상한에 여유가 있어야 한다
    realistic = "보험가입금액 한도 (증권 확인 필요), 1사고당 총보험금은 보험가입금액의 200% 한도"

    assert len(realistic) < MAX_LENGTHS["limit_label"]
    assert to_payload(make_item(limitLabel=realistic), {}).limit_label == realistic


# ── 화면 필드가 콜백까지 살아 나가는가 ──────────────────────


def test_screen_fields_survive_to_callback():
    item = make_item(
        limitLabel="1개당 20만원 한도",
        limitAmount=200000,
        detailItems=[{"title": "파손", "isCovered": True}],
        subLimits=[{"label": "1개당", "value": "20만원", "limitAmount": 200000}],
    )

    payload = to_payload(item, {})

    assert payload.title
    assert payload.limit_label == "1개당 20만원 한도"
    assert payload.limit_amount == 200000
    assert len(payload.detail_items) == 1
    assert payload.sub_limits[0].limit_amount == 200000


# 한도금액이 없어도 문구는 나가야 한다. 이 조합이 7개 중 6개다.
def test_limit_label_goes_out_even_when_amount_is_null():
    payload = to_payload(make_item(limitLabel="보험가입금액 한도 (증권 확인 필요)"), {})

    assert payload.limit_amount is None
    assert payload.limit_label == "보험가입금액 한도 (증권 확인 필요)"
