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

## [가입 정보]가 함께 주어진 경우

[가입 정보]는 사용자의 증권에서 읽은 개인 계약 내용이고, 약관은 그 상품이 판매할 수
있는 모든 특약을 담은 일반 문서입니다. 둘이 어긋나면 아래 기준으로 판단하십시오.

- **가입 여부, 금액, 한도 → [가입 정보]가 우선입니다.**
  약관에 조항이 있어도 사용자가 그 담보에 "미가입"이면 보상되지 않습니다.
  이때는 "약관에는 있으나 가입하지 않으셨습니다"라고 먼저 밝히십시오.
  약관의 "보험가입금액을 한도로"보다 [가입 정보]에 적힌 실제 금액을 쓰십시오.

- **목록에 없는 담보를 물으면 머리말을 보고 판단하십시오.**
  "보장내용 표 전체"라고 적혀 있으면 목록에 없는 담보는 가입하지 않은 것입니다.
  "가입하지 않으셨습니다"라고 먼저 명확히 답하고, 약관 내용은 참고로만 덧붙이십시오.
  **"가입되어 있는 경우 보상됩니다"처럼 조건부로 답하지 마십시오. 사용자는 보상된다고
  읽고, 실제로는 보험금을 받지 못합니다.**
  그런 표시가 없으면 확인되지 않은 것이므로 미가입으로 단정하지 마십시오.

- **보상 조건, 면책, 청구 절차 → 약관 근거가 기준입니다.**
  [가입 정보]에는 이런 내용이 없으므로 약관 근거로 답하십시오.

가입했더라도 약관의 면책이나 지급 요건은 그대로 적용됩니다.
"가입했으니 무조건 보상된다"고 답하지 마십시오.

[가입 정보]가 주어지지 않았다면 이 항목은 무시하고 약관 근거만으로 답하십시오.

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

    # 목록이 증권 전체인지 일부인지 머리말에서 밝힌다.
    #
    # 전체인데 "단정하지 말라"고 하면, 미가입 담보를 물었을 때 "가입되어 있는 경우
    # 보상받을 수 있습니다"라는 답이 나간다. 사용자는 보상된다고 읽는다.
    # 반대로 일부인데 "없으면 미가입"으로 두면, 파싱이 빠뜨린 담보를 안 된다고 답하게
    # 되어 실제 보장을 못 받는다고 오해시킨다. 그래서 어느 쪽인지 알려줘야 한다.
    complete = bool(contract_info.get("complete"))
    header = (
        "[가입 정보] (증권의 보장내용 표 전체입니다. 약관보다 우선하며, "
        "아래에 없는 담보는 가입하지 않은 것입니다)"
        if complete
        else "[가입 정보] (증권에서 확인된 내용. 약관보다 우선한다)"
    )

    lines = [header]
    for item in contract_info.get("coverages", []):
        status = "가입" if item.get("subscribed", True) else "미가입"
        limit = item.get("limitAmount")
        currency = item.get("limitCurrency") or "원"
        limit_text = f" / 한도 {limit:,}{currency}" if isinstance(limit, int) else ""
        lines.append(f"- {item.get('name')}: {status}{limit_text}")

    if len(lines) == 1:
        return ""

    if not complete:
        lines.append(
            "(위 목록에 없는 담보는 증권에서 확인되지 않은 것이며, 미가입으로 단정하지 마십시오)"
        )

    return "\n".join(lines)


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
