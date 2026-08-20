"""coverage_status 판정과 DB 번역 검증.

EXCLUDED와 NOT_COVERED를 나누는 이유는 화면 문구가 달라지기 때문이다.
  EXCLUDED     "이 약관은 OO를 보상하지 않습니다"
  NOT_COVERED  "관련 내용을 찾을 수 없습니다"
사용자가 "왜 안 되는지"를 아는 것과 "안 나온다"만 아는 것은 다르다.

LLM 호출은 하지 않는다. 여기서 확인할 것은 스키마가 값을 받아들이는지와
DB 허용값으로 번역되는지다. 실제 판정 품질은 조작한 조각으로 따로 확인했다.
"""

import typing

import pytest

from app.schemas import db_enums
from app.schemas.coverage import CoverageItem, CoverageStatus
from app.services.callback_mapper import to_payload


def make_item(status: str) -> CoverageItem:
    return CoverageItem.model_validate({
        "title": "치과 응급치료", "category": "dental_emergency",
        "isCovered": status == "COVERED", "coverageStatus": status,
    })


# 백엔드가 EXCLUDED를 살려달라고 했고 우리가 가능하다고 답했다.
# 스키마가 안 받으면 그 약속을 못 지킨다.
def test_excluded_is_an_allowed_internal_value():
    assert "EXCLUDED" in typing.get_args(CoverageStatus)


@pytest.mark.parametrize("internal,expected", [
    ("COVERED", "COVERED"),
    ("PARTIAL", "PARTIALLY_COVERED"),
    ("EXCLUDED", "EXCLUDED"),
    ("NOT_COVERED", "NOT_COVERED"),
    # 허용값에 UNKNOWN이 없어 접는다. 백엔드가 enum에 추가하면 그때 바꾼다.
    ("UNKNOWN", "NOT_COVERED"),
])
def test_status_is_translated_to_db_value(internal, expected):
    assert db_enums.coverage_status(internal) == expected


# 콜백까지 살아서 나가야 의미가 있다. 중간에 접히면 백엔드가 구분할 수 없다.
def test_excluded_survives_to_callback():
    assert to_payload(make_item("EXCLUDED"), {}).coverage_status == "EXCLUDED"


# EXCLUDED가 NOT_COVERED로 뭉개지면 구분한 의미가 없어진다.
def test_excluded_is_not_folded_into_not_covered():
    excluded = to_payload(make_item("EXCLUDED"), {}).coverage_status
    not_covered = to_payload(make_item("NOT_COVERED"), {}).coverage_status

    assert excluded != not_covered


# 모르는 값이 와도 예외를 내지 않는다. 값 하나 때문에 분석 전체가 실패하면
# 어렵게 만든 policy_chunks까지 무의미해진다.
def test_unknown_value_falls_back_safely():
    assert db_enums.coverage_status("정체불명") == "NOT_COVERED"
    assert db_enums.coverage_status(None) == "NOT_COVERED"
