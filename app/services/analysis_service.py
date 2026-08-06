import logging
from pathlib import Path

import httpx

from app.clients import spring_client
from app.clients.spring_client import SpringCallbackError
from app.repositories import VectorRepository, get_vector_repository
from app.repositories.base import ChunkScope
from app.schemas.analysis import AnalysisCompleteCallback, AnalysisFailCallback, CoverageItemPayload
from app.schemas.analysis import AnalysisStartRequest
from app.services.chunking_service import chunk_pages
from app.services.embedding_service import embed_chunks
from app.services.pdf_service import extract_pages

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_PDF_DIR = PROJECT_ROOT / "data" / "raw_pdfs"


def _download_pdf(download_url: str, document_id: str) -> Path:
    RAW_PDF_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = RAW_PDF_DIR / f"{document_id}.pdf"

    with httpx.stream("GET", download_url, timeout=30.0) as response:
        response.raise_for_status()
        with pdf_path.open("wb") as f:
            for data in response.iter_bytes():
                f.write(data)

    return pdf_path


# 콜백 전송 자체의 실패(Spring 다운 등)는 파이프라인 성공/실패와 별개 문제이므로
# 여기서 삼키고 로그만 남긴다. process_analysis의 예외 처리로 되돌리지 않는다.
def _safe_notify_complete(analysis_result_id: str, chunks: list[dict]) -> None:
    payload = AnalysisCompleteCallback(
        analysisResultId=analysis_result_id,
        summary=f"chunk {len(chunks)}개 생성 완료",
        coverageItems=[
            CoverageItemPayload(
                title=chunk["section_title"],
                coverageStatus=chunk["coverage_type"],
                sourceChunkIds=[chunk["chunk_id"]],
            )
            for chunk in chunks
            if chunk.get("matched_category")
        ],
    )
    try:
        spring_client.notify_complete(payload)
    except SpringCallbackError:
        logger.error("완료 콜백 전달 실패 (분석 자체는 성공): %s", analysis_result_id)


def _safe_notify_fail(analysis_result_id: str, error_message: str) -> None:
    payload = AnalysisFailCallback(analysisResultId=analysis_result_id, errorMessage=error_message)
    try:
        spring_client.notify_fail(payload)
    except SpringCallbackError:
        logger.error("실패 콜백 전달도 실패: %s", analysis_result_id)


# BackgroundTasks로 호출되는 진입점.
#
# repository를 인자로 받는 이유는 두 가지다. (1) 테스트에서 가짜 저장소를 넣어 DB/API 없이
# 파이프라인을 검증할 수 있고, (2) pgvector로 갈아탈 때 이 함수를 수정할 필요가 없다.
#
# 저장이 콜백보다 먼저 일어나야 한다. Spring의 coverage_item_sources가 chunk_id를 FK로
# 참조하기 때문에, 청크가 없는 상태에서 완료 콜백을 보내면 Spring 쪽 INSERT가 실패한다.
def process_analysis(
    request: AnalysisStartRequest,
    repository: VectorRepository | None = None,
) -> None:
    repository = repository or get_vector_repository()

    scope = ChunkScope(
        user_id=request.user_id,
        trip_id=request.trip_id,
        policy_id=request.policy_id,
        document_id=request.document_id,
    )

    try:
        pdf_path = _download_pdf(request.download_url, request.document_id)
        pages = extract_pages(pdf_path)
        chunks = chunk_pages(pages, source_file=pdf_path.name, scope=scope)
        embeddings = embed_chunks(chunks)
        repository.save(chunks, embeddings)
    except Exception as e:
        logger.exception("analysis pipeline failed: %s", request.analysis_result_id)
        _safe_notify_fail(request.analysis_result_id, str(e))
        return

    _safe_notify_complete(request.analysis_result_id, chunks)
