"""보장 항목 추출 결과 스키마.

확정 DDL의 테이블 구조를 그대로 따른다. 이 모델이 나중에 Spring 콜백 확장안의
payload가 되므로, 컬럼과 1:1로 맞춰두면 매핑 레이어가 필요 없다.

  coverage_items         <- CoverageItem 본체
  coverage_detail_items  <- detail_items
  sub_coverage_limits    <- sub_limits
  required_documents     <- required_documents
  exclusion_conditions   <- exclusions
  coverage_item_sources  <- sources
"""

from typing import Literal

from app.schemas.base import CamelModel

# coverage_items.coverage_status VARCHAR(20)
# 백엔드와 확정 전이라 우리 쪽 후보를 먼저 정의한다. 확정되면 이 목록만 맞추면 된다.
# 내부 값. DB에 쓸 때 db_enums.coverage_status가 CHECK 제약값으로 번역한다.
#
# EXCLUDED와 NOT_COVERED를 나누는 이유는 화면 문구가 달라지기 때문이다.
#   EXCLUDED     약관이 명시적으로 배제함 -> "이 약관은 OO를 보상하지 않습니다"
#   NOT_COVERED  약관에 관련 조항이 아예 없음 -> "관련 내용을 찾을 수 없습니다"
# 사용자가 "왜 안 되는지"를 아는 것과 "안 나온다"만 아는 것은 다르다.
CoverageStatus = Literal[
    "COVERED", "PARTIAL", "EXCLUDED", "NOT_COVERED", "UNKNOWN"
]

# exclusion_conditions.severity VARCHAR(20)
Severity = Literal["HIGH", "MEDIUM", "LOW"]

# coverage_item_sources.source_role VARCHAR(30)
# 같은 청크가 역할을 달리해 여러 번 연결될 수 있다(UNIQUE 제약이 item+chunk+role).
# 우리 clause_type과 대응된다: included->COVERAGE, excluded->EXCLUSION, procedure->DOCUMENT
SourceRole = Literal["COVERAGE", "EXCLUSION", "LIMIT", "DOCUMENT"]


class CoverageDetailItem(CamelModel):
    title: str
    subtitle: str | None = None
    is_covered: bool


class SubCoverageLimit(CamelModel):
    label: str
    value: str
    limit_amount: int | None = None
    limit_currency: str | None = None
    description: str | None = None


class RequiredDocument(CamelModel):
    document_name: str
    is_mandatory: bool = True


class ExclusionCondition(CamelModel):
    title: str
    description: str | None = None
    # 약관 원문을 그대로 옮긴다. 지어낸 문장이 근거로 저장되면 안 되므로
    # 추출 후 실제 청크 본문에 존재하는지 검증한다.
    source_text: str | None = None
    severity: Severity = "MEDIUM"


class CoverageSource(CamelModel):
    chunk_id: str
    source_role: SourceRole
    quote_text: str | None = None


class CoverageItem(CamelModel):
    title: str
    subtitle: str | None = None
    category: str
    limit_label: str | None = None
    is_covered: bool
    coverage_status: CoverageStatus
    limit_amount: int | None = None
    limit_currency: str | None = None
    conditions: str | None = None

    detail_items: list[CoverageDetailItem] = []
    sub_limits: list[SubCoverageLimit] = []
    required_documents: list[RequiredDocument] = []
    exclusions: list[ExclusionCondition] = []
    sources: list[CoverageSource] = []
