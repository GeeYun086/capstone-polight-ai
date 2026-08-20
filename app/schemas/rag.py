from typing import Literal

from app.schemas.base import CamelModel


class SourceChunk(CamelModel):
    chunk_id: str
    document_id: str
    page: int
    quote: str


# 대화 이력 1턴.
#
# Spring이 chat_messages에서 최근 6개(3턴)를 잘라 실어 보낸다. Python이 DB를 조회하지
# 않는 이유는 두 가지다. rag_service 계정에 chat_messages SELECT 권한을 주면 AI 서버가
# 전체 사용자 대화 전문에 접근하게 되고, 무상태로 두면 요청 페이로드만으로 재현되므로
# 디버깅이 쉽다.
#
# sender는 DB CHECK 제약값이라 이 셋만 허용된다.
class HistoryTurn(CamelModel):
    sender: Literal["USER", "ASSISTANT", "SYSTEM"]
    content: str


# 증권에 적힌 담보 1건.
#
# 개인정보 원칙: 판단에 필요한 최소 정보만 받는다. 담보명·가입여부·한도는 필요하지만
# 이름·생년월일·증권번호·연락처는 LLM에 보낼 이유가 없으므로 필드 자체를 두지 않는다.
class CertificateCoverage(CamelModel):
    name: str
    subscribed: bool = True
    # 증권에 인쇄된 실제 한도. 약관에는 없는 값이라 이것이 있어야 금액을 답할 수 있다.
    limit_amount: int | None = None
    limit_currency: str | None = None


# POST /internal/rag/query 요청 바디
class RagQueryRequest(CamelModel):
    user_id: str
    trip_id: str
    question: str

    # 검색 범위. document_id가 있으면 그 약관만, 없으면 trip_id로 여행 단위로 넓힌다.
    #
    # policy_id로 필터하지 않는다. 백엔드에 policies 행을 만드는 코드가 없어 이 값이
    # 항상 null로 오고, SQL에서 "= NULL"은 아무 행과도 일치하지 않아 검색이 0건이 된다.
    # 필드는 남겨두되 스코프로 쓰지 않는다.
    document_id: str | None = None
    policy_id: str | None = None

    # 멀티턴 대화용. 없으면 단발 질의로 동작한다.
    session_id: str | None = None
    history: list[HistoryTurn] = []

    # 증권에서 온 특약명 목록. 있으면 그 조항들로 검색을 좁힌다.
    #
    # 백엔드가 증권 분석 결과의 담보명을 clause_matcher로 특약명에 이어서 실어 보낸다.
    # 비어 있으면 필터가 붙지 않아 기존과 동일하게 동작하므로, 증권 경로가 준비되지
    # 않은 클라이언트도 그대로 쓸 수 있다.
    #
    # 다만 실측 결과 이 필터의 검색 품질 개선 효과는 확인되지 않았다(가상 증권 3종,
    # 답할 수 있는 문항만 비교했을 때 Recall@8 동일). 오답 방지는 아래 coverages로 한다.
    clause_paths: list[str] = []

    # 증권에 적힌 가입 담보. 프롬프트에 실려 LLM의 판단 근거가 된다.
    #
    # 검색 필터보다 이쪽이 중요하다. 약관에는 그 상품이 팔 수 있는 모든 특약이 실려 있어서,
    # 사용자가 가입하지 않은 담보를 물어도 조항이 검색되고 "보상됩니다"라는 틀린 답이 나간다.
    # 가입 여부를 알려주면 "가입하지 않으셨습니다"라고 답할 수 있다.
    #
    # 금액도 마찬가지다. 약관은 "보험가입금액을 한도로"라고만 쓰여 있어 실제 한도를 알 수 없고,
    # 그 값은 증권에만 있다(측정: 약관 추출 30건 중 limitAmount가 채워진 것은 3건뿐).
    coverages: list["CertificateCoverage"] = []

    # coverages가 증권의 보장내용 표 전체인지.
    #
    # 이 구분이 없으면 목록에 없는 담보를 물었을 때 답이 애매해진다. 실측에서
    # 증권에 없는 골프용품손해를 물었더니 "가입되어 있는 경우 보상받을 수 있습니다"라고
    # 답했다. 사용자는 보상된다고 읽는데 실제로는 미가입이다.
    #
    # 그렇다고 항상 "없으면 미가입"으로 두면 반대 사고가 난다. 증권 파싱이 담보를
    # 빠뜨렸을 때 보장되는 담보를 안 된다고 답하게 되고, 그쪽이 더 나쁘다.
    #
    # 그래서 보내는 쪽이 알려준다. 기본값은 false라 기존 동작(단정하지 않음)이 유지된다.
    coverages_complete: bool = False


# POST /internal/rag/query 응답 바디
#
# responseType은 chat_messages.response_type(NOT NULL)에 그대로 들어간다.
# 카드형 4종(HOSPITAL_CARDS 등)은 아직 렌더링하는 화면이 없어 항상 TEXT를 보낸다.
class RagQueryResponse(CamelModel):
    answer: str
    response_type: Literal[
        "TEXT", "HOSPITAL_CARDS", "COVERAGE_CARDS", "EMERGENCY_CONTACTS", "POLICY_SUMMARY"
    ] = "TEXT"
    sources: list[SourceChunk]
