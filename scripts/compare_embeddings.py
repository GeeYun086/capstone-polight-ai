"""임베딩 모델 비교.

벤더별로 같은 청크를 임베딩해 data/embeddings_{벤더}/에 저장하고,
같은 평가셋으로 Recall@k와 MRR을 재서 표로 출력한다.

BM25는 끄고 측정한다. 켜두면 키워드 점수가 섞여 임베딩 모델 간 차이가 희석된다.

사용법
    python scripts/compare_embeddings.py                    # 키가 있는 벤더 전부
    python scripts/compare_embeddings.py --providers openai-small,upstage
    python scripts/compare_embeddings.py --skip-embed       # 이미 임베딩했으면 평가만
"""

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.repositories.base import SearchScope
from app.repositories.file_repository import FileVectorRepository  # noqa: E402
from app.services.embedding_providers import (  # noqa: E402
    PROVIDERS,
    build_client,
    embed,
    get_provider,
    has_api_key,
)
from app.services.reranker import mmr_select  # noqa: E402
from scripts.embed_chunks import build_embed_text  # noqa: E402
from scripts.eval_retrieval import first_hit_rank  # noqa: E402

CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"
QUESTIONS_PATH = PROJECT_ROOT / "data" / "eval" / "questions.json"

BATCH_SIZE = 50


def embeddings_dir(provider_name: str) -> Path:
    return PROJECT_ROOT / "data" / f"embeddings_{provider_name}"


def embed_document(provider_name: str, chunks_path: Path) -> tuple[int, float]:
    """청크 파일 하나를 벤더로 임베딩해 저장한다. (차원, 소요초)를 돌려준다."""
    provider = get_provider(provider_name)
    client = build_client(provider)

    with chunks_path.open("r", encoding="utf-8") as f:
        chunks = json.load(f)

    out_dir = embeddings_dir(provider_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / chunks_path.name.replace("_chunks.json", "_embeddings.json")

    started = time.time()
    results: dict[str, list[float]] = {}
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        # 문서 쪽이므로 is_query=False. Upstage는 질의/문서 모델이 다르다.
        vectors = embed(provider, [build_embed_text(c) for c in batch], is_query=False, client=client)
        for chunk, vector in zip(batch, vectors):
            results[chunk["chunk_id"]] = vector

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)

    dimension = len(next(iter(results.values()))) if results else 0
    return dimension, time.time() - started


def evaluate_provider(provider_name: str, questions: list[dict], policy_id: str, top_k: int) -> dict:
    settings = get_settings()
    provider = get_provider(provider_name)
    client = build_client(provider)

    repository = FileVectorRepository(
        chunks_dir=CHUNKS_DIR,
        embeddings_dir=embeddings_dir(provider_name),
    )

    ranks: list[int | None] = []
    latencies: list[float] = []

    for item in questions:
        started = time.time()
        # 질의 쪽이므로 is_query=True
        vector = embed(provider, [item["question"]], is_query=True, client=client)[0]
        latencies.append(time.time() - started)

        # BM25는 끈다 - 임베딩 성능만 비교하기 위해서다
        candidates = repository.search(
            vector,
            scope=SearchScope(document_id=policy_id),
            top_k=top_k * settings.mmr_candidate_multiplier,
        )
        hits = mmr_select(candidates, top_k=top_k, lambda_=settings.mmr_lambda)
        ranks.append(first_hit_rank(hits, item["gold"]))

    total = len(ranks)
    return {
        "recall@1": sum(1 for r in ranks if r and r <= 1) / total,
        "recall@5": sum(1 for r in ranks if r and r <= 5) / total,
        "recall@8": sum(1 for r in ranks if r and r <= 8) / total,
        "mrr": sum(1 / r for r in ranks if r) / total,
        "query_ms": sum(latencies) / len(latencies) * 1000,
        "missed": [q["id"] for q, r in zip(questions, ranks) if r is None],
    }


def main() -> None:
    settings = get_settings()

    parser = argparse.ArgumentParser(description="임베딩 모델 비교 (BM25 끄고 측정)")
    parser.add_argument("--providers", type=str, default=None, help="쉼표 구분. 생략하면 키가 있는 전부")
    parser.add_argument("--policy-id", type=str, default="db_travel")
    parser.add_argument("--top-k", type=int, default=settings.top_k)
    parser.add_argument("--skip-embed", action="store_true", help="임베딩을 건너뛰고 평가만")
    args = parser.parse_args()

    if args.providers:
        names = [n.strip() for n in args.providers.split(",")]
    else:
        names = [n for n in PROVIDERS if has_api_key(PROVIDERS[n])]

    skipped = [n for n in PROVIDERS if n not in names]
    if skipped:
        print(f"제외된 벤더(키 없음 또는 미지정): {', '.join(skipped)}\n")

    if not names:
        raise SystemExit("비교할 벤더가 없습니다. .env에 키를 설정하세요.")

    with QUESTIONS_PATH.open("r", encoding="utf-8") as f:
        questions = json.load(f)

    chunks_path = CHUNKS_DIR / f"{args.policy_id}_chunks.json"
    results: dict[str, dict] = {}

    for name in names:
        print(f"[{name}] 처리 중...")
        try:
            if args.skip_embed:
                dimension, elapsed = 0, 0.0
            else:
                dimension, elapsed = embed_document(name, chunks_path)
                print(f"  임베딩 완료 - 차원 {dimension}, {elapsed:.1f}초")

            metrics = evaluate_provider(name, questions, args.policy_id, args.top_k)
            metrics["dimension"] = dimension
            metrics["embed_sec"] = elapsed
            results[name] = metrics
        except Exception as e:
            print(f"  실패: {e}")

    print()
    print(f'{"벤더":20} {"차원":>6} {"R@1":>7} {"R@5":>7} {"R@8":>7} {"MRR":>7} {"질의ms":>8}')
    print("-" * 68)
    for name, m in results.items():
        print(
            f'{name:20} {m["dimension"]:>6} {m["recall@1"]:>6.1%} {m["recall@5"]:>6.1%} '
            f'{m["recall@8"]:>6.1%} {m["mrr"]:>7.4f} {m["query_ms"]:>8.0f}'
        )
    print()
    for name, m in results.items():
        if m["missed"]:
            print(f'  {name} 실패: {", ".join(m["missed"])}')

    # 백엔드 DDL이 vector(1536)이라 차원이 다르면 마이그레이션 변경이 필요하다
    print()
    for name, m in results.items():
        if m["dimension"] and m["dimension"] != 1536:
            print(f'  ! {name}는 {m["dimension"]}차원 - 백엔드 policy_chunks.embedding 변경 필요')


if __name__ == "__main__":
    main()
