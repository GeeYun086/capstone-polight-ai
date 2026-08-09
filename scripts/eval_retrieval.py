"""검색 품질 평가 스크립트.

왜 필요한가: "임신·출산" 질의가 실패했을 때 임시로 "'임신'이 포함된 청크"를 정답으로
잡고 판정했는데, 그 기준이 실제 정답과 어긋나 개선 여부를 잘못 읽을 뻔했다.
모델이나 파싱을 바꿀 때마다 "좋아졌다"를 숫자로 말하려면 고정된 평가셋이 필요하다.

정답을 chunk_id가 아니라 "페이지 + 본문 문구"로 라벨링하는 이유:
청킹이나 파서를 바꾸면 chunk_id가 전부 달라져 평가셋이 통째로 무효화된다.
페이지와 본문은 같은 PDF를 쓰는 한 바뀌지 않는다.

사용법
    # 1) 라벨링 도우미 - 키워드가 들어있는 청크의 페이지/제목을 찾아준다
    python scripts/eval_retrieval.py --find "임신"

    # 2) 평가 실행
    python scripts/eval_retrieval.py

    # 3) MMR 효과 비교
    python scripts/eval_retrieval.py --no-mmr

    # 4) 임베딩 모델 비교 (모델별로 임베딩 디렉터리를 따로 둔 경우)
    python scripts/eval_retrieval.py --embeddings-dir data/embeddings_large
"""

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.repositories.base import ChunkHit  # noqa: E402
from app.repositories.file_repository import FileVectorRepository  # noqa: E402
from app.services.embedding_service import embed_query  # noqa: E402
from app.services.rag_service import hybrid_search  # noqa: E402
from app.services.reranker import mmr_select  # noqa: E402

DEFAULT_QUESTIONS = PROJECT_ROOT / "data" / "eval" / "questions.json"
DEFAULT_CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"
DEFAULT_EMBEDDINGS_DIR = PROJECT_ROOT / "data" / "embeddings"

# 지표를 계산할 상위 개수
RECALL_AT = (1, 3, 5, 8)


def normalize(text: str) -> str:
    """공백 차이로 매칭이 실패하지 않도록 정규화한다 (chunk_policy와 같은 방식)."""
    return re.sub(r"\s+", "", text).lower()


# 검색된 청크 하나가 정답 라벨과 맞는지 판정한다.
# page가 주어지면 청크의 페이지 범위가 그 페이지를 포함해야 하고,
# contains 문구는 본문 또는 제목에 있어야 한다.
def is_match(hit: ChunkHit, gold: dict) -> bool:
    page = gold.get("page")
    if page is not None and not (hit.page_start <= page <= hit.page_end):
        return False

    needle = normalize(gold["contains"])
    haystack = normalize(f"{hit.section_title}\n{hit.text}")
    return needle in haystack


def first_hit_rank(hits: list[ChunkHit], golds: list[dict]) -> int | None:
    """정답이 처음 등장한 순위(1부터). 없으면 None."""
    for rank, hit in enumerate(hits, start=1):
        if any(is_match(hit, gold) for gold in golds):
            return rank
    return None


# ── 라벨링 도우미 ────────────────────────────────────────────

# 평가셋을 손으로 만들 때 278개 청크를 눈으로 뒤지는 건 비현실적이라,
# 키워드로 후보를 찾아 페이지와 제목을 보여준다. 그대로 gold에 옮겨 적으면 된다.
def find_chunks(chunks_dir: Path, policy_id: str, keyword: str) -> None:
    path = chunks_dir / f"{policy_id}_chunks.json"
    if not path.exists():
        raise FileNotFoundError(f"청크 파일이 없습니다: {path}")

    with path.open("r", encoding="utf-8") as f:
        chunks = json.load(f)

    needle = normalize(keyword)
    found = 0
    for chunk in chunks:
        if needle not in normalize(f"{chunk['section_title']}\n{chunk['text']}"):
            continue
        found += 1
        index = normalize(chunk["text"]).find(needle)
        raw_index = chunk["text"].find(keyword)
        snippet = chunk["text"][max(0, raw_index - 30) : raw_index + 70] if raw_index >= 0 else chunk["text"][:100]
        print(f'  page {chunk["page_start"]}~{chunk["page_end"]}  [{chunk["coverage_type"]}]  {chunk["chunk_id"]}')
        print(f'    제목: {chunk["section_title"][:60]}')
        print(f'    본문: ...{snippet.replace(chr(10), " ")}...')
        print()

    print(f'"{keyword}" 포함 청크: {found}개')
    if found:
        print("\n위 결과에서 page와 본문 문구를 골라 questions.json의 gold에 적으세요.")


# ── 평가 ─────────────────────────────────────────────────────


def evaluate(
    questions: list[dict],
    repository: FileVectorRepository,
    policy_id: str,
    top_k: int,
    use_mmr: bool,
    use_hybrid: bool,
    multiplier: int,
    lambda_: float,
) -> None:
    ranks: list[int | None] = []

    print(f'{"ID":5} {"유형":10} {"순위":>4}  질문')
    print("-" * 78)

    for item in questions:
        vector = embed_query(item["question"])
        pool = top_k * multiplier if use_mmr else top_k

        # 임베딩 모델을 비교할 때는 --no-hybrid로 BM25를 꺼야 한다.
        # 켜두면 키워드 점수가 섞여 모델 간 차이가 희석된다.
        if use_hybrid:
            candidates = hybrid_search(repository, item["question"], vector, policy_id, pool)
        else:
            candidates = repository.search(vector, policy_id=policy_id, top_k=pool)

        hits = mmr_select(candidates, top_k=top_k, lambda_=lambda_) if use_mmr else candidates

        rank = first_hit_rank(hits, item["gold"])
        ranks.append(rank)

        rank_text = str(rank) if rank else "-"
        print(f'{item["id"]:5} {item.get("type", ""):10} {rank_text:>4}  {item["question"][:52]}')

    print("-" * 78)
    total = len(ranks)
    mode = f"MMR {'O' if use_mmr else 'X'} / hybrid {'O' if use_hybrid else 'X'}"
    print(f"\n질문 {total}개 / {mode} / top_k={top_k}")

    for k in RECALL_AT:
        if k > top_k:
            continue
        hit_count = sum(1 for r in ranks if r is not None and r <= k)
        print(f"  Recall@{k}: {hit_count}/{total} ({hit_count / total * 100:.1f}%)")

    # MRR: 첫 정답의 역순위 평균. 정답을 못 찾으면 0으로 센다.
    mrr = sum(1 / r for r in ranks if r) / total
    print(f"  MRR      : {mrr:.4f}")

    missed = [q["id"] for q, r in zip(questions, ranks) if r is None]
    if missed:
        print(f"\n  실패한 질문: {', '.join(missed)}")


def main() -> None:
    settings = get_settings()

    parser = argparse.ArgumentParser(description="검색 품질 평가 (Recall@k, MRR)")
    parser.add_argument("--questions", type=str, default=str(DEFAULT_QUESTIONS))
    parser.add_argument("--chunks-dir", type=str, default=str(DEFAULT_CHUNKS_DIR))
    parser.add_argument(
        "--embeddings-dir",
        type=str,
        default=str(DEFAULT_EMBEDDINGS_DIR),
        help="모델별로 임베딩을 따로 저장했다면 그 디렉터리를 지정한다",
    )
    parser.add_argument("--policy-id", type=str, default="db_travel")
    parser.add_argument("--top-k", type=int, default=settings.top_k)
    parser.add_argument("--no-mmr", action="store_true", help="MMR 없이 순수 유사도 정렬로 평가")
    parser.add_argument(
        "--no-hybrid",
        action="store_true",
        help="BM25를 끄고 벡터 검색만 사용. 임베딩 모델 비교 시 변수를 분리하려면 켜야 한다",
    )
    parser.add_argument("--find", type=str, default=None, help="라벨링 도우미: 키워드로 청크 검색")

    args = parser.parse_args()

    if args.find:
        find_chunks(Path(args.chunks_dir), args.policy_id, args.find)
        return

    questions_path = Path(args.questions)
    if not questions_path.exists():
        raise FileNotFoundError(
            f"평가셋이 없습니다: {questions_path}\n"
            f"data/eval/questions.example.json을 복사해서 채우세요."
        )

    with questions_path.open("r", encoding="utf-8") as f:
        questions = json.load(f)

    repository = FileVectorRepository(
        chunks_dir=Path(args.chunks_dir),
        embeddings_dir=Path(args.embeddings_dir),
    )

    evaluate(
        questions=questions,
        repository=repository,
        policy_id=args.policy_id,
        top_k=args.top_k,
        use_mmr=not args.no_mmr,
        use_hybrid=not args.no_hybrid,
        multiplier=settings.mmr_candidate_multiplier,
        lambda_=settings.mmr_lambda,
    )


if __name__ == "__main__":
    main()
