"""보장항목 추출 모델 비교 (Stage 4-B).

    python scripts/compare_extraction_models.py
    python scripts/compare_extraction_models.py --providers openai-mini openai-41

답변 모델과 채점 기준이 다르다. 요구사항 자체가 다르기 때문이다.

    답변  실시간이라 지연이 중요하고, 틀려도 그 대화 한 번으로 끝난다
    추출  비동기 배치라 지연이 덜 중요하고, 틀리면 DB에 저장돼 계속 노출된다

그래서 속도보다 '지어내지 않는가'와 '빠짐없이 뽑는가'를 본다.

채점 항목:
  JSON 성공률   구조 출력을 안정적으로 내는가 (벤더마다 JSON 지원 방식이 다르다)
  환각 경고     인용문·chunkId·금액이 원문에 실제로 있는가 (validate_against_source)
  chunkId 교정  인용문은 맞는데 출처를 틀리게 지목한 건수 (repair_source_ids)
  추출 충실도   면책조건·청구서류·세부항목·한도를 몇 개나 뽑았는가
  카테고리 수   8개 카테고리 중 몇 개를 만들어냈는가
"""

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services.answer_providers import PROVIDERS, generate_json  # noqa: E402
from app.services.coverage_extractor import (  # noqa: E402
    SYSTEM_PROMPT,
    OUTPUT_SCHEMA,
    build_context,
    load_categories,
    repair_source_ids,
    validate_against_source,
)
from app.schemas.coverage import CoverageItem  # noqa: E402

CHUNKS_PATH = PROJECT_ROOT / "data" / "chunks" / "db_travel_chunks.json"
OUTPUT_DIR = PROJECT_ROOT / "data" / "eval"

# 추출은 비용이 답변보다 훨씬 크다. 카테고리마다 조각 수십 개를 통째로 넣기 때문이다.
# 그래서 기본 후보를 좁게 잡고, 필요하면 --providers로 늘린다.
DEFAULT_PROVIDERS = ["openai-mini", "openai-41mini", "openai-41", "claude-opus", "gemini-flash"]


def extract_one(category: str, display_name: str, chunks: list[dict], provider: str) -> dict:
    """카테고리 하나를 추출하고 채점에 필요한 값을 모아 돌려준다."""
    user_message = (
        f"[담보 카테고리]\n{category} ({display_name})\n\n"
        f"[약관 조각]\n{build_context(chunks)}\n\n"
        f"[출력 형식]\n{OUTPUT_SCHEMA}"
    )

    try:
        raw, seconds = generate_json(SYSTEM_PROMPT, user_message, provider_name=provider)
    except Exception as e:
        return {"category": category, "error": f"호출 실패: {str(e)[:100]}", "seconds": 0.0}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"category": category, "error": "JSON 파싱 실패", "seconds": seconds,
                "raw_head": raw[:200]}

    payload["category"] = category
    try:
        item = CoverageItem.model_validate(payload)
    except Exception as e:
        return {"category": category, "error": f"스키마 위반: {str(e)[:100]}", "seconds": seconds}

    # 교정은 채점 전에 돌린다. 실제 파이프라인이 그렇게 동작하므로,
    # 교정 후에도 남는 경고가 진짜 문제다.
    repaired = repair_source_ids(item, chunks)
    warnings = validate_against_source(item, chunks)

    return {
        "category": category, "seconds": seconds, "error": None,
        "repaired": repaired, "warnings": warnings,
        "title": item.title,
        "limit_amount": item.limit_amount,
        "exclusions": len(item.exclusions),
        "documents": len(item.required_documents),
        "detail_items": len(item.detail_items),
        "sub_limits": len(item.sub_limits),
        "sources": len(item.sources),
        "item": item.model_dump(by_alias=True),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="추출 모델 비교")
    parser.add_argument("--providers", nargs="+", default=None)
    parser.add_argument("--chunks", default=str(CHUNKS_PATH))
    args = parser.parse_args()

    settings = get_settings()
    chunks = json.loads(Path(args.chunks).read_text(encoding="utf-8"))
    categories = load_categories()

    # 조각이 있는 카테고리만 대상으로 한다. 없는 카테고리는 어떤 모델도 못 뽑으므로
    # 비교에 넣으면 모두 같은 점수가 되어 변별만 흐려진다.
    targets = []
    for category, meta in sorted(categories.items(), key=lambda kv: kv[1]["ui_priority"]):
        matched = [c for c in chunks if c.get("matched_category") == category]
        if matched:
            targets.append((category, meta["display_name"], matched))

    print(f"약관: {Path(args.chunks).stem} (청크 {len(chunks)}개)")
    print(f"대상 카테고리 {len(targets)}개: {', '.join(c for c, _, _ in targets)}\n")

    names = args.providers or DEFAULT_PROVIDERS
    runnable = []
    for name in names:
        if not getattr(settings, PROVIDERS[name].api_key_field, ""):
            print(f"건너뜀: {name} (키 없음)")
            continue
        try:
            generate_json("JSON만 출력하세요.", '{"ok": true} 형식으로 답하세요.',
                          provider_name=name)
            runnable.append(name)
        except Exception as e:
            reason = ("무료 한도 초과" if "429" in str(e) else str(e)[:60])
            print(f"건너뜀: {name} — {reason}")

    if not runnable:
        print("\n실행 가능한 모델이 없습니다.")
        return
    print(f"비교 대상: {', '.join(runnable)}\n")

    print("[1] 추출")
    results = {}
    for name in runnable:
        started = time.time()
        rows = [extract_one(c, d, m, name) for c, d, m in targets]
        results[name] = rows
        ok = sum(1 for r in rows if not r["error"])
        print(f"  {name:14} {ok}/{len(rows)} 성공, {time.time() - started:.0f}초")

    print("\n[2] 채점")
    print(f"  {'모델':14} {'JSON':>6} {'환각':>6} {'교정':>6} {'면책':>6} "
          f"{'서류':>6} {'한도':>6} {'평균시간':>9}")
    print("  " + "-" * 70)

    summary = []
    for name in runnable:
        rows = results[name]
        ok = [r for r in rows if not r["error"]]
        json_rate = len(ok) / len(rows)

        warnings = sum(len(r["warnings"]) for r in ok)
        repaired = sum(r["repaired"] for r in ok)
        exclusions = sum(r["exclusions"] for r in ok)
        documents = sum(r["documents"] for r in ok)
        # 한도 금액을 실제로 뽑은 카테고리 수. 대부분의 약관이 "증권에 기재된 금액"이라
        # 낮게 나오는 게 정상이지만, 뽑을 수 있는 걸 놓치는지 보는 지표다.
        limits = sum(1 for r in ok if r["limit_amount"] is not None)
        avg = sum(r["seconds"] for r in ok) / len(ok) if ok else 0.0

        print(f"  {name:14} {json_rate:>5.0%} {warnings:>6} {repaired:>6} {exclusions:>6} "
              f"{documents:>6} {limits:>6} {avg:>8.1f}초")
        summary.append({
            "provider": name, "json_success_rate": round(json_rate, 3),
            "hallucination_warnings": warnings, "chunk_id_repairs": repaired,
            "exclusions": exclusions, "required_documents": documents,
            "limit_amounts_found": limits, "categories": len(ok),
            "avg_seconds": round(avg, 2),
        })

    print("\n  환각/교정은 낮을수록, 면책/서류/한도는 높을수록 좋다.")
    print("  단 면책·서류가 많은데 환각 경고도 많으면 지어낸 것이므로 함께 봐야 한다.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "extraction_comparison.json").write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    lines = ["# 추출 모델 비교 — 사람 검토용\n"]
    for category, display_name, _ in targets:
        lines.append(f"\n## {category} ({display_name})\n")
        for name in runnable:
            r = next(x for x in results[name] if x["category"] == category)
            if r["error"]:
                lines.append(f"\n### {name} — 실패: {r['error']}\n")
                continue
            lines.append(
                f"\n### {name} ({r['seconds']:.1f}초)\n\n"
                f"- 담보명: **{r['title']}**\n"
                f"- 한도: {r['limit_amount']}\n"
                f"- 면책 {r['exclusions']}건 / 서류 {r['documents']}건 / "
                f"세부 {r['detail_items']}건 / 근거 {r['sources']}건\n"
                f"- 교정 {r['repaired']}건 / 경고 {len(r['warnings'])}건\n"
            )
            for w in r["warnings"]:
                lines.append(f"  - 경고: {w}\n")
    (OUTPUT_DIR / "extraction_comparison.md").write_text("\n".join(lines), encoding="utf-8")

    print("\n저장:")
    print("  data/eval/extraction_comparison.json")
    print("  data/eval/extraction_comparison.md")


if __name__ == "__main__":
    main()
