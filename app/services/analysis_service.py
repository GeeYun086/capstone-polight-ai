import logging
from pathlib import Path

import httpx

from app.clients import spring_client
from app.clients.spring_client import SpringCallbackError
from app.repositories import VectorRepository, get_vector_repository
from app.repositories.base import ChunkScope
from app.schemas.analysis import AnalysisCompleteCallback, AnalysisFailCallback, CoverageItemPayload
from app.schemas.analysis import AnalysisStartRequest
from app.services.chunking_service import parse_and_chunk
from app.services.coverage_extractor import extract_all
from app.services.embedding_service import embed_chunks

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


# 추출된 보장 항목을 현재 확정된 콜백 스키마로 줄여 담는다.
#
# CoverageItem은 DDL의 자식 테이블(세부항목·세부한도·청구서류·면책조건)까지 담고 있지만,
# 합의된 콜백 필드는 아직 4개뿐이라 여기서 잘라낸다.
# 콜백 확장이 합의되면 이 함수가 전체를 그대로 싣도록 바뀐다.
def _to_callback_payload(item, chunk_id_map: dict[str, str]) -> CoverageItemPayload:
    # 저장 시 생성한 policy_chunks.id(UUID)로 바꿔야 Spring이 FK로 연결할 수 있다.
    # 매핑이 없으면(파일 저장소 등) 원래 chunk_id를 그대로 둔다.
    source_ids = [chunk_id_map.get(s.chunk_id, s.chunk_id) for s in item.sources]

    return CoverageItemPayload(
        title=item.title,
        coverageStatus=item.coverage_status,
        limitAmount=item.limit_amount,
        sourceChunkIds=source_ids,
    )


# 청크를 그대로 뒤집어 보내던 방식을 카테고리 기반 추출로 교체했다.
#
# 이전에는 청크 하나가 보장 항목 하나가 돼서, "제33조(약관의 해석)"이나 "손해액 ×" 같은
# 조항 제목 조각이 130개 넘게 나갔다. coverage_items 테이블에 넣어도 화면에 쓸 수 없다.
# 이제는 카테고리별로 조각을 모아 담보 단위로 정리하므로 "해외여행중 휴대품손해 특별약관"
# 같은 실제 담보명이 나오고 한도 금액도 뽑힌다.
#
# 콜백 전송 자체의 실패(Spring 다운 등)는 파이프라인 성공/실패와 별개 문제이므로
# 여기서 삼키고 로그만 남긴다. process_analysis의 예외 처리로 되돌리지 않는다.
def _safe_notify_complete(
    analysis_result_id: str,
    chunks: list[dict],
    chunk_id_map: dict[str, str] | None = None,
) -> None:
    items, warnings = extract_all(chunks)
    for warning in warnings:
        logger.warning("보장항목 추출 경고: %s", warning)

    payload = AnalysisCompleteCallback(
        analysisResultId=analysis_result_id,
        summary=f"보장 항목 {len(items)}개 추출 완료 (청크 {len(chunks)}개)",
        coverageItems=[_to_callback_payload(item, chunk_id_map or {}) for item in items],
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
        chunks = parse_and_chunk(pdf_path, scope=scope)
        embeddings = embed_chunks(chunks)
        repository.save(
            chunks,
            embeddings,
            analysis_result_id=request.analysis_result_id,
            scope=scope,
        )
    except Exception as e:
        logger.exception("analysis pipeline failed: %s", request.analysis_result_id)
        _safe_notify_fail(request.analysis_result_id, str(e))
        return

    # pgvector 저장소는 INSERT하며 policy_chunks.id(UUID)를 만들고 청크에 실어준다.
    # coverage_item_sources가 그 UUID를 FK로 참조하므로 콜백에 그대로 전달한다.
    # 저장소 인스턴스가 아니라 청크에서 읽는 이유는, 저장소가 싱글턴이라
    # 동시 분석 시 인스턴스 상태를 쓰면 서로 덮어쓰기 때문이다.
    chunk_id_map = {
        c["chunk_id"]: c["policy_chunk_id"] for c in chunks if c.get("policy_chunk_id")
    }
    _safe_notify_complete(request.analysis_result_id, chunks, chunk_id_map)
