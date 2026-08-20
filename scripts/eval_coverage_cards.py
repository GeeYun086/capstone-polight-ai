"""보장 카드 정확도를 채점한다. 약관 기반과 증권 기반을 같은 정답으로 비교하기 위한 도구.

정답은 증권의 보장내용 표를 사람이 그대로 옮긴 것이다(certificate_gold.json).
증권은 표라서 정답에 이견이 없다 - 채점자가 누구든 같은 답이 나온다. 약관을 정답
근거로 쓰면 "이 조항이 이 담보에 해당하는가"를 사람이 판단해야 해서 주관이 섞이고,
그 순간 수치의 설득력이 무너진다.

미리 알아둘 것: 약관 기반은 limit_amount에서 구조적으로 진다. 약관 원문에 숫자가
없기 때문이다("보험가입금액을 한도로"). 이건 불공정한 비교가 아니라 측정의 요점이다.
모델 성능 차이가 아니라 정보원의 차이를 보여주는 수치다.

사용법
    # 약관 기반(현재)
    python scripts/eval_coverage_cards.py --payload-dir data/eval/pred_terms \
        --out data/eval/cards_terms.json --note "약관 기반"

    # 증권 기반(신규)
    python scripts/eval_coverage_cards.py --payload-dir data/eval/pred_cert \
        --out data/eval/cards_cert.json --note "증권 기반"

payload-dir에는 증권 하나당 {certificate_id}.json 을 둔다. 내용은 완료 콜백 페이로드
전체(AnalysisCompleteCallback)여도 되고 coverageItems 배열만이어도 된다.
"""

import argparse
import json
import re
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_GOLD = PROJECT_ROOT / "data" / "eval" / "certificate_gold.json"
CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"

# 담보 이름이 이 정도 닮으면 같은 담보로 본다.
# "해외여행중 휴대품손해(분실제외)" vs "해외여행중 휴대품손해(분실제외) 특별약관"처럼
# 접미사만 다른 경우를 붙이기 위한 값이다.
NAME_SIMILARITY_THRESHOLD = 0.6

# 화면에 그대로 노출되면 안 되는 값들. LLM이 null을 문자열로 뱉는 사고가 실제로 있었다.
POLLUTED = {"null", "none", "n/a", "없음", "undefined"}


def normalize(text: str | None) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def is_polluted(value: str | None) -> bool:
    return value is not None and value.strip().lower() in POLLUTED


def load_items(path: Path) -> list[dict]:
    """콜백 페이로드 전체든 coverageItems 배열이든 받아준다."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data.get("coverageItems") or data.get("coverage_items") or []
    return data


def load_policy_text(chunks_dir: Path, terms_ref: str | None) -> str | None:
    """인용 검증용 원문. 인용문이 이 안에 없으면 지어낸 문장이다."""
    if not terms_ref:
        return None
    path = chunks_dir / f"{terms_ref}_chunks.json"
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return normalize("\n".join(c["text"] for c in json.load(f)))


def match_items(golds: list[dict], preds: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """정답 담보와 예측 담보를 짝짓는다. (짝, 못 찾은 정답, 여분의 예측)

    카테고리가 같은 것 중 이름이 가장 닮은 것을 고른다. 카테고리가 한쪽에라도 없으면
    이름 유사도만으로 판단한다. 한 예측은 한 정답에만 쓰인다(중복 매칭 금지) -
    그러지 않으면 담보 하나를 여러 번 세어 재현율이 부풀려진다.
    """
    remaining = list(preds)
    pairs, missed = [], []

    for gold in golds:
        best, best_score = None, 0.0
        for pred in remaining:
            gc, pc = gold.get("category"), pred.get("category")
            if gc and pc and gc != pc:
                continue
            score = similarity(gold.get("name", ""), pred.get("title", ""))
            if gc and pc and gc == pc:
                score = max(score, 0.75)  # 카테고리 일치는 그 자체로 강한 근거
            if score > best_score:
                best, best_score = pred, score

        if best is not None and best_score >= NAME_SIMILARITY_THRESHOLD:
            pairs.append((gold, best))
            remaining.remove(best)
        else:
            missed.append(gold)

    return pairs, missed, remaining


def main() -> None:
    parser = argparse.ArgumentParser(description="보장 카드 정확도 채점")
    parser.add_argument("--gold", type=str, default=str(DEFAULT_GOLD))
    parser.add_argument("--payload-dir", type=str, required=True)
    parser.add_argument("--chunks-dir", type=str, default=str(CHUNKS_DIR))
    parser.add_argument("--out", type=str, default=None)
    parser.add_argument("--note", type=str, default="")
    # 정답(증권)이 아직 없을 때를 위한 모드.
    #
    # 담보 P/R은 정답 없이는 못 재지만, 예측값만으로 계산되는 지표는 잴 수 있다.
    # limitLabel 충족률·오염 필드·인용 환각률이 그것이고, 이 셋만으로도 화면에
    # 빈칸이 뜨는지, 지어낸 인용이 실리는지는 알 수 있다. 증권을 구하기 전에
    # 시스템 A의 baseline을 절반이라도 고정해두려는 용도다.
    parser.add_argument("--no-gold", action="store_true", help="정답 없이 예측값 지표만 잰다")
    parser.add_argument("--terms-ref", type=str, default=None, help="--no-gold일 때 인용 검증에 쓸 약관 (예: db_travel)")
    args = parser.parse_args()

    if args.no_gold:
        certificates = [
            {"certificate_id": p.stem, "coverages": [], "terms_ref": args.terms_ref}
            for p in sorted(Path(args.payload_dir).glob("*.json"))
        ]
        if not certificates:
            raise SystemExit(f"예측 파일이 없습니다: {args.payload_dir}")
        gold_path = Path("(없음)")
    else:
        gold_path = Path(args.gold)
        if not gold_path.exists():
            raise SystemExit(
                f"정답이 없습니다: {gold_path}\n"
                "data/eval/certificate_gold.example.json 을 복사해서 채우세요.\n"
                "정답 없이 예측값 지표만 재려면 --no-gold 를 쓰세요."
            )

        with gold_path.open("r", encoding="utf-8") as f:
            certificates = [c for c in json.load(f) if not str(c.get("certificate_id", "")).startswith("_")]

    payload_dir = Path(args.payload_dir)
    totals = {
        "tp": 0, "fp": 0, "fn": 0,
        "amount_correct": 0, "amount_gold_present": 0,
        "status_correct": 0, "status_checked": 0,
        "label_filled": 0, "label_total": 0,
        "polluted_fields": 0,
        "quotes_total": 0, "quotes_hallucinated": 0,
    }
    per_cert = []

    for cert in certificates:
        cid = cert["certificate_id"]
        payload_path = payload_dir / f"{cid}.json"
        if not payload_path.exists():
            print(f"[!] {cid}: 예측 파일 없음 ({payload_path}) - 건너뜁니다")
            continue

        preds = load_items(payload_path)
        golds = cert.get("coverages", [])
        pairs, missed, extra = match_items(golds, preds)
        policy_text = load_policy_text(Path(args.chunks_dir), cert.get("terms_ref"))

        tp, fp, fn = len(pairs), len(extra), len(missed)
        amount_ok = amount_n = status_ok = 0

        for gold, pred in pairs:
            if gold.get("limit_amount") is not None:
                amount_n += 1
                if pred.get("limitAmount") == gold["limit_amount"]:
                    amount_ok += 1
            expected = "COVERED" if gold.get("covered", True) else "NOT_COVERED"
            actual = pred.get("coverageStatus")
            if expected == "COVERED" and actual in {"COVERED", "PARTIALLY_COVERED"}:
                status_ok += 1
            elif expected == actual:
                status_ok += 1

        label_filled = sum(
            1 for p in preds if p.get("limitLabel") and not is_polluted(p.get("limitLabel"))
        )
        polluted = sum(
            1 for p in preds for key in ("title", "subtitle", "limitLabel", "conditions")
            if is_polluted(p.get(key))
        )

        quotes_n = halluc = 0
        if policy_text:
            for p in preds:
                for s in p.get("sources", []):
                    quote = s.get("quoteText")
                    if not quote:
                        continue
                    quotes_n += 1
                    if normalize(quote) not in policy_text:
                        halluc += 1

        totals["tp"] += tp; totals["fp"] += fp; totals["fn"] += fn
        totals["amount_correct"] += amount_ok; totals["amount_gold_present"] += amount_n
        totals["status_correct"] += status_ok; totals["status_checked"] += len(pairs)
        totals["label_filled"] += label_filled; totals["label_total"] += len(preds)
        totals["polluted_fields"] += polluted
        totals["quotes_total"] += quotes_n; totals["quotes_hallucinated"] += halluc

        entry = {
            "certificate_id": cid, "gold_n": len(golds), "pred_n": len(preds),
            "limit_label_fill_rate": label_filled / len(preds) if preds else 0.0,
            "polluted_fields": polluted,
            "quote_hallucination_rate": halluc / quotes_n if quotes_n else 0.0,
            "quotes": quotes_n,
            "limit_amount_filled": sum(1 for p in preds if p.get("limitAmount") is not None),
        }

        if args.no_gold:
            # 정답이 없으면 TP/FP는 의미가 없다(전부 FP로 잡힌다). 예측값 지표만 보여준다.
            print(
                f"{cid:14} 담보 {len(preds):2}  "
                f"limitLabel {entry['limit_label_fill_rate']:5.0%}  "
                f"limitAmount {entry['limit_amount_filled']}/{len(preds)}  "
                f"오염 {polluted:2}  환각 {entry['quote_hallucination_rate']:5.1%} ({halluc}/{quotes_n})"
            )
        else:
            entry.update({
                "tp": tp, "fp": fp, "fn": fn,
                "missed": [g.get("name") for g in missed],
                "extra": [p.get("title") for p in extra],
            })
            print(f"{cid:14} 정답 {len(golds):2}  예측 {len(preds):2}  TP {tp:2} FP {fp:2} FN {fn:2}")
            if missed:
                print(f"             못 찾음: {', '.join(g.get('name', '?') for g in missed)}")
            if extra:
                print(f"             지어냄: {', '.join(p.get('title', '?') for p in extra)}")
        per_cert.append(entry)

    if not per_cert:
        raise SystemExit("채점한 증권이 없습니다.")

    t = totals
    precision = t["tp"] / (t["tp"] + t["fp"]) if t["tp"] + t["fp"] else 0.0
    recall = t["tp"] / (t["tp"] + t["fn"]) if t["tp"] + t["fn"] else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    def rate(num: int, den: int) -> float:
        return num / den if den else 0.0

    print("\n" + "=" * 58)
    label = "예측 파일" if args.no_gold else "증권"
    print(f"{label} {len(per_cert)}건 / {args.note or '(메모 없음)'}")
    print("=" * 58)
    if args.no_gold:
        print("  [정답 없음 - 담보 P/R, limitAmount·status 정확도는 측정 불가]")
    else:
        print(f"  담보 검출 Precision   {precision:.1%}   (지어낸 담보 {t['fp']}건)")
        print(f"  담보 검출 Recall      {recall:.1%}   (빠뜨린 담보 {t['fn']}건)")
        print(f"  담보 검출 F1          {f1:.1%}")
        print()
        print(f"  limitAmount 정확도    {rate(t['amount_correct'], t['amount_gold_present']):.1%}"
              f"   ({t['amount_correct']}/{t['amount_gold_present']})")
        print(f"  coverageStatus 정확도 {rate(t['status_correct'], t['status_checked']):.1%}"
              f"   ({t['status_correct']}/{t['status_checked']})")
    print(f"  limitLabel 충족률     {rate(t['label_filled'], t['label_total']):.1%}"
          f"   ({t['label_filled']}/{t['label_total']})   ← 화면 빈칸 노출")
    print(f"  오염 필드('null' 문자) {t['polluted_fields']}건")
    print(f"  인용 환각률           {rate(t['quotes_hallucinated'], t['quotes_total']):.1%}"
          f"   ({t['quotes_hallucinated']}/{t['quotes_total']})")

    if args.out:
        payload = {
            "note": args.note,
            "payload_dir": args.payload_dir,
            "certificates": len(per_cert),
            "detection": {"precision": precision, "recall": recall, "f1": f1, **{k: t[k] for k in ("tp", "fp", "fn")}},
            "limit_amount_accuracy": rate(t["amount_correct"], t["amount_gold_present"]),
            "coverage_status_accuracy": rate(t["status_correct"], t["status_checked"]),
            "limit_label_fill_rate": rate(t["label_filled"], t["label_total"]),
            "polluted_fields": t["polluted_fields"],
            "quote_hallucination_rate": rate(t["quotes_hallucinated"], t["quotes_total"]),
            "per_certificate": per_cert,
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
