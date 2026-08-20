"""후속 질문 재작성 검증.

멀티턴이 안 되던 이유는 이력을 LLM에게만 주고 검색에는 안 썼기 때문이다.
"그럼 얼마까지 나와요?"에는 항공기도 지연도 없어 벡터 검색이 엉뚱한 조항을
가져오고, 이력을 프롬프트에 넣어봐야 근거가 이미 틀렸다.

LLM 호출은 전부 대체한다. 여기서 확인할 것은 "언제 부르고 무엇을 쓰는가"다.
"""

import pytest

from app.services import query_rewriter
from app.services.query_rewriter import MAX_REWRITE_CHARS, needs_rewrite, rewrite

HISTORY = [
    {"role": "user", "content": "항공편 지연되면 보상돼요?"},
    {"role": "assistant", "content": "4시간 이상 지연 시 보상됩니다."},
]


@pytest.fixture
def fake_llm(monkeypatch):
    """generate를 대체하고 호출 횟수를 센다."""
    calls: list[str] = []

    def make(answer: str):
        def fake(system, user, provider_name=None):
            calls.append(user)
            return answer, 0.1
        monkeypatch.setattr(query_rewriter, "generate", fake)
        return calls

    return make


# ── 언제 부르는가 ────────────────────────────────────────────


# 호출 한 번이 1~2초다. 대부분의 질문은 그 자체로 완결돼 있어,
# 매번 재작성하면 모든 질문이 느려진다.
def test_no_history_means_no_call(fake_llm):
    calls = fake_llm("쓰이면 안 됨")

    assert rewrite("항공편 지연되면 보상돼요?", []) == "항공편 지연되면 보상돼요?"
    assert not calls


def test_self_contained_question_skips_rewrite(fake_llm):
    calls = fake_llm("쓰이면 안 됨")

    assert rewrite("임신 치료비가 보상되나요?", HISTORY) == "임신 치료비가 보상되나요?"
    assert not calls


@pytest.mark.parametrize("question", [
    "그럼 얼마까지 나와요?",
    "그건 언제까지 청구해야 해요?",
    "위에서 말한 조건이 뭐였죠?",
    "얼마나 받을 수 있어요?",
])
def test_follow_up_questions_are_rewritten(fake_llm, question):
    calls = fake_llm("항공기 지연 보상 한도는 얼마인가요?")

    result = rewrite(question, HISTORY)

    assert calls, f"{question!r}는 재작성되어야 한다"
    assert result == "항공기 지연 보상 한도는 얼마인가요?"


# ── 이력 처리 ────────────────────────────────────────────────


# 오래된 맥락은 지시어 해석에 도움이 안 되고 토큰만 늘린다.
def test_only_recent_turns_are_sent(fake_llm):
    calls = fake_llm("재작성됨")
    long_history = [{"role": "user", "content": f"질문{i}"} for i in range(20)]

    rewrite("그럼 얼마예요?", long_history)

    assert "질문19" in calls[0]
    assert "질문0" not in calls[0], "오래된 이력까지 보내면 토큰만 늘어난다"


# ── 결과를 못 믿을 때 ────────────────────────────────────────


# 재작성 실패가 질의 전체 실패가 되면 안 된다.
# 원문으로 검색하면 정확도는 떨어져도 답은 나간다.
def test_llm_failure_falls_back_to_original(monkeypatch):
    def boom(system, user, provider_name=None):
        raise RuntimeError("API 장애")

    monkeypatch.setattr(query_rewriter, "generate", boom)

    assert rewrite("그럼 얼마까지 나와요?", HISTORY) == "그럼 얼마까지 나와요?"


# 모델이 지시를 어기고 설명을 덧붙이는 경우가 있다.
def test_multiline_answer_uses_first_line(fake_llm):
    fake_llm("항공기 지연 보상 한도는?\n\n이렇게 바꾼 이유는 지시어를 풀었기 때문입니다.")

    assert rewrite("그럼 얼마예요?", HISTORY) == "항공기 지연 보상 한도는?"


@pytest.mark.parametrize("raw,expected", [
    ('"항공기 지연 보상 한도는?"', "항공기 지연 보상 한도는?"),
    ("재작성: 항공기 지연 보상 한도는?", "항공기 지연 보상 한도는?"),
    ("질문: 항공기 지연 보상 한도는?", "항공기 지연 보상 한도는?"),
])
def test_wrappers_are_stripped(fake_llm, raw, expected):
    fake_llm(raw)

    assert rewrite("그럼 얼마예요?", HISTORY) == expected


# 길게 답했다면 지시를 어기고 설명을 쓴 것이다. 그걸로 검색하면 더 나빠진다.
def test_overlong_answer_falls_back_to_original(fake_llm):
    fake_llm("가" * (MAX_REWRITE_CHARS + 1))

    assert rewrite("그럼 얼마예요?", HISTORY) == "그럼 얼마예요?"


def test_empty_answer_falls_back_to_original(fake_llm):
    fake_llm("   ")

    assert rewrite("그럼 얼마예요?", HISTORY) == "그럼 얼마예요?"


def test_needs_rewrite_is_pure():
    assert needs_rewrite("그럼 얼마예요?", HISTORY)
    assert not needs_rewrite("그럼 얼마예요?", [])
