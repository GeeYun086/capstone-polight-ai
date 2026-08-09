from app.repositories.base import ChunkHit

# coverage_type을 LLM이 읽을 수 있는 한국어 라벨로 바꾼다.
# 라벨을 명시하지 않으면 LLM이 보장 조항과 면책 조항을 구분하지 못해
# 면책 조항을 근거로 "보상됩니다"라고 답하는 사고가 난다.
COVERAGE_TYPE_LABELS = {
    "included": "보장",
    "excluded": "면책(보상하지 않음)",
    "procedure": "청구절차",
    "definition": "용어정의",
}

SYSTEM_PROMPT_V1 = """당신은 여행자보험 약관 분석 어시스턴트입니다.

규칙:
1. 아래 제공된 약관 근거만 사용해 답하십시오. 근거에 없는 내용은 절대 추측하지 마십시오.
2. 근거가 부족하면 "제공된 약관에서 확인할 수 없습니다"라고 명확히 답하십시오.
3. [면책(보상하지 않음)] 라벨이 붙은 근거를 반드시 확인하고, 보상 여부를 판단할 때 함께 고려하십시오.
   보장 조항만 보고 "보상된다"고 단정하면 안 됩니다.
4. 답변에 사용한 근거를 [근거 N] 형식으로 표기하십시오.
5. 금액이나 한도는 근거에 명시된 경우에만 말하십시오. 약관이 "보험증권에 기재된
   보험가입금액을 한도로" 같이 증권을 참조하는 경우, 실제 금액은 알 수 없다고 밝히십시오.
6. 최종 판단은 보험사 확인이 필요하다는 점을 답변 끝에 한 줄로 덧붙이십시오."""


# 현행 시스템 프롬프트. 아래 v1은 비교 실험용으로 남겨둔다.
#
# 모델 비교에서 나온 관찰을 반영한 개정판이다.
#
# 12문항을 6개 모델로 돌려보니 틀린 답은 한 건도 없었다. 갈린 것은 정확성이 아니라
# 완전성이었다. 근거를 8개 주면 저가 모델은 3개만 쓰고 끝내는 반면, 상위 모델은
# 8개를 다 활용해 조건과 예외까지 챙겼다.
#
# 실제로 놓친 것들:
#   식중독  "2일 이상 입원 시 보상"만 말하고, 특약 가입이 전제라는 점과
#           입원일수가 약관에 공란이라 증권을 봐야 한다는 점을 빠뜨림
#   배상책임 "증권 기재 금액이 한도"만 말하고, 자기부담금 초과분만 보상된다는
#           핵심 조건을 빠뜨림
#
# 보험에서 조건을 빠뜨리는 것은 틀리는 것과 같다. 사용자가 "보상된다"만 읽고
# 특약 미가입이나 자기부담금을 모른 채 기대를 갖게 되기 때문이다.
#
# 그래서 v2는 두 가지를 추가로 지시한다. 근거를 빠짐없이 쓰라는 것과,
# 답변을 결론-요건-예외 구조로 쓰라는 것이다. 구조를 지정하면 모델이
# 각 칸을 채우려 하므로 누락이 줄어든다.
SYSTEM_PROMPT = """당신은 여행자보험 약관 분석 어시스턴트입니다.

## 지켜야 할 규칙

1. 아래 제공된 약관 근거만 사용해 답하십시오. 근거에 없는 내용은 절대 추측하지 마십시오.
2. 근거가 부족하면 "제공된 약관에서 확인할 수 없습니다"라고 명확히 답하십시오.
3. [면책(보상하지 않음)] 라벨이 붙은 근거를 반드시 확인하고, 보상 여부를 판단할 때
   함께 고려하십시오. 보장 조항만 보고 "보상된다"고 단정하면 안 됩니다.
4. 답변에 사용한 근거를 [근거 N] 형식으로 표기하십시오.
5. 최종 판단은 보험사 확인이 필요하다는 점을 답변 끝에 한 줄로 덧붙이십시오.

## 빠뜨리면 안 되는 것

제공된 근거에 아래 내용이 있으면 반드시 답변에 포함하십시오.
사용자가 이것을 모르고 "보상된다"고만 믿으면 실제로 보험금을 못 받습니다.

- **가입 전제**: 특별약관 가입이 있어야 보장되는 경우, 그 사실
- **지급 요건**: 입원 일수, 지연 시간, 사고 유형 등 충족해야 할 조건
- **자기부담금**: 공제 후 지급되는 경우, 그 사실
- **한도**: 보상한도액과, 그것이 증권에 기재된다면 증권 확인이 필요하다는 점
- **면책**: 같은 상황에서 보상되지 않는 예외
- **약관에 공란인 값**: "( )일 이상"처럼 비어 있으면, 증권에서 확인해야 한다는 점

근거를 여러 개 받았다면 관련된 것을 모두 활용하십시오. 일부만 읽고 답하지 마십시오.

## 답변 형식

**결론**을 먼저 한두 문장으로 쓰고, 그 아래에 근거를 정리하십시오.

질문에 여러 경우가 얽혀 있으면(예: 사고 유형별로 필요 서류가 다름)
경우별로 소제목을 나눠 각각 정리하십시오.

내용이 짧으면 굳이 나누지 말고 문단 하나로 쓰십시오. 형식을 채우려고
빈 항목을 만들지 마십시오."""


# 검색된 청크를 LLM이 읽을 컨텍스트 문자열로 만든다 (파이프라인 B의 ⑦).
def format_evidence(hits: list[ChunkHit]) -> str:
    blocks = []
    for i, hit in enumerate(hits, start=1):
        label = COVERAGE_TYPE_LABELS.get(hit.coverage_type, hit.coverage_type)
        pages = (
            f"{hit.page_start}p"
            if hit.page_start == hit.page_end
            else f"{hit.page_start}~{hit.page_end}p"
        )
        blocks.append(
            f"[근거 {i}] [{label}] {hit.section_title} ({pages})\n{hit.text}"
        )
    return "\n\n".join(blocks)


# 가입 담보 정보(②)를 컨텍스트로 만든다.
#
# 지금은 호출하는 쪽에서 항상 None을 넘긴다. Spring이 가입 내역을 요청 바디에 실어주기로
# 합의되면 값이 채워진다. 이 자리를 미리 열어두는 이유는, 없으면 "차차 개선"이 아니라
# "나중에 재작성"이 되기 때문이다.
#
# 개인정보 원칙: 판단에 필요한 최소 정보만 넣는다. 담보명/한도/가입여부는 필요하지만
# 이름·생년월일·증권번호·연락처는 LLM에 보낼 이유가 없다.
def format_contract_info(contract_info: dict | None) -> str:
    if not contract_info:
        return ""

    lines = ["[가입 정보]"]
    for item in contract_info.get("coverages", []):
        status = "가입" if item.get("subscribed") else "미가입"
        limit = item.get("limitAmount")
        limit_text = f" / 한도 {limit:,}원" if isinstance(limit, int) else ""
        lines.append(f"- {item.get('name')}: {status}{limit_text}")

    return "\n".join(lines) if len(lines) > 1 else ""


# 대화 히스토리(③)를 컨텍스트로 만든다.
# contract_info와 같은 이유로 자리만 열어둔다. RagQueryRequest에 히스토리 필드가 없어
# 현재는 항상 None이며, Spring의 chat_messages를 넘겨받기로 합의되면 채워진다.
def format_history(history: list[dict] | None) -> str:
    if not history:
        return ""

    lines = ["[이전 대화]"]
    for turn in history:
        role = "사용자" if turn.get("role") == "user" else "어시스턴트"
        lines.append(f"{role}: {turn.get('content', '')}")

    return "\n".join(lines)


# 3종(약관 근거 / 가입 정보 / 대화 맥락)을 병합해 최종 user 메시지를 만든다 (파이프라인 B의 ⑧).
def build_user_message(
    question: str,
    hits: list[ChunkHit],
    contract_info: dict | None = None,
    history: list[dict] | None = None,
) -> str:
    sections = []

    contract_block = format_contract_info(contract_info)
    if contract_block:
        sections.append(contract_block)

    history_block = format_history(history)
    if history_block:
        sections.append(history_block)

    sections.append(f"[약관 근거]\n{format_evidence(hits)}")
    sections.append(f"[질문]\n{question}")

    return "\n\n".join(sections)
