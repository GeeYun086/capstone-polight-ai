"""파싱 캐시 키 회귀 테스트.

파싱은 파이프라인에서 가장 비싼 단계고 Upstage는 페이지 단위로 과금한다.
캐시가 빗나가면 조용히 돈만 나가고 기능은 멀쩡해 보이므로 눈으로는 알 수 없다.

키를 파일명에서 내용 해시로 바꾼 이유: analysis_service가 내려받은 PDF를
"{document_id}.pdf"로 저장해서, 같은 약관이라도 사용자마다 파일명이 달랐다.
100명이 같은 상품에 가입하면 126페이지를 100번 파싱했다.
"""

import json

import pytest

from app.services import upstage_parser


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(upstage_parser, "CACHE_DIR", tmp_path / "parsed")
    return tmp_path / "parsed"


@pytest.fixture
def no_api(monkeypatch):
    """파싱이 실제로 불리면 테스트가 실패하도록 막는다."""

    def explode(pdf_path, api_key):
        raise AssertionError("캐시가 있는데 Upstage를 호출했다")

    monkeypatch.setattr(upstage_parser, "_parse_in_batches", explode)


def write_pdf(path, content: bytes) -> None:
    path.write_bytes(content)


ELEMENTS = [{"page": 1, "category": "paragraph", "text": "약관 본문", "html": ""}]


# 핵심: 같은 내용이면 파일명이 달라도 캐시가 맞아야 한다.
# 사용자마다 document_id가 다르므로 이게 안 되면 공유가 통째로 무너진다.
def test_same_content_different_filename_hits_cache(tmp_path, cache_dir, monkeypatch):
    first = tmp_path / "aaaa-1111.pdf"
    second = tmp_path / "bbbb-2222.pdf"
    write_pdf(first, b"%PDF-1.4 same terms")
    write_pdf(second, b"%PDF-1.4 same terms")

    calls = []

    def fake_parse(pdf_path, api_key):
        calls.append(pdf_path.name)
        return ELEMENTS

    monkeypatch.setattr(upstage_parser, "_parse_in_batches", fake_parse)
    monkeypatch.setattr(upstage_parser, "get_settings", lambda: type("S", (), {"upstage_api_key": "k"})())

    upstage_parser.parse_pdf(first)
    upstage_parser.parse_pdf(second)

    assert calls == ["aaaa-1111.pdf"], "같은 약관인데 두 번 파싱했다"


# 내용이 다르면(개정판) 새로 파싱해야 한다.
def test_different_content_does_not_share_cache(tmp_path, cache_dir, monkeypatch):
    old = tmp_path / "terms.pdf"
    new = tmp_path / "terms_2025.pdf"
    write_pdf(old, b"%PDF-1.4 2022 revision")
    write_pdf(new, b"%PDF-1.4 2025 revision")

    calls = []
    monkeypatch.setattr(
        upstage_parser, "_parse_in_batches", lambda p, k: calls.append(p.name) or ELEMENTS
    )
    monkeypatch.setattr(upstage_parser, "get_settings", lambda: type("S", (), {"upstage_api_key": "k"})())

    upstage_parser.parse_pdf(old)
    upstage_parser.parse_pdf(new)

    assert len(calls) == 2, "개정판인데 예전 캐시를 재사용했다"


# 파일명 기반으로 만들어둔 예전 캐시는 이미 돈을 낸 결과다.
# 키를 바꾸면서 이걸 버리면 7개 약관을 전부 다시 파싱하게 된다.
def test_legacy_filename_cache_is_migrated(tmp_path, cache_dir, no_api):
    pdf = tmp_path / "db_travel.pdf"
    write_pdf(pdf, b"%PDF-1.4 legacy")

    cache_dir.mkdir(parents=True)
    legacy = cache_dir / "db_travel_upstage.json"
    legacy.write_text(json.dumps(ELEMENTS), encoding="utf-8")

    elements = upstage_parser.parse_pdf(pdf)

    assert elements == ELEMENTS
    assert not legacy.exists(), "예전 캐시가 그대로 남아 있다"
    assert upstage_parser._cache_path(pdf).exists(), "해시 이름으로 옮겨지지 않았다"


def test_use_cache_false_reparses(tmp_path, cache_dir, monkeypatch):
    pdf = tmp_path / "terms.pdf"
    write_pdf(pdf, b"%PDF-1.4 content")

    calls = []
    monkeypatch.setattr(
        upstage_parser, "_parse_in_batches", lambda p, k: calls.append(p.name) or ELEMENTS
    )
    monkeypatch.setattr(upstage_parser, "get_settings", lambda: type("S", (), {"upstage_api_key": "k"})())

    upstage_parser.parse_pdf(pdf)
    upstage_parser.parse_pdf(pdf, use_cache=False)

    assert len(calls) == 2
