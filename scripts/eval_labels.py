"""라벨링(coverage_category) 정확도를 채점한다.

make_label_sample.py로 만든 정답지와 현재 자동 라벨을 대조해 카테고리별 정밀도/재현율을 낸다.

두 가지 실패를 나눠서 본다. 원인이 다르고 고치는 방법도 다르기 때문이다.

  과잉 라벨(over)   정답은 null인데 라벨이 붙음.  본문을 스친 키워드 하나로 붙는 경우다.
                    -> 검색 필터에 쓰레기가 섞여 들어온다. 제목 기준 매칭으로 고친다.
  누락 라벨(under)  정답은 있는데 null.           키워드 목록에 그 표현이 없는 경우다.
                    -> 진짜 근거가 필터에서 빠진다. 상속·임베딩 분류로 고친다.

사용법
    python scripts/eval_labels.py
    python scripts/eval_labels.py --out data/eval/labels_before.json --note "개선 전"
    # 라벨링 로직을 고치고 청크를 다시 만든 뒤
    python scripts/eval_labels.py --out data/eval/labels_after.json  --note "제목기준+상속"
"""

import argparse
import csv
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"
DEFAULT_GOLD = PROJECT_ROOT / "data" / "eval" / "label_gold.csv"

NULL_TOKENS = {"", "null", "none", "없음", "-"}


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def load_auto_labels(chunks_dir: Path) -> tuple[dict, dict]:
    """(policy, chunk_id) -> 라벨, 그리고 본문 앞부분 -> 라벨.

    본문 기반 조회를 함께 두는 이유: 청킹 로직을 건드리면 chunk_id가 달라져
    정답지가 통째로 무효가 된다. 본문으로도 찾을 수 있으면 정답지를 살릴 수 있다.
    """
    by_id: dict[tuple[str, str], str | None] = {}
    by_text: dict[str, str | None] = {}

    for path in sorted(chunks_dir.glob("*_chunks.json")):
        policy = path.stem.replace("_chunks", "")
        with path.open("r", encoding="utf-8") as f:
            for chunk in json.load(f):
                label = chunk.get("matched_category")
                by_id[(policy, chunk["chunk_id"])] = label
                by_text[normalize(chunk["text"][:200])] = label

    return by_id, by_text


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def main() -> None:
    parser = argparse.ArgumentParser(description="라벨링 정확도 채점")
    parser.add_argument("--gold", type=str, default=str(DEFAULT_GOLD))
    parser.add_argument("--chunks-dir", type=str, default=str(CHUNKS_DIR))
    parser.add_argument("--out", type=str, default=None)
    parser.add_argument("--note", type=str, default="")
    args = parser.parse_args()

    gold_path = Path(args.gold)
    if not gold_path.exists():
        raise SystemExit(
            f"정답지가 없습니다: {gold_path}\n먼저 python scripts/make_label_sample.py 를 실행하세요."
        )

    by_id, by_text = load_auto_labels(Path(args.chunks_dir))

    pairs: list[tuple[str, str | None, str | None]] = []  # (sample_id, gold, auto)
    unfilled = 0
    unmatched = []

    with gold_path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            raw = (row.get("gold_category") or "").strip()
            if raw.lower() in NULL_TOKENS and raw == "":
                unfilled += 1
                continue
            gold = None if raw.lower() in NULL_TOKENS else raw

            key = (row["policy"], row["chunk_id"])
            if key in by_id:
                auto = by_id[key]
            else:
                auto = by_text.get(normalize((row.get("text_preview") or "")[:200]), "__MISSING__")
                if auto == "__MISSING__":
                    unmatched.append(row["sample_id"])
                    continue
            pairs.append((row["sample_id"], gold, auto))

    if unfilled:
        print(f"[!] 아직 안 채운 행 {unfilled}개는 건너뜁니다.")
    if unmatched:
        print(f"[!] 청크를 못 찾은 행 {len(unmatched)}개: {', '.join(unmatched[:8])}")
    if not pairs:
        raise SystemExit("채점할 행이 없습니다. gold_category 열을 채우세요.")

    total = len(pairs)
    exact = sum(1 for _, g, a in pairs if g == a)

    # 라벨 유무만 본 4분면. 필터 관점에서 가장 중요한 두 실패가 여기 드러난다.
    over = sum(1 for _, g, a in pairs if g is None and a is not None)
    under = sum(1 for _, g, a in pairs if g is not None and a is None)
    wrong_cat = sum(1 for _, g, a in pairs if g is not None and a is not None and g != a)
    both_null = sum(1 for _, g, a in pairs if g is None and a is None)

    print(f"\n표본 {total}개 / 청크 {args.chunks_dir}")
    print("=" * 62)
    print(f"  전체 정확도(Exact)     {exact}/{total} ({exact / total:.1%})")
    print()
    print(f"  정답 null & 라벨 null   {both_null:4d}  ({both_null / total:5.1%})  정상")
    print(f"  과잉 라벨 (over)        {over:4d}  ({over / total:5.1%})  ← 필터에 쓰레기 유입")
    print(f"  누락 라벨 (under)       {under:4d}  ({under / total:5.1%})  ← 근거가 필터에서 빠짐")
    print(f"  다른 카테고리로 오분류    {wrong_cat:4d}  ({wrong_cat / total:5.1%})")

    # 카테고리별 P/R/F1
    cats = sorted({g for _, g, _ in pairs if g} | {a for _, _, a in pairs if a})
    stats = {}
    print("\n" + "-" * 62)
    print(f"{'카테고리':22} {'정답':>4} {'예측':>4} {'P':>6} {'R':>6} {'F1':>6}")
    print("-" * 62)
    macro = []
    for cat in cats:
        tp = sum(1 for _, g, a in pairs if g == cat and a == cat)
        fp = sum(1 for _, g, a in pairs if g != cat and a == cat)
        fn = sum(1 for _, g, a in pairs if g == cat and a != cat)
        p, r, f1 = prf(tp, fp, fn)
        stats[cat] = {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f1}
        macro.append(f1)
        print(f"{cat:22} {tp + fn:>4} {tp + fp:>4} {p:>6.2f} {r:>6.2f} {f1:>6.2f}")
    macro_f1 = sum(macro) / len(macro) if macro else 0.0
    print("-" * 62)
    print(f"{'macro F1':22} {'':>4} {'':>4} {'':>6} {'':>6} {macro_f1:>6.2f}")

    # 어떤 오분류가 잦은지. 매핑 키워드를 어디부터 고칠지 알려준다.
    confusion = Counter(
        (g or "null", a or "null") for _, g, a in pairs if g != a
    )
    if confusion:
        print("\n오분류 상위 (정답 -> 자동):")
        for (g, a), n in confusion.most_common(8):
            print(f"  {g:22} -> {a:22} {n}건")

    if args.out:
        payload = {
            "note": args.note,
            "chunks_dir": args.chunks_dir,
            "gold": str(gold_path),
            "total": total,
            "exact_accuracy": exact / total,
            "over_label": over / total,
            "under_label": under / total,
            "wrong_category": wrong_cat / total,
            "macro_f1": macro_f1,
            "per_category": stats,
            "confusion": [{"gold": g, "auto": a, "n": n} for (g, a), n in confusion.most_common()],
            "git_commit": _git_commit(),
        }
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n측정 결과 저장: {out_path}")


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
    except Exception:
        return None


if __name__ == "__main__":
    main()
