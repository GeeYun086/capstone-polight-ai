"""후속 질문을 검색 가능한 독립 질문으로 바꾼다.

멀티턴이 안 되던 이유는 이력을 LLM에게만 주고 검색에는 안 썼기 때문이다.

    사용자: 항공편 지연되면 보상돼요?
    챗봇  : 4시간 이상 지연 시 보상됩니다.
    사용자: 그럼 얼마까지 나와요?      <- 이 문장만으로 검색

"그럼 얼마까지 나와요?"에는 항공기도 지연도 없다. 벡터 검색이 무엇에 대한
질문인지 알 수 없어 엉뚱한 조항(상해 입원의료비 등)을 가져오고, LLM은 잘못된
근거를 받아 그럴듯하게 답한다. 이력을 프롬프트에 넣어봐야 근거가 이미 틀렸다.

그래서 검색 '전에' 질문을 독립적으로 만든다.

    "그럼 얼마까지 나와요?" -> "항공기 지연 보상 한도는 얼마인가요?"

LLM을 한 번 더 부르므로 지연이 는다. 필요할 때만 부르는 것이 중요하다.
"""

import logging
import re

from app.services.answer_providers import generate

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """당신은 검색 질의를 다듬는 도구입니다.

이전 대화를 참고해, 마지막 질문을 그 자체로 이해되는 문장으로 바꾸십시오.

규칙:
1. 지시어("그럼", "그거", "저건", "위에서 말한")를 실제 대상으로 바꾸십시오.
2. 생략된 주어나 대상을 이전 대화에서 찾아 채우십시오.
3. 질문의 의도를 바꾸지 마십시오. 없는 조건을 덧붙이지 마십시오.
4. 이미 그 자체로 이해되는 질문이면 그대로 두십시오.
5. 다시 쓴 질문 한 문장만 출력하십시오. 설명이나 따옴표를 붙이지 마십시오."""

# 이력이 이보다 길면 앞부분을 버린다. 오래된 맥락은 지시어 해석에 도움이 안 되고
# 토큰만 늘린다. Spring도 최근 6개(3턴)만 보내기로 했다.
MAX_TURNS = 6

# 재작성된 질문이 이보다 길면 원문을 쓴다. 모델이 지시를 어기고 설명을 붙인 경우다.
MAX_REWRITE_CHARS = 200

# 이 표현이 없으면 앞 대화를 참조하지 않는 질문으로 본다.
#
# 굳이 판별하는 이유는 LLM 호출 한 번이 1~2초이기 때문이다. 대부분의 질문은
# 그 자체로 완결돼 있어, 매번 재작성하면 모든 질문이 느려진다.
REFERENCE_MARKERS = (
    "그럼", "그러면", "그건", "그거", "그때", "그게", "그런", "그 경우",
    "저건", "저거", "이건", "이거", "위에서", "방금", "아까",
    "말한", "말씀하신", "설명한", "언급한",
    "얼마", "몇", "어떻게", "왜", "언제", "어디",
)


def needs_rewrite(question: str, history: list[dict]) -> bool:
    """재작성이 필요한 질문인지 판단한다.

    이력이 없으면 참조할 것이 없으니 불필요하다.
    지시어나 의문사가 없으면 그 자체로 완결된 질문으로 본다.

    보수적으로 잡는다. 필요 없는데 재작성하면 1~2초 느려지고 끝이지만,
    필요한데 안 하면 엉뚱한 답이 나간다.
    """
    if not history:
        return False
    return any(marker in question for marker in REFERENCE_MARKERS)


def _clean(rewritten: str, original: str) -> str:
    """모델 출력을 다듬고, 못 믿을 결과면 원문으로 되돌린다."""
    text = rewritten.strip().strip('"').strip("'").strip()

    # 여러 줄로 답한 경우 첫 줄만 쓴다. 설명을 덧붙인 경우가 대부분이다.
    text = text.split("\n")[0].strip()

    # "재작성: ..." 같은 접두사를 떼어낸다
    text = re.sub(r"^(재작성|질문|다시 쓴 질문)\s*[:：]\s*", "", text)

    if not text or len(text) > MAX_REWRITE_CHARS:
        logger.warning("재작성 결과를 쓸 수 없어 원문을 씁니다: %r", rewritten[:80])
        return original
    return text


def rewrite(question: str, history: list[dict]) -> str:
    """검색에 쓸 질문을 돌려준다. 재작성이 불필요하거나 실패하면 원문 그대로."""
    if not needs_rewrite(question, history):
        return question

    recent = history[-MAX_TURNS:]
    lines = [
        f"{'사용자' if turn.get('role') == 'user' else '어시스턴트'}: {turn.get('content', '')}"
        for turn in recent
    ]
    user_message = "[이전 대화]\n" + "\n".join(lines) + f"\n\n[마지막 질문]\n{question}"

    try:
        rewritten, seconds = generate(SYSTEM_PROMPT, user_message)
    except Exception as e:
        # 재작성 실패가 질의 전체 실패가 되면 안 된다.
        # 원문으로 검색하면 정확도는 떨어져도 답은 나간다.
        logger.warning("질의 재작성 실패, 원문으로 검색합니다: %s", e)
        return question

    result = _clean(rewritten, question)
    if result != question:
        logger.info("질의 재작성 (%.1f초): %r -> %r", seconds, question, result)
    return result
