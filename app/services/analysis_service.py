import json
import logging
import time
from pathlib import Path

import fitz
import httpx

from app.clients import spring_client
from app.clients.spring_client import SpringCallbackError
from app.core.auth import HEADER_NAME
from app.core.config import get_settings
from app.repositories import VectorRepository, get_vector_repository
from app.repositories.base import ChunkScope
from app.schemas.analysis import (
    AnalysisCompleteCallback,
    AnalysisFailCallback,
    AnalysisStartRequest,
)
from app.services.analysis_errors import AnalysisFailure
from app.services.callback_mapper import to_payload
from app.services.certificate_adapter import (
    coverages_complete,
    describe_structure,
    insurance_period,
    looks_like_certificate,
    to_payloads,
)
from app.services.certificate_analyzer import CertificateAnalysisError, analyze_certificate
from app.services.chunking_service import parse_and_chunk
from app.services.coverage_extractor import extract_all
from app.services.embedding_providers import get_provider
from app.services.embedding_service import embed_chunks

# policy_chunks.embedding이 vector(1536)이고, 우리가 쓰는 upstage-1536도 1536이다.
# provider.dimensions가 비어 있는(축소하지 않는) 벤더를 쓸 때의 기본값.
DEFAULT_EMBEDDING_DIMENSION = 1536

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_PDF_DIR = PROJECT_ROOT / "data" / "raw_pdfs"


# 약관 PDF를 받아온다.
#
# 주소가 어떤 형태로 올지 확정되지 않았다. S3 presigned URL이면 인증이 필요 없고,
# Spring이 직접 파일을 내려주는 엔드포인트면 내부 API 키가 필요하다.
# 어느 쪽인지 물어보고 기다리는 대신, 키가 있으면 항상 헤더에 실어 보낸다.
# presigned URL은 서명에 포함되지 않은 헤더를 무시하므로 있어도 무해하다.
#
# 재시도를 두는 이유는 이 단계 실패가 곧 분석 전체 실패이기 때문이다.
# 일시적인 네트워크 문제로 약관 하나를 통째로 날리는 것은 아깝다.
DOWNLOAD_MAX_ATTEMPTS = 3


def _download_pdf(download_url: str, document_id: str) -> Path:
    RAW_PDF_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = RAW_PDF_DIR / f"{document_id}.pdf"

    settings = get_settings()
    headers = (
        {HEADER_NAME: settings.internal_api_key} if settings.internal_api_key else {}
    )

    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            with httpx.stream(
                "GET", download_url, headers=headers, timeout=60.0, follow_redirects=True
            ) as response:
                response.raise_for_status()
                with pdf_path.open("wb") as f:
                    for data in response.iter_bytes():
                        f.write(data)
            return pdf_path
        except httpx.HTTPError as e:
            last_error = e
            # 4xx는 다시 받아도 같다. 주소가 틀렸거나 만료됐거나 권한이 없다.
            if isinstance(e, httpx.HTTPStatusError) and 400 <= e.response.status_code < 500:
                break
            if attempt < DOWNLOAD_MAX_ATTEMPTS:
                logger.warning("PDF 내려받기 실패 (%d/%d): %s", attempt, DOWNLOAD_MAX_ATTEMPTS, e)
                time.sleep(2.0 * attempt)

    # 주소를 사용자 문구에 넣지 않는다. presigned URL에는 서명이 붙어 있고,
    # 실패 사유는 analysis_results.failure_reason에 영구 저장된 뒤 화면에 뜬다.
    raise AnalysisFailure(
        f"PDF를 내려받지 못했습니다 ({download_url}): {last_error}",
        user_message="문서를 내려받지 못했습니다. 잠시 후 다시 시도해 주세요.",
    )


# 암호가 걸린 PDF를 여기서 처리한다.
#
# PDF 암호는 두 종류이고, 우리에게 문제가 되는 것은 하나뿐이다.
#
#   권한(소유자) 암호   편집·인쇄만 제한. needs_pass=0이고 본문이 읽힌다. 그냥 통과
#   열기(사용자) 암호   needs_pass=1. 본문을 못 읽는다. 이것만 처리 대상
#
# 보험사 증권에서 "보호된 문서"로 보이는 것 상당수가 권한 암호만이라 지금도
# 정상 동작한다.
#
# 열기 암호는 그냥 두면 조용히 망가진다. fitz.open()이 성공하고 page_count까지
# 읽혀 라우팅을 통과하고, 잠긴 파일이 Upstage에 올라가 본문이 빈다. 담보 0건 ->
# "증권 원본 파일인지 확인해 주세요"가 나가는데 파일은 맞고 잠겨 있을 뿐이라
# 사용자는 같은 파일을 계속 올린다. 매번 에이전트 호출 비용도 나간다.
#
# 암호를 받으면 복호화한다. 사용자가 뷰어에서 암호를 없애는 것은 유료 기능이라
# 실질적으로 어렵지만, 사용자는 암호를 알고 있다. 파일을 고치게 하는 대신 암호만
# 받는 편이 낫다. 추측은 하지 않는다 - 생년월일로 열리는 경우가 많아도 그 값은
# 우리에게 없고, 있다고 해도 개인정보로 잠금을 자동으로 풀 일은 아니다.
def _unlock_or_reject(pdf_path: Path, password: str | None) -> None:
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        # 열지도 못하는 것은 다른 문제다(손상·PDF 아님). 뒤 단계에서 드러난다.
        logger.warning("PDF를 열어보지 못해 잠금 여부를 확인하지 못했습니다: %s", e)
        return

    unlocked: Path | None = None
    try:
        if not doc.needs_pass:
            return

        if not password:
            raise AnalysisFailure(
                f"열기 암호가 걸린 PDF인데 비밀번호가 오지 않았습니다 ({pdf_path.name})",
                user_message=(
                    "비밀번호가 설정된 파일입니다. 비밀번호를 입력하거나 해제한 뒤 "
                    "다시 시도해 주세요."
                ),
            )

        if not doc.authenticate(password):
            # 암호를 로그에 남기지 않는다. 틀렸다는 사실만 남긴다.
            raise AnalysisFailure(
                f"PDF 비밀번호가 맞지 않습니다 ({pdf_path.name})",
                user_message="비밀번호가 맞지 않습니다. 다시 확인해 주세요.",
            )

        # 복호화본을 만들어 원본 자리에 덮어쓴다. 뒤 단계와 정리 코드가 경로를
        # 하나만 알면 되도록 파일을 늘리지 않는다.
        unlocked = pdf_path.with_suffix(".unlocked.pdf")
        doc.save(str(unlocked), encryption=fitz.PDF_ENCRYPT_NONE)
    finally:
        # 윈도우에서는 열려 있는 파일을 덮어쓸 수 없어 먼저 닫는다.
        doc.close()

    if unlocked:
        unlocked.replace(pdf_path)
        logger.info("잠긴 PDF를 복호화했습니다: %s", pdf_path.name)


# 화면에 노출되는 요약 문구.
#
# analysis_results.summary는 현재 프론트에 보이는 유일한 분석 텍스트다.
# "청크 268개" 같은 내부 수치를 쓰면 사용자에게 의미가 없으므로 담보 이름을 넣는다.
def _build_summary(items: list) -> str:
    if not items:
        return "약관에서 보장 항목을 찾지 못했습니다. 약관 형식을 확인해 주세요."

    names = ", ".join(item.title for item in items[:3])
    if len(items) > 3:
        names += f" 외 {len(items) - 3}건"
    return f"보장 항목 {len(items)}개를 확인했습니다: {names}"


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

    payloads = [to_payload(item, chunk_id_map or {}) for item in items]

    settings = get_settings()
    provider = get_provider(settings.embedding_provider)
    payload = AnalysisCompleteCallback(
        analysisResultId=analysis_result_id,
        summary=_build_summary(items),
        coverageItems=payloads,
        # analysis_results에 자리가 있는데 비어 있던 컬럼들.
        # 어떤 모델로 만든 임베딩인지 남겨두지 않으면, 모델을 바꿨을 때
        # 어느 문서를 재색인해야 하는지 알 수 없다.
        embeddingModel=provider.doc_model,
        embeddingDimension=provider.dimensions or DEFAULT_EMBEDDING_DIMENSION,
        # 디버깅·재처리용 원본. Spring이 파싱하지 않고 TEXT로 보관한다.
        rawResultJson=json.dumps(
            [p.model_dump(by_alias=True) for p in payloads], ensure_ascii=False
        ),
    )
    try:
        spring_client.notify_complete(payload)
    except SpringCallbackError:
        logger.error("완료 콜백 전달 실패 (분석 자체는 성공): %s", analysis_result_id)


# 증권은 1~2페이지, 약관은 100페이지를 넘는다. 이 정도면 겹치지 않는다.
#
# 백엔드가 documentType을 보내주면 그걸 쓰고, 안 보내도 동작하게 하려는 안전망이다.
# 백엔드는 다른 작업 중이라 아직 증권 경로가 없는데, 그쪽 일정에 우리가 묶이면 안 된다.
CERTIFICATE_MAX_PAGES = 10


def _remove_quietly(path: Path) -> None:
    """파일을 지운다. 삭제 실패로 분석 결과를 버릴 수는 없으니 예외는 삼킨다."""
    try:
        path.unlink(missing_ok=True)
        logger.info("내려받은 증권 삭제: %s", path.name)
    except OSError as e:
        logger.warning("내려받은 증권을 지우지 못했습니다 (%s): %s", path, e)


# 두께로 증권과 약관을 가르려 했으나 폐기했다.
#
# 현대해상은 증권 2장과 약관 153장을 한 파일로 발급한다(실물 157페이지). 두께로
# 보면 약관인데 실제로는 정상 증권이다. 오분류 판정은 에이전트가 증권 고유 항목을
# 뽑았는지로 한다(certificate_adapter.looks_like_certificate).
#
# 페이지 수는 진단 정보로만 남긴다.


def _page_count(pdf_path: Path) -> int | None:
    """페이지 수. 못 세면 None. 세는 데 실패한 것으로 분석을 멈추지는 않는다."""
    try:
        with fitz.open(pdf_path) as doc:
            return doc.page_count
    except Exception as e:
        logger.warning("페이지 수를 세지 못했습니다: %s", e)
        return None


def _resolve_document_type(request: AnalysisStartRequest, pdf_path: Path) -> str:
    """약관인지 증권인지 정한다. 요청에 명시돼 있으면 그것을 믿는다."""
    if request.document_type:
        # 믿는다. 다만 몇 페이지가 들어왔는지는 남긴다.
        #
        # 증권이 두꺼우면 오분류일 수도 있고(프론트가 documentKind를 보내지 않아
        # 백엔드 기본값 CERTIFICATE로 흘러온 경우), 약관 합본일 수도 있다.
        # 두 경우를 두께로는 구분할 수 없으니 판정하지 않고 기록만 한다.
        # 합본이면 에이전트에 약관 150여 장이 함께 올라가 분석이 느려진다.
        pages = _page_count(pdf_path) if request.document_type == "CERTIFICATE" else None
        if pages is not None and pages > CERTIFICATE_MAX_PAGES:
            logger.info(
                "CERTIFICATE로 요청된 문서가 %d페이지입니다 (약관 합본이거나 오분류). "
                "documentId=%s",
                pages, request.document_id,
            )
        return request.document_type

    pages = _page_count(pdf_path)
    if pages is None:
        logger.warning("페이지 수를 세지 못해 약관으로 처리합니다")
        return "TERMS"

    resolved = "CERTIFICATE" if pages <= CERTIFICATE_MAX_PAGES else "TERMS"
    logger.info("documentType이 없어 페이지 수(%d)로 %s로 판별했습니다", pages, resolved)
    return resolved


# 증권 경로. 약관 경로와 공유하는 것이 없어 함수를 따로 둔다.
#
# 약관에서 담보를 발굴하던 것을 증권에서 읽어오는 것으로 바꾸면 LLM 호출이 사라진다.
# 카테고리별 추출(extract_all)이 7회 돌던 자리가 통째로 없어지고, 대신 Studio Agent가
# 한 번 돈다. 금액도 이쪽에서만 얻을 수 있다 - 약관에는 "보험가입금액을 한도로"라고만
# 적혀 있어, 실측에서 약관 추출 30건 중 금액이 채워진 것은 3건뿐이었다.
def _process_certificate(request: AnalysisStartRequest, pdf_path: Path) -> None:
    certificate = analyze_certificate(pdf_path)

    # 성공이든 실패든 한 줄 남긴다. 실패했을 때 이 줄이 유일한 단서이고,
    # 성공했을 때는 나중에 Studio에서 스키마가 바뀌었는지 비교할 기준이 된다.
    # 값은 넣지 않는다(describe_structure 주석 참고).
    logger.info("증권 에이전트 출력 구조: %s", describe_structure(certificate))

    payloads = to_payloads(certificate)
    if not payloads:
        if not looks_like_certificate(certificate):
            # 담보가 없는 것이 아니라 애초에 증권이 아니다. 이걸 "담보를 못
            # 찾았다"로 끝내면 사용자는 증권을 다시 올려도 같은 실패를 본다.
            raise CertificateAnalysisError(
                "증권 고유 항목(증권번호·보험기간·보험사)이 하나도 없다. 증권이 아닌 "
                f"문서로 보인다 (documentId={request.document_id}): "
                + describe_structure(certificate),
                user_message="증권이 아닌 문서로 보입니다. 보험 증권 파일인지 확인해 주세요.",
            )
        raise CertificateAnalysisError(
            "증권에서 보장 담보를 찾지 못했습니다. 에이전트 출력 구조: "
            + describe_structure(certificate),
            user_message="증권에서 보장 내용을 찾지 못했습니다. 증권 원본 파일인지 확인해 주세요.",
        )

    start_date, end_date = insurance_period(certificate)
    if not start_date or not end_date:
        # policies.start_date/end_date가 NOT NULL이라, 비면 백엔드가 행을 만들 수 없다.
        logger.warning(
            "증권에서 보험기간을 읽지 못했습니다 (%s ~ %s). 에이전트 출력 키를 확인하십시오.",
            start_date, end_date,
        )

    callback = AnalysisCompleteCallback(
        analysisResultId=request.analysis_result_id,
        summary=_build_summary(payloads),
        coverageItems=payloads,
        # 백엔드가 이 둘로 약관을 찾아 연결한다.
        insurerName=certificate.get("insurer_name"),
        productName=certificate.get("product_name") or certificate.get("document_title"),
        # policies의 NOT NULL 두 개. 증권에만 있는 값이다.
        startDate=start_date,
        endDate=end_date,
        # 담보 목록이 표 전체인지. 폴백으로 만든 목록은 근거가 달라 켜지 않는다.
        coveragesComplete=coverages_complete(certificate),
        rawResultJson=json.dumps(certificate, ensure_ascii=False),
    )

    try:
        spring_client.notify_complete(callback)
    except SpringCallbackError:
        logger.error("완료 콜백 전달 실패 (증권 분석 자체는 성공): %s", request.analysis_result_id)

    logger.info(
        "증권 분석 완료: 담보 %d건, 금액 %d건 확보",
        len(payloads),
        sum(1 for p in payloads if p.limit_amount is not None),
    )


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
        document_id=request.document_id,
        policy_id=request.policy_id,
    )

    try:
        pdf_path = _download_pdf(request.download_url, request.document_id)

        # 약관·증권 공통. 잠긴 파일은 어느 경로로 가도 본문이 비어 있다.
        _unlock_or_reject(
            pdf_path,
            request.document_password.get_secret_value() if request.document_password else None,
        )

        # 증권이면 여기서 끝난다. 청킹도 임베딩도 하지 않는다.
        # 증권은 화면에 뜰 담보 목록이지 챗봇이 검색할 근거가 아니다.
        if _resolve_document_type(request, pdf_path) == "CERTIFICATE":
            try:
                _process_certificate(request, pdf_path)
            finally:
                # 내려받은 증권을 지운다.
                #
                # 약관은 재분석에 대비해 남겨두지만 증권은 개인정보다. 피보험자 이름·
                # 생년월일·증권번호가 들어 있어 디스크에 쌓이면 안 된다.
                #
                # 남겨두면 다른 사고도 난다. 실제로 테스트 중에 내려받은 증권이
                # raw_pdfs에 남아, 약관 색인 배치가 그것을 약관으로 오인해 청킹·임베딩까지
                # 했다. 공유 약관 인덱스에 개인정보가 섞여 들어간 것이다.
                _remove_quietly(pdf_path)
            return

        chunks = parse_and_chunk(pdf_path, scope=scope)
        embeddings = embed_chunks(chunks)
        repository.save(
            chunks,
            embeddings,
            analysis_result_id=request.analysis_result_id,
            scope=scope,
        )
    except AnalysisFailure as e:
        # 사용자에게는 e.user_message만 나간다. 원인은 위 traceback에 남는다.
        logger.exception("분석 실패: %s", request.analysis_result_id)
        _safe_notify_fail(request.analysis_result_id, e.user_message)
        return
    except Exception:
        # 우리가 예상하지 못한 예외다. 메시지에 무엇이 들어 있을지 알 수 없으니
        # 사용자에게 보내지 않는다. 추적은 traceback과 analysisResultId로 한다.
        logger.exception("분석 실패 (예상하지 못한 오류): %s", request.analysis_result_id)
        _safe_notify_fail(request.analysis_result_id, AnalysisFailure.user_message)
        return

    # pgvector 저장소는 INSERT하며 policy_chunks.id(UUID)를 만들고 청크에 실어준다.
    # coverage_item_sources가 그 UUID를 FK로 참조하므로 콜백에 그대로 전달한다.
    # 저장소 인스턴스가 아니라 청크에서 읽는 이유는, 저장소가 싱글턴이라
    # 동시 분석 시 인스턴스 상태를 쓰면 서로 덮어쓰기 때문이다.
    chunk_id_map = {
        c["chunk_id"]: c["policy_chunk_id"] for c in chunks if c.get("policy_chunk_id")
    }
    _safe_notify_complete(request.analysis_result_id, chunks, chunk_id_map)
