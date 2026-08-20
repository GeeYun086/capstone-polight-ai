"""증권 분석기(Upstage Studio Agent) 회귀 테스트.

비동기 잡이라 실패 지점이 여럿이다. 업로드, 잡 생성, 폴링, 결과 파싱, 파일 삭제가
각각 다른 이유로 깨지는데 전부 "분석이 안 된다"로만 보인다.

특히 파일 삭제는 조용히 빠뜨리기 쉽다. 증권에는 이름·생년월일·증권번호가 들어 있고
Upstage는 지울 때까지 파일을 보관하므로, 삭제가 빠지면 개인정보가 남는다.
기능은 멀쩡히 동작해서 눈으로는 알 수 없다.
"""

import json

import pytest

from app.core.config import Settings
from app.services import certificate_analyzer
from app.services.certificate_analyzer import CertificateAnalysisError, analyze_certificate

RESULT = {"insurer_name": "한화손해보험(주)", "coverage_by_age_table": []}


class FakeJob:
    def __init__(self, job_id="job_1", statuses=("completed",), output_text=None):
        self.id = job_id
        self._statuses = list(statuses)
        self.status = self._statuses.pop(0)
        self.output_text = output_text

    def advance(self):
        if self._statuses:
            self.status = self._statuses.pop(0)
        return self


class FakeFiles:
    def __init__(self):
        self.created = []
        self.deleted = []

    def create(self, file, purpose):
        self.created.append(purpose)
        return type("F", (), {"id": "file_1"})()

    def delete(self, file_id):
        self.deleted.append(file_id)


class FakeResponses:
    def __init__(self, job):
        self._job = job
        self.create_params = None

    def create(self, **params):
        self.create_params = params
        return self._job

    def retrieve(self, job_id, include=None):
        return self._job.advance()


class FakeClient:
    def __init__(self, job):
        self.files = FakeFiles()
        self.responses = FakeResponses(job)


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(certificate_analyzer.time, "sleep", lambda s: None)


@pytest.fixture
def settings(monkeypatch):
    def build(**overrides):
        values = {
            "upstage_api_key": "up_test",
            "upstage_agent_id": "agt_test",
            "certificate_poll_interval": 0.0,
            "certificate_timeout": 10.0,
            **overrides,
        }
        monkeypatch.setattr(
            certificate_analyzer, "get_settings", lambda: Settings(**values)
        )

    return build


def test_returns_parsed_json(tmp_path, settings, fast_poll):
    settings()
    pdf = tmp_path / "cert.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    client = FakeClient(FakeJob(output_text=json.dumps(RESULT)))

    assert analyze_certificate(pdf, client=client) == RESULT


def test_polls_until_completed(tmp_path, settings, fast_poll):
    settings()
    pdf = tmp_path / "cert.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    job = FakeJob(statuses=("queued", "in_progress", "completed"), output_text=json.dumps(RESULT))

    assert analyze_certificate(pdf, client=FakeClient(job)) == RESULT
    assert job.status == "completed"


# 증권에는 이름·생년월일·증권번호가 들어 있다. Upstage는 지울 때까지 파일을 보관한다.
def test_deletes_uploaded_file(tmp_path, settings, fast_poll):
    settings()
    pdf = tmp_path / "cert.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    client = FakeClient(FakeJob(output_text=json.dumps(RESULT)))

    analyze_certificate(pdf, client=client)

    assert client.files.deleted == ["file_1"], "업로드한 증권이 서버에 남는다"


# 실패했을 때가 더 중요하다. 성공 경로만 지우면 실패한 증권이 계속 쌓인다.
def test_deletes_uploaded_file_even_on_failure(tmp_path, settings, fast_poll):
    settings()
    pdf = tmp_path / "cert.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    client = FakeClient(FakeJob(statuses=("failed",)))

    with pytest.raises(CertificateAnalysisError):
        analyze_certificate(pdf, client=client)

    assert client.files.deleted == ["file_1"]


def test_failed_job_raises_with_studio_hint(tmp_path, settings, fast_poll):
    settings()
    pdf = tmp_path / "cert.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    with pytest.raises(CertificateAnalysisError, match="Studio"):
        analyze_certificate(pdf, client=FakeClient(FakeJob(statuses=("failed",))))


# 에이전트 설정이 잘못되면 잡이 오래 매달린다. BackgroundTasks에서 도는 코드라
# 아무도 취소해주지 않아, 시간 제한이 없으면 워커가 물린 채로 남는다.
def test_times_out_instead_of_hanging(tmp_path, settings, monkeypatch):
    settings(certificate_timeout=0.0)
    monkeypatch.setattr(certificate_analyzer.time, "sleep", lambda s: None)
    pdf = tmp_path / "cert.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    job = FakeJob(statuses=("in_progress",) * 50)

    with pytest.raises(CertificateAnalysisError, match="끝나지 않았습니다"):
        analyze_certificate(pdf, client=FakeClient(job))


def test_non_json_output_raises_with_the_text(tmp_path, settings, fast_poll):
    settings()
    pdf = tmp_path / "cert.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    client = FakeClient(FakeJob(output_text="죄송합니다. 문서를 읽을 수 없습니다."))

    with pytest.raises(CertificateAnalysisError, match="JSON이 아닙니다"):
        analyze_certificate(pdf, client=client)


def test_empty_output_raises(tmp_path, settings, fast_poll):
    settings()
    pdf = tmp_path / "cert.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    with pytest.raises(CertificateAnalysisError, match="비어 있습니다"):
        analyze_certificate(pdf, client=FakeClient(FakeJob(output_text=None)))


# config_id를 비우면 최신 설정을 쓴다. Studio에서 고친 것이 바로 반영되게 하려는 것이다.
def test_config_id_omitted_when_not_set(tmp_path, settings, fast_poll):
    settings()
    pdf = tmp_path / "cert.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    client = FakeClient(FakeJob(output_text=json.dumps(RESULT)))

    analyze_certificate(pdf, client=client)

    assert "config_id" not in client.responses.create_params
    assert client.responses.create_params["model"] == "agt_test"


def test_config_id_pinned_when_set(tmp_path, settings, fast_poll):
    settings(upstage_agent_config_id="2")
    pdf = tmp_path / "cert.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    client = FakeClient(FakeJob(output_text=json.dumps(RESULT)))

    analyze_certificate(pdf, client=client)

    assert client.responses.create_params["config_id"] == "2"


def test_missing_agent_id_fails_early(tmp_path, settings):
    settings(upstage_agent_id="")
    pdf = tmp_path / "cert.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    with pytest.raises(CertificateAnalysisError, match="UPSTAGE_AGENT_ID"):
        analyze_certificate(pdf)
