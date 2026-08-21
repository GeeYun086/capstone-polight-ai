"""추출 결과 -> Spring 콜백 payload 변환 검증.

DB 없이 확정 제약과의 어긋남을 잡는다. 아래 항목들은 틀리면 Spring 쪽 INSERT가
실패하거나, 더 나쁘게는 화면에 잘못된 값이 뜬다.
"""

from app.schemas.coverage import CoverageItem
from app.schemas.db_limits import MAX_LENGTHS
from app.services.callback_mapper import to_payload


def make_item(**overrides) -> CoverageItem:
    payload = {
        "title": "해외여행중 휴대품손해",
        "category": "baggage",
        "isCovered": True,
        "coverageStatus": "COVERED",
    }
    payload.update(overrides)
    return CoverageItem.model_validate(payload)


# ── enum 번역 ────────────────────────────────────────────────


def test_coverage_status_is_translated():
    # PARTIAL은 허용값이 아니다. 그대로 넣으면 CHECK 제약 위반이다.
    assert to_payload(make_item(coverageStatus="PARTIAL"), {}).coverage_status == "PARTIALLY_COVERED"
    # UNKNOWN도 허용값에 없어 NOT_COVERED로 접는다.
    assert to_payload(make_item(coverageStatus="UNKNOWN"), {}).coverage_status == "NOT_COVERED"


def test_source_role_and_severity_are_translated():
    item = make_item(
        sources=[
            {"chunkId": "c1", "sourceRole": "COVERAGE"},
            {"chunkId": "c2", "sourceRole": "DOCUMENT"},
        ],
        exclusions=[{"title": "고의", "severity": "HIGH"}],
    )

    payload = to_payload(item, {})

    assert [s.source_role for s in payload.sources] == ["PRIMARY", "REQUIRED_DOCUMENT"]
    assert payload.exclusions[0].severity == "CRITICAL"


# ── chunk_id 치환 ────────────────────────────────────────────


# coverage_item_sources.policy_chunk_id는 FK다. 내부 문자열 id를 그대로 보내면
# Spring 쪽 INSERT가 FK 위반으로 실패한다.
def test_chunk_id_is_replaced_with_saved_uuid():
    item = make_item(sources=[{"chunkId": "db_travel_0001", "sourceRole": "COVERAGE"}])

    payload = to_payload(item, {"db_travel_0001": "aaaa-bbbb"})

    assert payload.sources[0].chunk_id == "aaaa-bbbb"


# 파일 저장소로 개발할 때는 매핑이 없다. 그때 예외를 내면 로컬 개발이 막힌다.
def test_unmapped_chunk_id_passes_through():
    item = make_item(sources=[{"chunkId": "local_0001", "sourceRole": "COVERAGE"}])

    assert to_payload(item, {}).sources[0].chunk_id == "local_0001"


# ── 중복 제거 ────────────────────────────────────────────────


# UNIQUE(coverage_item_id, policy_chunk_id, source_role)이 걸려 있다.
# LLM은 같은 조각을 여러 번 인용하는 경우가 흔하다.
def test_duplicate_sources_are_removed():
    item = make_item(sources=[
        {"chunkId": "c1", "sourceRole": "COVERAGE", "quoteText": "첫 인용"},
        {"chunkId": "c1", "sourceRole": "COVERAGE", "quoteText": "같은 조각 다른 인용"},
        {"chunkId": "c1", "sourceRole": "LIMIT"},
    ])

    sources = to_payload(item, {}).sources

    # 같은 (chunk, role)만 제거한다. role이 다르면 별개 행이므로 남아야 한다.
    assert len(sources) == 2
    assert sources[0].quote_text == "첫 인용", "첫 등장을 남겨야 순서가 안정적이다"


# 번역 때문에 두 역할이 같아지는 경우는 없다. 내부 4개 값(COVERAGE/EXCLUSION/LIMIT/
# DOCUMENT)이 DB 값(PRIMARY/EXCLUSION/LIMIT/REQUIRED_DOCUMENT)으로 1:1 대응하므로
# 서로 다른 입력이 같은 출력이 되지 않는다. 그래서 번역 후 충돌은 검증하지 않는다.
#
# 치환 후에 같아지는 경우는 실제로 생길 수 있다. 조각을 분할했다가 합치면
# 서로 다른 내부 id가 같은 UUID를 가리킬 수 있다.
def test_duplicates_after_uuid_mapping_are_removed():
    item = make_item(sources=[
        {"chunkId": "a", "sourceRole": "COVERAGE"},
        {"chunkId": "b", "sourceRole": "COVERAGE"},
    ])

    payload = to_payload(item, {"a": "same-uuid", "b": "same-uuid"})

    assert len(payload.sources) == 1


# ── 길이 컷 ──────────────────────────────────────────────────


def test_long_title_is_truncated():
    payload = to_payload(make_item(title="가" * 500), {})

    assert len(payload.title) == MAX_LENGTHS["title"]


# 백엔드가 특히 짚어준 항목. 약관 원문을 그대로 담으면 200자를 넘기기 쉽다.
def test_sub_limit_value_is_truncated():
    item = make_item(subLimits=[{"label": "한도", "value": "나" * 400}])

    assert len(to_payload(item, {}).sub_limits[0].value) == MAX_LENGTHS["sub_limit_value"]


# conditions는 TEXT 컬럼이라 자르면 안 된다. 자르면 보장 조건이 잘려 오해를 만든다.
def test_text_columns_are_not_truncated():
    long_text = "다" * 3000
    item = make_item(
        conditions=long_text,
        exclusions=[{"title": "면책", "severity": "MEDIUM", "sourceText": long_text}],
    )

    payload = to_payload(item, {})

    assert payload.conditions == long_text
    assert payload.exclusions[0].source_text == long_text


# ── 자식 배열 전달 ───────────────────────────────────────────


# 이전 콜백에는 통로가 없어 추출은 해놓고 버리고 있었다(면책 38건, 청구서류 23건).
def test_child_arrays_are_carried():
    item = make_item(
        detailItems=[{"title": "입원", "isCovered": True}],
        subLimits=[{"label": "1개당", "value": "20만원", "limitAmount": 200000}],
        requiredDocuments=[{"documentName": "청구서", "isMandatory": True}],
        exclusions=[{"title": "고의", "severity": "HIGH"}],
    )

    payload = to_payload(item, {})

    assert len(payload.detail_items) == 1
    assert payload.sub_limits[0].limit_amount == 200000
    assert payload.required_documents[0].document_name == "청구서"
    assert len(payload.exclusions) == 1
