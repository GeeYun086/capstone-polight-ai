from fastapi import APIRouter, BackgroundTasks, status

from app.schemas.analysis import AnalysisStartRequest
from app.services.analysis_service import process_analysis

router = APIRouter(tags=["analysis"])


# 요청을 받으면 즉시 202로 접수 응답하고, 실제 처리(다운로드/추출/청킹/임베딩)는 백그라운드에서 진행한다.
# 완료/실패는 Spring의 /internal/analysis-results/{id}/complete|fail 콜백으로 알린다.
@router.post(
    "/analysis",
    status_code=status.HTTP_202_ACCEPTED,
    summary="약관 분석 요청 접수",
    description="downloadUrl의 PDF를 비동기로 다운로드/추출/청킹/임베딩한다. "
    "즉시 202로 접수만 응답하고, 최종 결과는 Spring 콜백으로 통지한다.",
)
def start_analysis(request: AnalysisStartRequest, background_tasks: BackgroundTasks) -> dict[str, str]:
    background_tasks.add_task(process_analysis, request)
    return {
        "analysisResultId": request.analysis_result_id,
        "status": "ACCEPTED",
    }
