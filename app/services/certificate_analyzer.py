"""증권 PDF를 Upstage Studio Agent로 분석해 구조화 JSON을 얻는다.

약관 파싱(/v1/document-digitization)과는 다른 API다. Studio에서 만든 에이전트가
파싱 -> 분류 -> 추출을 다단계로 수행하고, 응답의 output[].model이 step_1_parse 같은
단계 이름으로 나온다. 스키마는 에이전트에 저장돼 있어 우리가 들고 있지 않는다.

동기 요청이 아니라 잡이다.

    POST /v2/files              업로드 -> file_id
    POST /v2/responses          잡 생성 -> job_id
    GET  /v2/responses/{id}     completed 될 때까지 폴링
    DELETE /v2/files/{id}       삭제

마지막 삭제가 중요하다. 문서에 "Files are retained server-side until explicitly
deleted"라고 되어 있는데, 증권에는 피보험자 이름·생년월일·증권번호가 들어 있다.
지우지 않으면 개인정보가 남는다. 그래서 성공하든 실패하든 지운다.
"""

import json
import logging
import time
from pathlib import Path

from openai import OpenAI

from app.core.config import get_settings
from app.services.analysis_errors import AnalysisFailure

logger = logging.getLogger(__name__)

# 잡이 끝났다고 볼 수 있는 상태.
TERMINAL = {"completed", "failed", "cancelled", "incomplete"}


class CertificateAnalysisError(AnalysisFailure):
    """증권 분석 실패. process_analysis가 잡아 FAILED 콜백으로 바꾼다.

    인자로 주는 문자열은 로그용이다. 사용자에게는 user_message가 나간다.
    """

    user_message = "증권을 분석하지 못했습니다. 잠시 후 다시 시도해 주세요."


# 설정이 빠진 것은 사용자가 파일을 바꿔 봐야 해결되지 않는다. 다시 시도하라고
# 안내하면 같은 실패를 반복하게 되므로 문구를 따로 둔다.
CONFIG_USER_MESSAGE = "분석 서버 설정 문제로 실패했습니다. 관리자에게 문의해 주세요."


def agent_api_key() -> str:
    """에이전트 호출용 키. 따로 지정하지 않으면 공용 키를 쓴다.

    에이전트가 다른 계정에 있으면 공용 키로는 404가 난다. 인증은 통과하는데
    그 계정에 그 에이전트가 없어서다. 그때 이 값만 채우면 약관 파싱·임베딩은
    기존 계정 그대로 두고 증권만 다른 계정으로 부를 수 있다.
    """
    settings = get_settings()
    return settings.upstage_agent_api_key or settings.upstage_api_key


def _client() -> OpenAI:
    settings = get_settings()
    key = agent_api_key()
    if not key:
        raise CertificateAnalysisError(
            ".env에 UPSTAGE_API_KEY(또는 UPSTAGE_AGENT_API_KEY)가 설정되지 않았습니다.",
            user_message=CONFIG_USER_MESSAGE,
        )
    if not settings.upstage_agent_id:
        raise CertificateAnalysisError(
            ".env에 UPSTAGE_AGENT_ID가 설정되지 않았습니다. "
            "Upstage Studio의 에이전트 ID(agt_로 시작)를 넣으십시오.",
            user_message=CONFIG_USER_MESSAGE,
        )
    return OpenAI(api_key=key, base_url=settings.upstage_agent_base_url)


def _wait(client: OpenAI, job, interval: float, timeout: float):
    """잡이 끝날 때까지 폴링한다.

    시간 제한을 두는 이유: 에이전트 설정이 잘못되면 잡이 오래 매달릴 수 있는데,
    process_analysis는 BackgroundTasks에서 돌아 아무도 취소해주지 않는다.
    무한정 기다리면 워커가 물린 채로 남는다.
    """
    deadline = time.monotonic() + timeout

    while job.status not in TERMINAL:
        if time.monotonic() > deadline:
            raise CertificateAnalysisError(
                f"증권 분석이 {timeout:.0f}초 안에 끝나지 않았습니다 (job {job.id}, 상태 {job.status})"
            )
        time.sleep(interval)
        job = client.responses.retrieve(job.id, include=["last"])
        logger.info("증권 분석 진행 중: %s (%s)", job.id, job.status)

    return job


def _read_output(job) -> dict:
    """최종 단계의 output_text를 파싱한다.

    문자열이 아니라 dict를 돌려주는 이유: 호출하는 쪽이 매번 json.loads를 하면
    실패 지점이 흩어지고, 에이전트가 JSON이 아닌 것을 뱉었을 때 원인이 안 보인다.
    """
    text = getattr(job, "output_text", None)
    if not text:
        raise CertificateAnalysisError(f"증권 분석 결과가 비어 있습니다 (job {job.id})")

    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        raise CertificateAnalysisError(
            f"증권 분석 결과가 JSON이 아닙니다 (job {job.id}): {text[:200]}"
        ) from e

    if not isinstance(result, dict):
        raise CertificateAnalysisError(
            f"증권 분석 결과가 객체가 아닙니다 (job {job.id}): {type(result).__name__}"
        )
    return result


def analyze_certificate(pdf_path: Path, client: OpenAI | None = None) -> dict:
    """증권 PDF -> 구조화 JSON. certificate_adapter가 받는 그 형태다."""
    settings = get_settings()
    client = client or _client()

    with pdf_path.open("rb") as f:
        uploaded = client.files.create(file=f, purpose="user_data")
    logger.info("증권 업로드 완료: %s (%s)", uploaded.id, pdf_path.name)

    try:
        params = {
            "model": settings.upstage_agent_id,
            "include": ["last"],
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_file", "file_id": uploaded.id}],
                }
            ],
        }
        # 비우면 최신 설정을 쓴다. Studio에서 고친 것이 바로 반영되게 하려는 것이다.
        if settings.upstage_agent_config_id:
            params["config_id"] = settings.upstage_agent_config_id

        job = client.responses.create(**params)
        logger.info("증권 분석 잡 생성: %s", job.id)

        job = _wait(
            client,
            job,
            interval=settings.certificate_poll_interval,
            timeout=settings.certificate_timeout,
        )

        if job.status != "completed":
            # 잡 단위 실패는 대개 에이전트 설정 문제다. Studio를 봐야 한다.
            raise CertificateAnalysisError(
                f"증권 분석 실패 (job {job.id}, 상태 {job.status}). Studio에서 에이전트 설정을 확인하십시오."
            )

        return _read_output(job)

    finally:
        # 증권에는 이름·생년월일·증권번호가 들어 있다. 업로드한 파일은 지울 때까지
        # 남으므로 반드시 지운다. 삭제 실패로 분석 결과를 버릴 수는 없으니 예외는 삼킨다.
        try:
            client.files.delete(uploaded.id)
            logger.info("업로드한 증권 삭제: %s", uploaded.id)
        except Exception:
            logger.warning(
                "업로드한 증권을 지우지 못했습니다: %s. 개인정보가 남아 있으니 "
                "콘솔에서 확인하십시오.",
                uploaded.id,
            )
