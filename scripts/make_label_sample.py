"""라벨링 정확도 평가용 정답지 템플릿을 만든다.

왜 필요한가: 증권 기반으로 바꾸면 "증권에서 받은 카테고리로 약관을 좁혀 검색"하게 되는데,
그 필터가 딛고 서는 것이 약관 청크의 coverage_category다. 라벨이 틀려 있으면 필터를 켜는
순간 검색 품질이 지금보다 나빠진다. 그래서 필터를 붙이기 전에 라벨 정확도를 알아야 한다.

측정에서 지킨 두 가지:

  무작위 추출    눈에 띄는 청크만 고르면 편향된다. 고정 시드로 뽑아 재현 가능하게 한다.
  자동 라벨 숨김  정답지에 현재 라벨을 같이 보여주면 사람이 그것에 끌려간다(앵커링).
                 그래서 이 CSV에는 자동 라벨이 들어가지 않는다. 채점할 때 chunk_id로 붙인다.

사용법
    python scripts/make_label_sample.py                 # 100개 뽑기
    python scripts/make_label_sample.py --size 150
    # -> data/eval/label_gold.csv 를 열어 gold_category 열만 채운다
    python scripts/eval_labels.py                       # 채점
"""

import argparse
import csv
import json
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"
CATEGORIES_PATH = PROJECT_ROOT / "config" / "standard_categories.json"
DEFAULT_OUT = PROJECT_ROOT / "data" / "eval" / "label_gold.csv"

# 시드를 고정한다. 표본이 매번 달라지면 개선 전후를 같은 잣대로 잴 수 없고,
# 정답을 다시 만들어야 한다.
SEED = 42

# 본문 미리보기 길이. 사람이 판단하기에 충분하면서 엑셀 셀에서 읽을 수 있는 정도.
PREVIEW_CHARS = 300


def load_all_chunks(chunks_dir: Path) -> list[dict]:
    rows = []
    for path in sorted(chunks_dir.glob("*_chunks.json")):
        policy = path.stem.replace("_chunks", "")
        with path.open("r", encoding="utf-8") as f:
            for chunk in json.load(f):
                rows.append({"policy": policy, "chunk": chunk})
    return rows


def show_sample(out_path: Path, chunks_dir: Path, sample_id: str) -> None:
    """정답지의 한 행을 원문 전체와 함께 보여준다.

    text_preview는 엑셀에서 읽히도록 300자로 잘라놨는데, 그것만으로 판단이 안 되는
    조각이 있다. 그때 청크 파일을 직접 열어보는 대신 이걸 쓴다.
    """
    with out_path.open("r", encoding="utf-8-sig", newline="") as f:
        row = next((r for r in csv.DictReader(f) if r["sample_id"] == sample_id.upper()), None)
    if row is None:
        raise SystemExit(f"{sample_id} 를 찾을 수 없습니다.")

    path = chunks_dir / f'{row["policy"]}_chunks.json'
    with path.open("r", encoding="utf-8") as f:
        chunk = next((c for c in json.load(f) if c["chunk_id"] == row["chunk_id"]), None)

    print(f'[{row["sample_id"]}] {row["policy"]}  p.{row["pages"]}')
    print(f'특약   : {row["clause_path"] or "(없음)"}')
    print(f'조항명 : {row["section_title"]}')
    print("-" * 70)
    print(chunk["text"] if chunk else row["text_preview"])
    print("-" * 70)
    print("이 조각이 어떤 담보에 대한 질문의 근거가 될 수 있는지 판단하세요.")
    print("담보 이름을 바꿔도 내용이 그대로인 정형 문구라면 null입니다.")


def main() -> None:
    parser = argparse.ArgumentParser(description="라벨링 정답지 템플릿 생성")
    parser.add_argument("--size", type=int, default=100, help="표본 수 (기본 100)")
    parser.add_argument("--chunks-dir", type=str, default=str(CHUNKS_DIR))
    parser.add_argument("--out", type=str, default=str(DEFAULT_OUT))
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--show", type=str, default=None, help="정답지의 한 행을 원문 전체와 함께 본다 (예: --show S001)")
    args = parser.parse_args()

    out_path = Path(args.out)
    if args.show:
        show_sample(out_path, Path(args.chunks_dir), args.show)
        return

    if out_path.exists():
        raise SystemExit(
            f"이미 있습니다: {out_path}\n"
            "정답지를 덮어쓰면 채워둔 답이 사라집니다. 다시 만들려면 파일을 옮기고 실행하세요."
        )

    population = load_all_chunks(Path(args.chunks_dir))
    if not population:
        raise SystemExit(f"청크 파일이 없습니다: {args.chunks_dir}")

    size = min(args.size, len(population))
    sample = random.Random(args.seed).sample(population, size)

    with CATEGORIES_PATH.open("r", encoding="utf-8") as f:
        categories = json.load(f)

    # utf-8-sig: 엑셀이 UTF-8 CSV를 한글 깨짐 없이 열게 하려면 BOM이 필요하다.
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "sample_id",
                "policy",
                "chunk_id",
                "pages",
                "clause_path",
                "section_title",
                "text_preview",
                "gold_category",
                "note",
            ]
        )
        for i, row in enumerate(sample, start=1):
            c = row["chunk"]
            preview = " ".join(c["text"][:PREVIEW_CHARS].split())
            writer.writerow(
                [
                    f"S{i:03d}",
                    row["policy"],
                    c["chunk_id"],
                    f'{c.get("page_start")}-{c.get("page_end")}',
                    c.get("clause_path") or "",
                    (c.get("section_title") or "")[:120],
                    preview,
                    "",  # 사람이 채울 칸
                    "",
                ]
            )

    print(f"정답지 템플릿 생성: {out_path}")
    print(f"  표본 {size}개 / 모집단 {len(population)}개 / 시드 {args.seed}")
    print(f"  약관별 분포: ", end="")
    counts: dict[str, int] = {}
    for row in sample:
        counts[row["policy"]] = counts.get(row["policy"], 0) + 1
    print(", ".join(f"{k} {v}" for k, v in sorted(counts.items())))

    print("\ngold_category 열에 아래 중 하나를 적으세요. 해당 없으면 null:")
    for key, meta in sorted(categories.items(), key=lambda kv: kv[1]["ui_priority"]):
        print(f"  {key:22} {meta['display_name']}")
    print("  null                   어느 담보에도 해당하지 않음 (용어정의·청구절차·일반면책 등)")
    print("\n판단 기준: '이 조각이 그 담보를 설명하는 조항인가'로 보세요.")
    print("다른 담보 얘기를 하다가 단어만 스친 경우는 null입니다.")
    print(f"\n채우고 나면:  python scripts/eval_labels.py")


if __name__ == "__main__":
    main()
