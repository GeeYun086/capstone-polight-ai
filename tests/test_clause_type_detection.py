"""조항 성격(clause_type) 분류 검증.

라벨이 틀리면 두 곳이 조용히 망가진다.
  면책 짝짓기가 어긋나 보장 조항에 엉뚱한 면책이 붙거나 아예 안 붙는다.
  프롬프트가 보장 조항을 "[면책]"으로 표시해 LLM이 "보상되지 않는다"고 답한다.

실측에서 보장을 선언한 제목 56개 중 14개(25%)가 excluded로 뒤집혀 있었다.
"""

import pytest

from scripts.chunk_policy import detect_coverage_type


# ── 제목이 본문보다 우선한다 ─────────────────────────────────


# 보장 조항이 면책 조항을 참조하는 일이 흔하다. 본문만 보면 전부 면책으로 뒤집힌다.
@pytest.mark.parametrize("title,text", [
    (
        "제4조의2(비급여 실손의료비 특별약관에서 보상하는 사항)",
        "① 제3조 및 제4조에도 불구하고 다음 각 호는 기본형에서 보상하지 않습니다.",
    ),
    (
        "제2조(보상하는 사항)",
        "회사는 기본형 실손의료비 특별약관의 제4조(보상하지 않는 사항)에도 불구하고",
    ),
    (
        "②회사가 보상하는 손해는 아래와 같습니다.",
        "긴급구입 비용(단, 수하물을 수령한 이후 발생한 비용은 보상하지 않습니다.)",
    ),
])
def test_coverage_title_wins_over_exclusion_reference_in_body(title, text):
    assert detect_coverage_type(title, text) == "included"


# 반대로, 제목이 면책을 선언하면 본문에 보장 표현이 있어도 면책이다.
def test_exclusion_title_wins():
    assert detect_coverage_type(
        "제4조(보상하지 않는 사항)",
        "제3조에서 보상하는 손해 중 다음은 제외합니다.",
    ) == "excluded"


# 순서가 중요하다. "보상하지"를 먼저 봐야 "보상하지 않는 사항"이 보장으로 잡히지 않는다.
def test_negation_is_checked_before_coverage():
    assert detect_coverage_type("보상하지 않는 손해", "") == "excluded"
    assert detect_coverage_type("보상하는 손해", "") == "included"


# ── 일반 조항 ────────────────────────────────────────────────


# 담보가 아니라 계약 자체를 다루는 조항이다. "보장"으로 라벨되면 프롬프트에
# [보장]으로 표시돼 LLM이 담보 조항으로 오해한다.
@pytest.mark.parametrize("title", [
    "제18조(약관교부 및 설명의무 등)",
    "제13조(계약 전 알릴 의무)",
    "제37조(관할법원)",
    "제32조(계약자의 임의해지)",
    "제34조(분쟁의 조정)",
])
def test_general_clauses_are_not_coverage(title):
    assert detect_coverage_type(title, "") == "general"


# 담보 선언이 일반 조항 패턴보다 먼저 잡혀야 한다.
# "보험료의 납입"은 일반 조항이고 "보험금의 지급사유"는 담보 선언이다.
def test_coverage_declaration_beats_general_pattern():
    assert detect_coverage_type("제1조(보험금의 지급사유)", "") == "included"
    assert detect_coverage_type("제3조(보장종목별 보상내용)", "") == "included"


# ── 나머지 ───────────────────────────────────────────────────


@pytest.mark.parametrize("title,expected", [
    ("제7조(보험금의 청구)", "procedure"),
    ("제8조(보험금의 지급절차)", "procedure"),
    ("<붙임1>용어의 정의", "definition"),
    ("용어 해설", "definition"),
])
def test_other_clause_types(title, expected):
    assert detect_coverage_type(title, expected and title) == expected


# 제목으로 판단이 안 되면 본문을 본다(기존 방식이 살아 있어야 한다).
def test_falls_back_to_body_when_title_is_uninformative():
    assert detect_coverage_type("가. 세부 항목", "보상하지 않는 손해는 다음과 같습니다") == "excluded"


# 공백이 불규칙해도 같은 결과여야 한다. PDF에서 넘어온 텍스트는 공백이 깨져 있다.
def test_irregular_whitespace_is_normalized():
    assert detect_coverage_type("보상하지  않는   손해", "") == "excluded"
    assert detect_coverage_type("보상하지않는손해", "") == "excluded"
