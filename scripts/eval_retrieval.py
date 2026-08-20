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
from app.repositories.base import SearchScope  # noqa: E402
from app.repositories.file_repository import FileVectorRepository, _matches  # noqa: E402
from app.services.embedding_service import embed_query  # noqa: E402
from app.services.rag_service import hybrid_search  # noqa: E402
from app.services.query_rewriter import rewrite
from app.services.reranker import mmr_select  # noqa: E402

DEFAULT_QUESTIONS = PROJECT_ROOT / "data" / "eval" / "questions.json"
DEFAULT_CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"
DEFAULT_EMBEDDINGS_DIR = PROJECT_ROOT / "data" / "embeddings"

# 지표를 계산할 상위 개수
RECALL_AT = (1, 3, 5, 8)


def normalize(text: str) -> str:
    """공백 차이로 매칭이 실패하지 않도록 정규화한다 (chunk_policy와 같은 방식)."""
    return re.sub(r"\s+", "", text).lower()


def _git_commit() -> str | None:
    """측정 시점의 커밋. 어떤 코드로 낸 수치인지 남겨야 나중에 재현할 수 있다."""
    import subprocess

    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
    except Exception:
        return None


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


# gold 문구와 일치하는 청크가 몇 개인지 센다.
#
# 이 값이 문항 난이도다. 같은 표준 면책 조항이 여러 특약에 반복되면 정답 청크가
# 10개가 되고, 그중 아무거나 찾아도 정답이라 훨씬 쉽다. 난이도를 모르고
# 하나의 Recall로 합치면 실제 능력을 과대평가한다.
_density_cache: dict[str, int] = {}


def gold_density(repository: FileVectorRepository, gold: list[dict], policy_id: str) -> int:
    """gold 문구와 일치하는 청크가 검색 범위 안에 몇 개 있는지 센다.

    반드시 policy_id로 범위를 좁힌다. 저장소는 약관 7건을 모두 들고 있는데,
    검색은 한 건 안에서만 돈다. 전체에서 세면 표준 면책 조항이 7배로 부풀어
    난이도를 완전히 잘못 읽는다(임신·출산 면책: 실제 10건 -> 전체 69건).
    """
    key = f"{policy_id}|" + "|".join(sorted(g["contains"] for g in gold))
    if key not in _density_cache:
        repository._ensure_loaded()
        needles = [normalize(g["contains"]) for g in gold]
        scope = SearchScope(document_id=policy_id)
        _density_cache[key] = sum(
            1
            for chunk in repository._chunks
            if _matches(chunk, scope) and any(n in normalize(chunk["text"]) for n in needles)
        )
    return _density_cache[key]


def _report(label: str, group: list[tuple[dict, int | None]], top_k: int) -> dict:
    hit = sum(1 for _, r in group if r is not None and r <= top_k)
    mrr = sum(1 / r for _, r in group if r) / len(group)
    print(f"  {label:22} Recall@{top_k} {hit}/{len(group)} ({hit / len(group) * 100:3.0f}%)  MRR {mrr:.4f}")
    return {"n": len(group), "hit": hit, "recall": hit / len(group), "mrr": mrr}


# ── 평가 ─────────────────────────────────────────────────────


def load_clause_paths(repository: FileVectorRepository, policy_id: str) -> list[str]:
    """해당 약관에 실제로 존재하는 특약명 목록. 증권 담보명을 여기에 맞춰 잇는다."""
    repository._ensure_loaded()
    scope = SearchScope(document_id=policy_id)
    return sorted({
        c.get("clause_path")
        for c in repository._chunks
        if _matches(c, scope) and c.get("clause_path")
    })


def evaluate(
    questions: list[dict],
    repository: FileVectorRepository,
    policy_id: str,
    top_k: int,
    use_mmr: bool,
    use_hybrid: bool,
    multiplier: int,
    lambda_: float,
    clause_paths: tuple[str, ...] | None = None,
) -> dict:
    """평가를 돌리고 화면에 출력하면서, 같은 수치를 dict로도 돌려준다.

    dict를 돌려주는 이유: 개선 전후를 비교하려면 화면 출력을 눈으로 옮겨 적는 대신
    파일로 고정해야 한다. 손으로 옮긴 숫자는 재현되지 않고, 나중에 "그때 몇이었지"를
    다시 확인할 방법이 없다. --out으로 저장해두면 커밋에 남는다.
    """
    ranks: list[int | None] = []
    result: dict = {"per_question": [], "groups": {}}

    print(f'{"ID":5} {"유형":10} {"gold":>5} {"순위":>4}  질문')
    print("-" * 84)

    for item in questions:
        # 멀티턴 문항은 이력을 참고해 질문을 독립적으로 바꾼 뒤 검색한다.
        # 지시어만 남은 질문("그럼 얼마까지요?")은 그대로 검색하면 무엇에 대한
        # 질문인지 알 수 없어 엉뚱한 조항이 나온다. 실제 서비스 경로와 같게 맞춘다.
        history = item.get("history") or []
        query = rewrite(item["question"], history) if history else item["question"]

        vector = embed_query(query)
        pool = top_k * multiplier if use_mmr else top_k

        def run(scope: SearchScope) -> list[ChunkHit]:
            # 임베딩 모델을 비교할 때는 --no-hybrid로 BM25를 꺼야 한다.
            # 켜두면 키워드 점수가 섞여 모델 간 차이가 희석된다.
            if use_hybrid:
                return hybrid_search(repository, query, vector, scope, pool)
            return repository.search(vector, scope=scope, top_k=pool)

        base_scope = SearchScope(document_id=policy_id)
        candidates: list[ChunkHit] = []
        fell_back = False

        if clause_paths:
            candidates = run(SearchScope(document_id=policy_id, clause_paths=clause_paths))
            # rag_service와 같은 폴백 규칙을 쓴다. 평가와 서비스가 다르게 동작하면
            # 여기서 좋게 나온 수치가 실제로는 재현되지 않는다.
            if len(candidates) < top_k:
                candidates = []
                fell_back = True

        if not candidates:
            candidates = run(base_scope)

        hits = mmr_select(candidates, top_k=top_k, lambda_=lambda_) if use_mmr else candidates

        rank = first_hit_rank(hits, item["gold"])
        ranks.append(rank)

        rank_text = str(rank) if rank else "-"
        gold_n = gold_density(repository, item["gold"], policy_id)
        print(f'{item["id"]:5} {item.get("type", ""):10} {gold_n:>5} {rank_text:>4}  {item["question"][:50]}')
        result["per_question"].append(
            {
                "id": item["id"],
                "type": item.get("type"),
                "gold_density": gold_n,
                "rank": rank,
                "phrasing": item.get("phrasing"),
                "multiturn": bool(item.get("history")),
                "fell_back": fell_back,
            }
        )

    print("-" * 84)
    total = len(ranks)
    mode = f"MMR {'O' if use_mmr else 'X'} / hybrid {'O' if use_hybrid else 'X'}"
    print(f"\n질문 {total}개 / {mode} / top_k={top_k}")

    result["total"] = total
    result["mode"] = {"mmr": use_mmr, "hybrid": use_hybrid, "top_k": top_k}

    for k in RECALL_AT:
        if k > top_k:
            continue
        hit_count = sum(1 for r in ranks if r is not None and r <= k)
        print(f"  Recall@{k}: {hit_count}/{total} ({hit_count / total * 100:.1f}%)")
        result[f"recall@{k}"] = hit_count / total

    # MRR: 첫 정답의 역순위 평균. 정답을 못 찾으면 0으로 센다.
    mrr = sum(1 / r for r in ranks if r) / total
    print(f"  MRR      : {mrr:.4f}")
    result["mrr"] = mrr

    # 난이도로 나눠 본다.
    #
    # 같은 표준 면책 조항이 여러 특약에 반복되면 정답 청크가 수십 개가 된다.
    # 그중 아무거나 찾아도 정답이라 1개짜리 문항과는 난이도가 전혀 다르다.
    # 합쳐서 하나의 Recall로 보면 반복 조항이 점수를 끌어올려 실력을 과대평가한다.
    print()
    for label, keep in (
        ("고유 조항 (gold 1~2개)", lambda n: n <= 2),
        ("반복 조항 (gold 3개+)", lambda n: n >= 3),
    ):
        group = [
            (q, r)
            for q, r in zip(questions, ranks)
            if keep(gold_density(repository, q["gold"], policy_id))
        ]
        if group:
            result["groups"][label] = _report(label, group, top_k)

    # 표현에 따른 차이. 같은 조항을 약관 용어와 일상 표현으로 물었을 때의 격차다.
    # 하이브리드 검색이 어휘 격차를 실제로 메우는지가 여기서 드러난다.
    if any(q.get("pair_id") for q in questions):
        print()
        for phrasing, label in (("formal", "약관 용어"), ("casual", "일상 표현")):
            group = [
                (q, r)
                for q, r in zip(questions, ranks)
                if q.get("pair_id") and q.get("phrasing") == phrasing
            ]
            if group:
                result["groups"][label] = _report(label, group, top_k)

    multi = [(q, r) for q, r in zip(questions, ranks) if q.get("history")]
    if multi:
        print()
        result["groups"]["멀티턴 (재작성 후)"] = _report("멀티턴 (재작성 후)", multi, top_k)

    missed = [q["id"] for q, r in zip(questions, ranks) if r is None]
    result["missed"] = missed
    if missed:
        print(f"\n  실패한 질문: {', '.join(missed)}")

    # 필터가 실제로 몇 문항에 적용됐는지. 폴백이 대부분이면 "필터를 켰다"는 말이
    # 무의미하므로 이 수치 없이 Recall만 비교하면 결론을 잘못 읽는다.
    if clause_paths:
        fell = sum(1 for q in result["per_question"] if q["fell_back"])
        applied = total - fell
        result["filter"] = {"clause_paths": list(clause_paths), "applied": applied, "fell_back": fell}
        print(f"\n  특약 필터 적용 {applied}/{total}문항, 폴백 {fell}문항")

    return result


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
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="측정 결과를 JSON으로 저장한다. 개선 전후를 비교하려면 baseline을 파일로 고정해야 한다",
    )
    parser.add_argument("--note", type=str, default="", help="--out에 함께 남길 메모 (무엇을 바꾼 실행인지)")
    parser.add_argument(
        "--certificate",
        type=str,
        default=None,
        help="가상 증권 id (minimal/standard/premium). 그 증권의 담보로 특약 필터를 켠다",
    )

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

    # 가상 증권으로 특약 필터를 켠다.
    #
    # 실제 증권이 없어도 필터 부품만 따로 잴 수 있다. 필터가 받는 것은 특약명 목록이고,
    # 증권은 그 목록을 만들어주는 수단일 뿐이기 때문이다. 다만 질문마다 정답이 든 특약을
    # 집어주면 부정행위가 되므로, 실제 증권처럼 담보 세트를 통째로 준다.
    clause_paths = None
    if args.certificate:
        from app.services.clause_matcher import match_coverages  # noqa: E402

        with (PROJECT_ROOT / "data" / "eval" / "virtual_certificates.json").open(encoding="utf-8") as f:
            certs = json.load(f)["certificates"]
        cert = next((c for c in certs if c["id"] == args.certificate), None)
        if cert is None:
            raise SystemExit(f"가상 증권을 찾을 수 없습니다: {args.certificate}")

        available = load_clause_paths(repository, args.policy_id)
        clause_paths, report = match_coverages(cert["coverages"], available)
        print(f'증권: {cert["label"]} - {cert["description"]}')
        print(f'  담보 {len(cert["coverages"])}개 중 {len(report["matched"])}개 매칭 '
              f'({report["match_ratio"]:.0%}) -> {report["reason"]}')
        if report["unmatched"]:
            print(f'  못 이은 담보: {", ".join(report["unmatched"])}')
        print()

    result = evaluate(
        questions=questions,
        repository=repository,
        policy_id=args.policy_id,
        top_k=args.top_k,
        use_mmr=not args.no_mmr,
        use_hybrid=not args.no_hybrid,
        multiplier=settings.mmr_candidate_multiplier,
        lambda_=settings.mmr_lambda,
        clause_paths=clause_paths,
    )

    if args.out:
        # 어떤 조건에서 나온 수치인지 함께 남긴다. 수치만 남기면 몇 달 뒤에
        # "이게 어떤 설정이었지"를 알 수 없어 비교 자체가 불가능해진다.
        result["meta"] = {
            "note": args.note,
            "questions": str(questions_path),
            "chunks_dir": args.chunks_dir,
            "embeddings_dir": args.embeddings_dir,
            "policy_id": args.policy_id,
            "embedding_provider": settings.embedding_provider,
            "git_commit": _git_commit(),
        }
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n측정 결과 저장: {out_path}")


if __name__ == "__main__":
    main()
