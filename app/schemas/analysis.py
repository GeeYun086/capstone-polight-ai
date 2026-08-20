from typing import Literal

from pydantic import AliasChoices, Field

from app.schemas.base import CamelModel


# Spring -> Python: 분석 시작 요청. Python이 Spring을 재조회하지 않도록 필요한 정보를 모두 담아 보낸다.
#
# policy_id가 optional인 이유: 백엔드에 policies 행을 만드는 코드가 없어 항상 null이 온다.
# DDL도 nullable이라 저장은 하되 검색 스코프로는 쓰지 않는다.
class AnalysisStartRequest(CamelModel):
    analysis_result_id: str
    document_id: str
    user_id: str
    trip_id: str
    policy_id: str | None = None

    # downloadUrl과 fileUrl을 둘 다 받는다.
    #
    # 백엔드 답변서의 요청 예시는 fileUrl인데 우리 스키마는 downloadUrl이었다.
    # 어느 한쪽으로 맞추자고 논의하는 대신 둘 다 받으면, 그쪽이 어느 이름으로
    # 보내든 동작하고 나중에 바꿔도 깨지지 않는다. 이름 하나 때문에 첫 요청이
    # 422로 튕기고 원인을 찾는 시간이 아깝다.
    download_url: str = Field(validation_alias=AliasChoices("downloadUrl", "fileUrl", "download_url"))

    # 약관이냐 증권이냐. 처리 경로가 완전히 다르다.
    #
    #   TERMS        파싱 -> 청킹 -> 임베딩 -> 저장. 챗봇이 검색할 근거가 된다
    #   CERTIFICATE  Studio Agent -> 담보·금액 -> 보장 카드. 화면에 뜨는 내용이 된다
    #
    # 기본값을 TERMS로 둔 이유: 백엔드가 이 필드를 보내지 않아도 지금과 똑같이 동작해야
    # 한다. 백엔드는 다른 작업 중이라 아직 증권 경로가 없고, 그쪽 일정에 우리가 묶이면
    # 안 된다. 안 보내도 페이지 수로 자동 판별하므로 실제로는 대개 맞는다.
    document_type: Literal["TERMS", "CERTIFICATE"] | None = None


# ── 자식 배열 ────────────────────────────────────────────────
#
# 아래 4종은 coverage_items의 자식 테이블에 그대로 들어간다. 이전 콜백에는 통로가 없어
# 추출은 해놓고 버리고 있었다(면책 38건, 청구서류 23건).
#
# sortOrder는 어디에도 넣지 않는다. Spring이 배열 인덱스로 부여하기로 합의했다.
# 우리가 번호를 보내면 배열 순서와 어긋날 때 원인 추적이 어려워지므로,
# 대신 배열을 화면에 보여줄 순서(중요도 순)로 정렬해 보낸다.


class DetailItemPayload(CamelModel):
    title: str
    subtitle: str | None = None
    is_covered: bool


class SubLimitPayload(CamelModel):
    label: str
    value: str
    limit_amount: int | None = None
    limit_currency: str | None = None
    description: str | None = None


class RequiredDocumentPayload(CamelModel):
    document_name: str
    is_mandatory: bool = True


class ExclusionPayload(CamelModel):
    title: str
    description: str | None = None
    source_text: str | None = None
    # GENERAL | WARNING | CRITICAL (DB CHECK 제약)
    severity: str


class SourcePayload(CamelModel):
    """근거 조항 1건.

    chunk_id는 policy_chunks INSERT 때 Python이 만든 UUID다. 그래서 저장 순서가
    policy_chunks -> 콜백이어야 한다. 반대로 하면 coverage_item_sources의 FK가 깨진다.

    UNIQUE(coverage_item_id, policy_chunk_id, source_role)이 걸려 있어
    같은 담보에 같은 조각을 같은 역할로 두 번 실으면 저장이 실패한다.
    """

    chunk_id: str
    # PRIMARY | CONDITION | EXCLUSION | LIMIT | PROCEDURE | REQUIRED_DOCUMENT | DEFINITION
    source_role: str
    quote_text: str | None = None


# coverage_items 1행 + 자식 테이블 전체.
#
# isCovered를 보내지 않는다. coverage_status에서 Spring이 파생한다.
#   is_covered = coverage_status IN (COVERED, PARTIALLY_COVERED)
# 우리가 둘 다 보내면 어긋날 여지가 생기고, PARTIALLY_COVERED를 false로 잘못
# 계산하면 부분 보장 담보가 화면에서 "미보장"으로 표시된다.
class CoverageItemPayload(CamelModel):
    title: str
    # COVERED | PARTIALLY_COVERED | NOT_COVERED | EXCLUDED (DB CHECK 제약)
    coverage_status: str

    subtitle: str | None = None
    category: str | None = None
    limit_label: str | None = None
    # BIGINT 컬럼이라 정수만 들어간다. "1,000만원" 같은 문자열은 limit_label로 보낸다.
    limit_amount: int | None = None
    limit_currency: str | None = None
    conditions: str | None = None

    detail_items: list[DetailItemPayload] = []
    sub_limits: list[SubLimitPayload] = []
    required_documents: list[RequiredDocumentPayload] = []
    exclusions: list[ExclusionPayload] = []
    sources: list[SourcePayload] = []


# Python -> Spring: 분석 완료 콜백. Spring이 이 데이터를 받아 하나의 트랜잭션으로 저장한다.
#
# summary는 analysis_results.summary에 들어가며, 현재 프론트에 노출되는 유일한 분석
# 텍스트다. embedding_model/dimension과 raw_result_json은 analysis_results에 자리가
# 있는데 비어 있던 컬럼들로, 디버깅과 재처리에 쓰인다.
class AnalysisCompleteCallback(CamelModel):
    analysis_result_id: str
    status: Literal["COMPLETED"] = "COMPLETED"
    summary: str
    coverage_items: list[CoverageItemPayload]

    embedding_model: str | None = None
    embedding_dimension: int | None = None
    raw_result_json: str | None = None

    # 증권에서 읽은 보험사·상품명. 백엔드가 이걸로 약관을 찾아 연결한다.
    # 약관 분석일 때는 비어 있다. 자세한 내용은 docs/BACKEND_INTERFACE.md 3-1.
    insurer_name: str | None = None
    product_name: str | None = None


# Python -> Spring: 분석 실패 콜백.
class AnalysisFailCallback(CamelModel):
    analysis_result_id: str
    status: Literal["FAILED"] = "FAILED"
    error_message: str
