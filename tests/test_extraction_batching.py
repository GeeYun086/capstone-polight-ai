"""큰 카테고리를 나눠 보내고 합치는 동작 검증.

medical_expense가 48,276토큰이라 OpenAI 분당 한도(30,000)에 걸려 통째로
실패했다. 가장 중요한 의료비 담보가 빠지는 문제라, 다시 깨지지 않게 막아둔다.
LLM은 호출하지 않는다.
"""

from app.schemas.coverage import CoverageItem
from app.services.coverage_extractor import MAX_CONTEXT_CHARS, _merge_items, _split_by_budget


def make_chunk(i: int, size: int) -> dict:
    return {"chunk_id": f"c{i}", "text": "가" * size, "section_title": "제1조",
            "coverage_type": "included", "clause_path": ""}


def make_item(**overrides) -> CoverageItem:
    payload = {"title": "의료비", "category": "medical_expense",
               "isCovered": True, "coverageStatus": "COVERED"}
    payload.update(overrides)
    return CoverageItem.model_validate(payload)


def test_small_input_stays_in_one_batch():
    assert len(_split_by_budget([make_chunk(i, 100) for i in range(5)])) == 1


def test_large_input_is_split():
    batches = _split_by_budget([make_chunk(i, 5_000) for i in range(20)])

    assert len(batches) > 1
    for batch in batches:
        assert sum(len(c["text"]) for c in batch) <= MAX_CONTEXT_CHARS


# 조각 하나가 예산을 넘어도 버리면 안 된다. 그 조각에만 있는 조항이 사라진다.
def test_oversized_chunk_is_kept():
    batches = _split_by_budget([make_chunk(0, MAX_CONTEXT_CHARS * 2)])

    assert sum(len(b) for b in batches) == 1


# 나눠 보냈다고 면책 조건이 줄면 나누는 의미가 없다.
def test_merge_unions_list_fields():
    a = make_item(exclusions=[{"title": "고의"}, {"title": "전쟁"}],
                  requiredDocuments=[{"documentName": "청구서"}])
    b = make_item(exclusions=[{"title": "전쟁"}, {"title": "지진"}],
                  requiredDocuments=[{"documentName": "진단서"}])

    merged = _merge_items([a, b])

    assert [e.title for e in merged.exclusions] == ["고의", "전쟁", "지진"], "중복 제거 실패"
    assert len(merged.required_documents) == 2


# 앞 배치가 못 찾은 값을 뒤 배치가 찾는 경우를 살려야 한다.
def test_merge_takes_first_non_null_scalar():
    merged = _merge_items([make_item(), make_item(limitAmount=3_500_000)])

    assert merged.limit_amount == 3_500_000


def test_merge_keeps_earlier_scalar_when_both_present():
    merged = _merge_items([make_item(limitAmount=100), make_item(limitAmount=200)])

    assert merged.limit_amount == 100
