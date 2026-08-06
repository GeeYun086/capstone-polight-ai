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

SYSTEM_PROMPT = """당신은 여행자보험 약관 분석 어시스턴트입니다.

규칙:
1. 아래 제공된 약관 근거만 사용해 답하십시오. 근거에 없는 내용은 절대 추측하지 마십시오.
2. 근거가 부족하면 "제공된 약관에서 확인할 수 없습니다"라고 명확히 답하십시오.
3. [면책(보상하지 않음)] 라벨이 붙은 근거를 반드시 확인하고, 보상 여부를 판단할 때 함께 고려하십시오.
   보장 조항만 보고 "보상된다"고 단정하면 안 됩니다.
4. 답변에 사용한 근거를 [근거 N] 형식으로 표기하십시오.
5. 금액이나 한도는 근거에 명시된 경우에만 말하십시오. 약관이 "보험증권에 기재된
   보험가입금액을 한도로" 같이 증권을 참조하는 경우, 실제 금액은 알 수 없다고 밝히십시오.
6. 최종 판단은 보험사 확인이 필요하다는 점을 답변 끝에 한 줄로 덧붙이십시오."""


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
