"""증권 연동(가입 담보 주입 + 특약 필터) 회귀 테스트.

약관만으로는 구조적으로 답할 수 없는 것이 두 가지 있고, 증권이 그걸 채운다.

  가입 여부  약관에는 그 상품이 팔 수 있는 모든 특약이 실려 있어서, 가입하지 않은
             담보를 물어도 조항이 검색되고 "보상됩니다"라는 틀린 답이 나간다.
  실제 한도  약관은 "보험가입금액을 한도로"라고만 쓴다. 실측에서 약관 추출 30건 중
             limitAmount가 채워진 것은 3건뿐이었다.

그래서 이 경로가 조용히 끊기면 답변이 다시 틀려지는데, 화면에는 그럴듯한 답이
나가므로 눈으로는 알아채기 어렵다. LLM 호출 없이 프롬프트 문자열까지만 검증한다.
"""

import pytest

from app.repositories.base import SearchScope
from app.repositories.file_repository import _matches
from app.repositories.pg_repository import _scope_condition
from app.schemas.rag import RagQueryRequest
from app.services import rag_service
from app.services.clause_matcher import match_coverages
from app.services.prompt_builder import build_user_message, format_contract_info
from tests.conftest import FakeVectorRepository, make_hit


# ── 가입 정보 렌더링 ──────────────────────────────────────────


def test_contract_info_marks_subscribed_and_not_subscribed():
    text = format_contract_info(
        {
            "coverages": [
                {"name": "기본형 실손의료비", "subscribed": True, "limitAmount": 30000000},
                {"name": "해외여행중 항공기 및 수하물 지연비용", "subscribed": False},
            ]
        }
    )

    assert "기본형 실손의료비: 가입 / 한도 30,000,000원" in text
    assert "해외여행중 항공기 및 수하물 지연비용: 미가입" in text


# 목록이 증권 일부일 때는 "미가입"으로 단정하면 안 된다.
# 증권 파싱이 담보를 빠뜨렸을 때 실제로는 보장되는데 안 된다고 답하게 되고,
# 그건 보장되는 걸 못 찾는 것보다 나쁘다.
def test_partial_coverage_list_warns_against_assuming_absent():
    text = format_contract_info({"coverages": [{"name": "기본형 실손의료비", "subscribed": True}]})

    assert "미가입으로 단정하지 마십시오" in text


# 반대로 목록이 증권 전체이면 없는 담보는 미가입이라고 알려줘야 한다.
#
# 이 구분이 없을 때 실제로 이런 답이 나갔다. 증권에 골프용품손해가 없는데
#   "'골프용품손해 특별약관'에 가입되어 있는 경우 보상받을 수 있습니다"
# 사용자는 보상된다고 읽지만 실제로는 미가입이라 보험금을 못 받는다.
def test_complete_coverage_list_says_absent_means_not_subscribed():
    text = format_contract_info(
        {"coverages": [{"name": "기본형 실손의료비", "subscribed": True}], "complete": True}
    )

    assert "보장내용 표 전체" in text
    assert "가입하지 않은 것입니다" in text
    assert "단정하지 마십시오" not in text, "전체 목록인데 단정하지 말라고 하면 미가입 판정이 안 된다"


# 조건부 답변을 금지하는 지시가 살아 있어야 한다. 이게 빠지면 "가입되어 있다면
# 보상됩니다"로 돌아가고, 화면에는 그럴듯한 답이 나가 눈으로는 알 수 없다.
def test_system_prompt_forbids_conditional_coverage_answers():
    from app.services.prompt_builder import SYSTEM_PROMPT

    assert "보장내용 표 전체" in SYSTEM_PROMPT
    assert "조건부로 답하지 마십시오" in SYSTEM_PROMPT


def test_coverages_complete_defaults_to_false():
    request = RagQueryRequest.model_validate(
        {"userId": "u", "tripId": "t", "question": "q",
         "coverages": [{"name": "휴대품손해", "subscribed": True}]}
    )

    assert request.coverages_complete is False, "기본값이 바뀌면 기존 클라이언트 동작이 달라진다"


def test_contract_info_is_empty_without_coverages():
    assert format_contract_info(None) == ""
    assert format_contract_info({"coverages": []}) == ""


# ── 요청 -> 프롬프트 연결 ────────────────────────────────────


def test_request_parses_camel_case_coverages():
    request = RagQueryRequest.model_validate(
        {
            "userId": "u",
            "tripId": "t",
            "question": "한도가 얼마예요?",
            "coverages": [
                {"name": "해외여행중 휴대품손해(분실제외)", "subscribed": True, "limitAmount": 200000}
            ],
        }
    )

    assert request.coverages[0].limit_amount == 200000
    assert request.coverages[0].subscribed is True


def test_prompt_includes_certificate_block():
    message = build_user_message(
        "휴대품 한도가 얼마예요?",
        [make_hit("c1")],
        contract_info={
            "coverages": [
                {"name": "해외여행중 휴대품손해(분실제외)", "subscribed": True, "limitAmount": 200000}
            ]
        },
    )

    assert "[가입 정보]" in message
    assert "200,000원" in message


# 요청에 실려 온 coverages가 실제로 프롬프트까지 도달하는지.
# 스키마에 필드만 있고 서비스가 안 읽으면 아무 일도 일어나지 않는데, 답변은
# 그럴듯하게 나오므로 눈으로는 끊긴 걸 알 수 없다.
def test_request_coverages_reach_the_prompt(monkeypatch):
    captured = {}

    monkeypatch.setattr(rag_service, "embed_query", lambda q, client=None: [0.1] * 8)
    monkeypatch.setattr(rag_service, "_call_llm", lambda msg, client=None: "답변 [근거 1]")
    monkeypatch.setattr(
        rag_service,
        "build_user_message",
        lambda *args, **kwargs: captured.setdefault("message", _real_message(*args, **kwargs)),
    )

    repository = FakeVectorRepository([make_hit("c1", document_id="doc-1")])
    request = RagQueryRequest.model_validate(
        {
            "userId": "u",
            "tripId": "t",
            "documentId": "doc-1",
            "question": "항공기 지연 보상되나요?",
            "coverages": [
                {"name": "해외여행중 항공기 및 수하물 지연비용", "subscribed": False}
            ],
        }
    )

    rag_service.answer_question(request, repository)

    assert "[가입 정보]" in captured["message"]
    assert "미가입" in captured["message"]


_real_message = build_user_message


# 증권을 안 보내는 클라이언트(팀원들 기존 경로)는 영향을 받으면 안 된다.
def test_prompt_has_no_certificate_block_without_coverages(monkeypatch):
    captured = {}

    monkeypatch.setattr(rag_service, "embed_query", lambda q, client=None: [0.1] * 8)
    monkeypatch.setattr(rag_service, "_call_llm", lambda msg, client=None: "답변 [근거 1]")
    monkeypatch.setattr(
        rag_service,
        "build_user_message",
        lambda *args, **kwargs: captured.setdefault("message", _real_message(*args, **kwargs)),
    )

    repository = FakeVectorRepository([make_hit("c1", document_id="doc-1")])
    request = RagQueryRequest.model_validate(
        {"userId": "u", "tripId": "t", "documentId": "doc-1", "question": "보상되나요?"}
    )

    rag_service.answer_question(request, repository)

    assert "[가입 정보]" not in captured["message"]


# ── 시스템 프롬프트의 우선순위 규칙 ──────────────────────────


# 이 규칙이 빠지면 LLM이 약관의 "보험가입금액을 한도로"로 증권의 구체적 금액을 덮어쓰고,
# 미가입 담보도 "보상됩니다"라고 답한다. 실제로 이 문구를 넣고서야 답변이 바뀌었다.
def test_system_prompt_states_certificate_priority():
    from app.services.prompt_builder import SYSTEM_PROMPT

    assert "[가입 정보]" in SYSTEM_PROMPT
    assert "우선" in SYSTEM_PROMPT
    assert "가입했으니 무조건 보상된다" in SYSTEM_PROMPT


# ── 담보명 -> 특약명 매칭 ────────────────────────────────────


PATHS = [
    "기본형 실손의료비 특별약관",
    "해외여행중 휴대품손해(분실제외) 특별약관",
    "해외여행중 항공기 및 수하물 지연비용 특별약관",
    "해외여행중 배상책임 특별약관",
]


# 증권은 "특별약관" 꼬리표를 떼고 적는 경우가 많다.
@pytest.mark.parametrize(
    "coverage_name, expected",
    [
        ("기본형 실손의료비", "기본형 실손의료비 특별약관"),
        ("해외여행중 휴대품손해(분실제외)", "해외여행중 휴대품손해(분실제외) 특별약관"),
        # 앞부분("해외여행중")을 생략하는 표기도 흔하다
        ("항공기 및 수하물 지연비용", "해외여행중 항공기 및 수하물 지연비용 특별약관"),
    ],
)
def test_matches_certificate_names_to_clause_paths(coverage_name, expected):
    paths, report = match_coverages([coverage_name], PATHS)

    assert report["matched"][coverage_name] == expected
    assert paths == (expected,)


# 두세 글자 담보명은 어느 특약 이름에나 들어 있어 포함 관계가 근거가 되지 못한다.
#
# 실물 증권에서 "상해"(2글자)가 "해외여행 중 폭력상해피해 변호사선임비용 보장 특별약관"에
# 포함된다는 이유로 매칭됐다. 의료비를 물었는데 변호사비 조항이 근거로 올라온다.
def test_short_coverage_name_does_not_match_by_containment():
    paths = ["해외여행 중 폭력상해피해 변호사선임비용 보장 특별약관", "해외여행 중 배상책임 특별약관"]

    _, report = match_coverages(["상해"], paths)

    assert report["matched"] == {}, "두 글자 담보명이 무관한 특약에 붙었다"


# 네 글자 이상 실제 담보명은 그대로 이어져야 한다. 위 수정으로 같이 죽으면 안 된다.
def test_real_coverage_names_still_match_by_containment():
    paths = ["해외여행 중 배상책임 특별약관", "해외여행 중 폭력상해피해 변호사선임비용 보장 특별약관"]

    _, report = match_coverages(["배상책임"], paths)

    assert report["matched"]["배상책임"] == "해외여행 중 배상책임 특별약관"


# 매칭이 너무 적게 되면 필터를 거는 것이 오히려 해가 된다.
# 근거를 못 찾아 "모르겠습니다"가 나가는데, 그건 필터를 안 걸었을 때보다 나쁘다.
def test_gives_up_filter_when_match_ratio_is_low():
    paths, report = match_coverages(
        ["치과 응급치료", "반려동물 보상", "골프 홀인원"], PATHS
    )

    assert paths is None
    assert "필터 포기" in report["reason"]


# ── 특약 필터 (두 저장소가 같은 규칙이어야 한다) ─────────────


COMMON = {"document_id": "db_travel", "clause_path": ""}
BAGGAGE = {"document_id": "db_travel", "clause_path": "해외여행중 휴대품손해(분실제외) 특별약관"}
MEDICAL = {"document_id": "db_travel", "clause_path": "기본형 실손의료비 특별약관"}


def test_clause_filter_keeps_only_listed_clauses():
    scope = SearchScope(
        document_id="db_travel",
        clause_paths=("해외여행중 휴대품손해(분실제외) 특별약관",),
    )

    assert _matches(BAGGAGE, scope) is True
    assert _matches(MEDICAL, scope) is False


# clause_path가 빈 청크는 보통약관의 공통 조항(청구 절차·용어 정의·일반 면책)이다.
# 어느 특약에도 속하지 않지만 "청구 서류 뭐 필요해요?" 같은 질문의 유일한 근거라
# 필터를 걸어도 항상 통과시켜야 한다. db_travel 222청크 중 34개가 여기 해당한다.
def test_clause_filter_always_keeps_common_clauses():
    scope = SearchScope(document_id="db_travel", clause_paths=("기본형 실손의료비 특별약관",))

    assert _matches(COMMON, scope) is True


def test_no_clause_filter_keeps_everything_in_scope():
    scope = SearchScope(document_id="db_travel")

    assert all(_matches(c, scope) for c in (COMMON, BAGGAGE, MEDICAL))


# 평가는 파일 저장소로 돌리고 서비스는 pgvector로 돈다. 두 규칙이 어긋나면
# 평가에서 검증한 동작이 실제로는 재현되지 않는다.
def test_pg_scope_condition_includes_clause_filter_and_common_clauses():
    condition, params = _scope_condition(
        SearchScope(document_id="doc-1", clause_paths=("특약 A", "특약 B")), prefix="AND"
    )

    assert "clause_path = ANY(%(clause_paths)s)" in condition
    assert "c.clause_path IS NULL" in condition, "공통 조항이 빠지면 청구 절차 질문이 죽는다"
    assert params["clause_paths"] == ["특약 A", "특약 B"]
    assert params["document_id"] == "doc-1"


def test_pg_scope_condition_unchanged_without_clause_filter():
    condition, params = _scope_condition(SearchScope(document_id="doc-1"), prefix="AND")

    assert condition == "AND c.document_id = %(document_id)s"
    assert params == {"document_id": "doc-1"}
