from typing import Literal

from app.schemas.base import CamelModel


# Spring -> Python: 분석 시작 요청. Python이 Spring을 재조회하지 않도록 필요한 정보를 모두 담아 보낸다.
class AnalysisStartRequest(CamelModel):
    analysis_result_id: str
    document_id: str
    download_url: str
    user_id: str
    trip_id: str
    policy_id: str


class CoverageItemPayload(CamelModel):
    title: str
    coverage_status: str
    limit_amount: int | None = None
    source_chunk_ids: list[str] = []


# Python -> Spring: 분석 완료 콜백. Spring이 이 데이터를 받아 하나의 트랜잭션으로 저장한다.
class AnalysisCompleteCallback(CamelModel):
    analysis_result_id: str
    status: Literal["COMPLETED"] = "COMPLETED"
    summary: str
    coverage_items: list[CoverageItemPayload]


# Python -> Spring: 분석 실패 콜백.
class AnalysisFailCallback(CamelModel):
    analysis_result_id: str
    status: Literal["FAILED"] = "FAILED"
    error_message: str
