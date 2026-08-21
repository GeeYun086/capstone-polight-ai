"""증권 분석이 실패했을 때 원인을 찾을 수 있는지, 그리고 실패가 사용자에게
어떻게 보이는지에 대한 회귀 테스트.

실제로 이런 일이 있었다. 프론트 화면에 다음 문구가 그대로 떴다.

    증권에서 보장 담보를 찾지 못했습니다. 에이전트 출력 형식을 확인하십시오.

두 가지가 동시에 잘못됐다.

  사용자 쪽   "에이전트 출력 형식을 확인하십시오"는 사용자가 할 수 있는 일이 아니다
  개발자 쪽   원인을 찾으려 했더니 남은 것이 이 문구뿐이었다. 에이전트 출력은
              어디에도 기록되지 않아 Studio 콘솔을 열어야만 알 수 있었다

그래서 셋을 고정한다. 출력 구조는 로그에 남고, 값은 절대 남지 않고,
사용자에게는 사용자용 문구만 나간다.
"""

import logging

import fitz
import pytest

from app.schemas.analysis import AnalysisStartRequest
from app.schemas.db_limits import MAX_LENGTHS
from app.services import analysis_service
from app.services.certificate_adapter import describe_structure, to_payloads

BASE = {
    "analysisResultId": "a-1",
    "documentId": "d-1",
    "userId": "u-1",
    "tripId": "t-1",
    "downloadUrl": "https://x.test/f.pdf?X-Amz-Signature=deadbeef",
    "documentType": "CERTIFICATE",
}


def make_pdf(path, pages: int):
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def captured(monkeypatch, tmp_path):
    sent = {}
    pdf = make_pdf(tmp_path / "cert.pdf", pages=1)
    monkeypatch.setattr(analysis_service, "_download_pdf", lambda url, doc_id: pdf)
    monkeypatch.setattr(
        analysis_service.spring_client, "notify_complete",
        lambda payload: sent.setdefault("complete", payload),
    )
    monkeypatch.setattr(
        analysis_service.spring_client, "notify_fail",
        lambda payload: sent.setdefault("fail", payload),
    )
    return sent


# ── 구조 요약 ────────────────────────────────────────────────


# 원인을 가르는 데 필요한 것은 값이 아니라 모양이다. 키가 바뀐 것인지
# 표가 비어 있는 것인지만 알면 다음 행동이 정해진다.
def test_structure_shows_keys_and_row_counts():
    described = describe_structure({
        "insurer_name": "한화손해보험(주)",
        "coverage_by_age_table": [{"coverage_item_name": "상해", "coverage_amount_age_15_80": "5만원"}],
        "coverage_description_table": [],
    })

    assert "coverage_by_age_table[1]" in described
    assert "coverage_item_name" in described
    assert "coverage_description_table[0]" in described


# 증권에는 피보험자 이름·생년월일·증권번호가 들어 있고 로그는 지우기 어렵다.
# 값이 한 글자라도 새면 이 진단은 쓸 수 없는 것이 된다.
def test_structure_never_contains_values():
    described = describe_structure({
        "insured_person_name": "홍길동",
        "insured_birth_date": "1990-01-01",
        "policy_no": "1234-5678",
        "coverage_by_age_table": [{"coverage_item_name": "상해", "amount": "5,000만원"}],
    })

    for value in ("홍길동", "1990-01-01", "1234-5678", "상해", "5,000만원"):
        assert value not in described, f"{value}가 로그에 남는다"


# 키 이름이 바뀌었을 때 그것이 보여야 한다. 이 케이스가 실제로 터진 것이다.
def test_structure_reveals_renamed_keys():
    described = describe_structure({"coverages": [{"name": "상해"}]})

    assert "coverages[1]" in described
    assert "coverage_by_age_table" not in described


# ── 실패 사유 ────────────────────────────────────────────────


# 담보가 0건인 것과 애초에 증권이 아닌 것은 사용자가 할 일이 다르다.
# 전자는 "다른 증권으로", 후자는 "증권을 올려주세요"다. 같은 문구로 끝내면
# 사용자는 증권을 다시 올려도 같은 실패를 본다.
#
# 두께로 가르지 않는다. 처음에 페이지 수로 판정했다가 틀렸다 - 아래 테스트 참고.
def test_non_certificate_document_says_so(monkeypatch, captured):
    monkeypatch.setattr(
        analysis_service, "analyze_certificate",
        lambda path: {"content": "...", "elements": []},
    )

    analysis_service.process_analysis(
        AnalysisStartRequest.model_validate(BASE), repository=None
    )

    assert "증권이 아닌" in captured["fail"].error_message


# 157페이지짜리 증권이 실재한다. 현대해상은 증권 2장과 약관 153장을 한 파일로
# 발급한다. 두께로 판정하면 이 정상 증권을 "증권이 아니다"로 되돌린다.
def test_thick_bundled_certificate_is_not_called_a_wrong_document(
    monkeypatch, captured, tmp_path
):
    pdf = make_pdf(tmp_path / "bundled.pdf", pages=157)
    monkeypatch.setattr(analysis_service, "_download_pdf", lambda url, doc_id: pdf)
    monkeypatch.setattr(
        analysis_service, "analyze_certificate",
        lambda path: {
            "policy_number": "F-26PA-0120186",
            "insurer_name": "현대해상",
            "coverage_by_age_table": [],
            "coverage_description_table": [],
        },
    )

    analysis_service.process_analysis(
        AnalysisStartRequest.model_validate(BASE), repository=None
    )

    sent = captured["fail"].error_message
    assert "증권이 아닌" not in sent, "정상 증권을 잘못된 문서로 되돌린다"
    assert "보장 내용을 찾지 못했습니다" in sent


def test_empty_table_asks_for_the_original(monkeypatch, captured):
    monkeypatch.setattr(
        analysis_service, "analyze_certificate",
        lambda path: {"policy_number": "F-1", "coverage_by_age_table": []},
    )

    analysis_service.process_analysis(
        AnalysisStartRequest.model_validate(BASE), repository=None
    )

    sent = captured["fail"].error_message
    assert "보장 내용을 찾지 못했습니다" in sent
    # 내부 지시문이 사용자 화면에 뜨던 문구다
    assert "에이전트" not in sent


# presigned URL에는 서명이 붙어 있다. 실패 사유는 DB에 영구 저장된 뒤 화면에 뜬다.
def test_presigned_url_never_reaches_the_callback(monkeypatch, captured):
    def boom(url, doc_id):
        raise analysis_service.AnalysisFailure(
            f"PDF를 내려받지 못했습니다 ({url})",
            user_message="문서를 내려받지 못했습니다. 잠시 후 다시 시도해 주세요.",
        )

    monkeypatch.setattr(analysis_service, "_download_pdf", boom)

    analysis_service.process_analysis(
        AnalysisStartRequest.model_validate(BASE), repository=None
    )

    sent = captured["fail"].error_message
    assert "X-Amz-Signature" not in sent
    assert "x.test" not in sent


# ── 길이 컷 ──────────────────────────────────────────────────


# 백엔드에 길이 검증이 없어 한도를 넘기면 400이 아니라 500이 온다. 500은
# 재시도 대상이라 세 번 다 500을 받고 콜백을 포기하고, 분석은 성공했는데
# 상태가 PROCESSING에 남는다. 문서당 분석은 1회뿐이라 되돌릴 수도 없다.
#
# 증권 쪽이 특히 위험하다. 이 값들은 에이전트가 뽑은 문자열이라 길이를
# 우리가 통제하지 못한다. 실제로 이 경로에 길이 컷이 빠져 있었다.
def test_certificate_payload_respects_length_limits():
    payloads = to_payloads({
        "coverage_by_age_table": [
            {
                "coverage_category_level_1": "가" * 300,
                "coverage_category_level_2": "나" * 900,
                "coverage_item_name": "다" * 300,
                "coverage_amount_age_15_80": "5,000만원 " + "라" * 300,
            }
        ],
        "coverage_description_table": [],
    })

    item = payloads[0]
    assert len(item.title) == MAX_LENGTHS["title"]
    assert len(item.subtitle) == MAX_LENGTHS["subtitle"]
    assert len(item.limit_label) == MAX_LENGTHS["limit_label"]
    # category는 더 이상 에이전트 원값이 아니라 표준 어휘(짧은 코드)로 환산되거나
    # None이라 길이 컷 대상이 아니다. 어휘 밖 긴 문자열은 여기 실리지 않는다.
    assert item.category is None or len(item.category) <= MAX_LENGTHS["category"]


# 자르는 것은 넘칠 때만이어야 한다. 실제 증권 값은 그대로 나가야 화면 문구가 온전하다.
def test_realistic_certificate_values_are_untouched():
    payloads = to_payloads({
        "coverage_by_age_table": [
            {
                "coverage_category_level_1": "해외의료비 보장",
                "coverage_category_level_2": "",
                "coverage_item_name": "상해",
                "coverage_amount_age_15_80": "US 5만달러",
            }
        ],
        "coverage_description_table": [],
    })

    item = payloads[0]
    assert item.limit_label == "US 5만달러"
    assert item.title == "해외의료비 보장 상해"
    assert item.limit_currency == "USD"


# ── 비밀번호가 걸린 파일 ─────────────────────────────────────


# 보험사는 증권을 생년월일 6자리 같은 암호로 잠가 배포하는 경우가 많다.
#
# fitz.open()이 성공하고 page_count도 읽히기 때문에 라우팅을 그대로 통과한다.
# 그러면 잠긴 파일이 Upstage까지 올라가 본문이 비고, 담보 0건 -> "증권 원본
# 파일인지 확인해 주세요"가 나간다. 파일은 맞고 잠겨 있을 뿐이라 사용자는
# 같은 파일을 계속 올린다.
def make_locked_pdf(path, user_pw="020512"):
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "certificate")
    doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=user_pw)
    doc.close()
    return path


def test_password_protected_pdf_says_so(monkeypatch, captured, tmp_path):
    pdf = make_locked_pdf(tmp_path / "locked.pdf")
    monkeypatch.setattr(analysis_service, "_download_pdf", lambda url, doc_id: pdf)

    def never(path):
        raise AssertionError("잠긴 파일을 Upstage에 올렸다")

    monkeypatch.setattr(analysis_service, "analyze_certificate", never)

    analysis_service.process_analysis(
        AnalysisStartRequest.model_validate(BASE), repository=None
    )

    sent = captured["fail"].error_message
    assert "비밀번호" in sent
    assert "해제" in sent


# 잠기지 않은 파일은 그대로 흘러가야 한다. 검사가 정상 경로를 막으면 안 된다.
def test_unlocked_pdf_passes_through(monkeypatch, captured, tmp_path):
    pdf = make_pdf(tmp_path / "open.pdf", pages=1)
    monkeypatch.setattr(analysis_service, "_download_pdf", lambda url, doc_id: pdf)
    monkeypatch.setattr(
        analysis_service, "analyze_certificate",
        lambda path: {
            "policy_number": "F-1",
            "coverage_table": [
                {"coverage_name": "상해", "coverage_amount": "100,000,000원"}
            ],
        },
    )

    analysis_service.process_analysis(
        AnalysisStartRequest.model_validate(BASE), repository=None
    )

    assert "fail" not in captured
    assert captured["complete"].coverage_items[0].limit_amount == 100_000_000


# 권한(소유자) 암호만 걸린 PDF는 열기가 자유롭다. needs_pass가 0이고 본문도
# 읽힌다. 보험사 증권에서 "보호된 문서"로 보이는 것 상당수가 이 경우라,
# 여기서 막으면 정상 증권을 거절하게 된다.
def test_owner_password_only_is_not_blocked(monkeypatch, captured, tmp_path):
    path = tmp_path / "owner_only.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "certificate")
    doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner")
    doc.close()

    monkeypatch.setattr(analysis_service, "_download_pdf", lambda url, doc_id: path)
    monkeypatch.setattr(
        analysis_service, "analyze_certificate",
        lambda p: {"policy_number": "F-1",
                   "coverage_table": [{"coverage_name": "상해", "coverage_amount": "1억원"}]},
    )

    analysis_service.process_analysis(
        AnalysisStartRequest.model_validate(BASE), repository=None
    )

    assert "fail" not in captured


# 암호를 받으면 복호화해서 진행한다. 사용자가 뷰어에서 암호를 없애는 것은
# 유료 기능이라, 파일을 고치게 하는 대신 암호를 받는다.
def test_password_unlocks_the_document(monkeypatch, captured, tmp_path):
    pdf = make_locked_pdf(tmp_path / "locked.pdf")
    monkeypatch.setattr(analysis_service, "_download_pdf", lambda url, doc_id: pdf)

    seen = {}

    def analyze(path):
        # 복호화된 파일이 올라가야 한다. 잠긴 채로 올리면 본문이 빈다.
        with fitz.open(path) as doc:
            seen["needs_pass"] = bool(doc.needs_pass)
            seen["text"] = doc[0].get_text()
        return {"policy_number": "F-1",
                "coverage_table": [{"coverage_name": "상해", "coverage_amount": "1억원"}]}

    monkeypatch.setattr(analysis_service, "analyze_certificate", analyze)

    analysis_service.process_analysis(
        AnalysisStartRequest.model_validate({**BASE, "documentPassword": "020512"}),
        repository=None,
    )

    assert seen["needs_pass"] is False
    assert "certificate" in seen["text"]
    assert "fail" not in captured
    assert captured["complete"].coverage_items[0].limit_amount == 100_000_000


def test_wrong_password_says_so(monkeypatch, captured, tmp_path):
    pdf = make_locked_pdf(tmp_path / "locked.pdf")
    monkeypatch.setattr(analysis_service, "_download_pdf", lambda url, doc_id: pdf)

    def never(path):
        raise AssertionError("잠긴 파일을 Upstage에 올렸다")

    monkeypatch.setattr(analysis_service, "analyze_certificate", never)

    analysis_service.process_analysis(
        AnalysisStartRequest.model_validate({**BASE, "documentPassword": "999999"}),
        repository=None,
    )

    assert "맞지 않습니다" in captured["fail"].error_message


# 비밀번호가 로그·실패 사유·콜백에 섞여 나가면 안 된다.
def test_password_never_leaves_the_server(monkeypatch, captured, tmp_path, caplog):
    pdf = make_locked_pdf(tmp_path / "locked.pdf")
    monkeypatch.setattr(analysis_service, "_download_pdf", lambda url, doc_id: pdf)
    monkeypatch.setattr(
        analysis_service, "analyze_certificate",
        lambda p: {"policy_number": "F-1", "coverage_table": []},
    )

    request = AnalysisStartRequest.model_validate({**BASE, "documentPassword": "020512"})
    assert "020512" not in repr(request), "SecretStr가 아니면 repr로 새어 나간다"

    with caplog.at_level(logging.DEBUG):
        analysis_service.process_analysis(request, repository=None)

    assert "020512" not in caplog.text
    assert "020512" not in captured["fail"].error_message
