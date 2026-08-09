"""답변 생성 모델 비교 (Stage 4-A).

    python scripts/compare_answer_models.py
    python scripts/compare_answer_models.py --providers openai-mini claude-sonnet

검색 결과를 한 번만 뽑아 모든 모델에 같은 근거를 준다. 모델마다 따로 검색하면
검색 편차가 섞여 무엇 때문에 답이 달라졌는지 알 수 없다. 임베딩 비교와 다른 점이다.

자동 채점은 세 가지만 본다. 나머지는 사람이 읽어야 하므로 답변 전문을 파일로 남긴다.
  근거 인용   [근거 N] 표기가 있는가 (없으면 출처 UI가 성립하지 않는다)
  환각 방지   근거에 없는 내용을 지어내지 않는가 (unanswerable 문항으로 확인)
  응답 시간   챗봇은 사용자가 기다리는 화면이라 정확도만큼 중요하다
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.repositories import get_vector_repository  # noqa: E402
from app.services.answer_providers import PROVIDERS, generate  # noqa: E402
from app.services.embedding_service import embed_query  # noqa: E402
from app.services.prompt_builder import SYSTEM_PROMPT, SYSTEM_PROMPT_V1  # noqa: E402
from app.services.rag_service import (  # noqa: E402
    NO_EVIDENCE_ANSWER,
    attach_related_chunks,
    build_user_message,
    hybrid_search,
)
from app.services.reranker import mmr_select  # noqa: E402

QUESTIONS_PATH = PROJECT_ROOT / "data" / "eval" / "questions.json"
OUTPUT_DIR = PROJECT_ROOT / "data" / "eval"

# 근거 없이 답하는지 보려면 약관에 답이 없는 질문이 필요하다.
# 기존 평가셋은 전부 답이 있는 문항이라 이 축을 측정할 수 없다.
UNANSWERABLE = [
    "이 보험의 가입자 평균 연령이 어떻게 되나요?",
    "보험료를 신용카드 할부로 낼 수 있나요?",
]


# 호출 자체가 실패한 답변에 붙이는 표시. 채점에서 제외해야 한다.
FAILURE_MARK = "[호출 실패]"


def cites_evidence(answer: str) -> bool:
    """근거 번호를 표기했는가.

    닫는 괄호를 요구하면 안 된다. 모델마다 묶는 방식이 달라서
    Claude는 [근거 1][근거 2]로, Gemini는 [근거 1, 근거 2, 근거 3]으로 쓴다.
    후자를 놓치면 인용을 하고 있는데도 안 한 것으로 세게 된다(실측 92% -> 33%).
    """
    return bool(re.search(r"\[근거\s*\d+", answer))


def evidence_usage(answer: str, context: str) -> tuple[int, int]:
    """인용한 근거 개수와 제공된 근거 개수.

    인용률(달았나/안 달았나)은 6개 모델이 전부 100%라 변별이 안 됐다.
    실제 차이는 "몇 개를 썼나"에 있다. 근거 8개를 주면 저가 모델은 3개만 쓰고
    끝내는 반면, 상위 모델은 8개를 다 활용해 조건과 예외까지 챙긴다.
    보험 답변에서 조건을 빠뜨리는 것은 틀리는 것과 같으므로 이걸 지표로 삼는다.
    """
    provided = len(set(re.findall(r"\[근거 (\d+)\]", context)))
    used = len(set(re.findall(r"근거\s*(\d+)", answer)))
    # 없는 번호를 지어내면 제공 개수로 자른다
    return min(used, provided), provided


def refuses(answer: str) -> bool:
    """근거가 없을 때 모른다고 답하는지. 지어내면 False.

    표현 목록을 좁게 잡으면 모델이 아니라 채점기가 틀린다. 실제로 gpt-4o-mini는
    "확인할 수 없습니다"라고 제대로 거절했는데 "확인되지 않"만 찾다가 0%가 나왔다.
    '없다'는 뜻을 담은 어미 변화를 넓게 받아야 한다.
    """
    signals = (
        "확인할 수 없", "확인되지 않", "확인이 어렵", "찾을 수 없", "찾아볼 수 없",
        "나와 있지 않", "명시되어 있지 않", "언급이 없", "언급되어 있지 않",
        "정보가 없", "내용이 없", "알 수 없", "포함되어 있지 않",
    )
    return NO_EVIDENCE_ANSWER[:20] in answer or any(s in answer for s in signals)


def main() -> None:
    parser = argparse.ArgumentParser(description="답변 모델 비교")
    parser.add_argument("--providers", nargs="+", default=None,
                        help="생략하면 키가 있는 벤더 전부")
    parser.add_argument("--policy-id", default=None, help="pgvector 모드에서 대상 계약")
    parser.add_argument("--prompt", choices=["v1", "v2"], default="v1",
                        help="v2는 완전성과 형식을 지정한 개정판")
    parser.add_argument("--out", default=None, help="결과 파일 접미사")
    args = parser.parse_args()

    settings = get_settings()
    system_prompt = SYSTEM_PROMPT if args.prompt == "v2" else SYSTEM_PROMPT_V1
    suffix = args.out or args.prompt
    print(f"시스템 프롬프트: {args.prompt}\n")

    # 키가 없는 벤더는 조용히 건너뛴다. 세 벤더 키를 다 모으기 전에도
    # 가진 것끼리 비교를 시작할 수 있어야 실험이 막히지 않는다.
    names = args.providers or list(PROVIDERS)
    runnable, skipped = [], []
    for name in names:
        if getattr(settings, PROVIDERS[name].api_key_field, ""):
            runnable.append(name)
        else:
            skipped.append((name, PROVIDERS[name].api_key_field.upper()))

    for name, field in skipped:
        print(f"건너뜀: {name} ({field} 없음)")

    # 사전 점검. 키가 있어도 모델이 막혀 있을 수 있다. Gemini 무료 등급은 pro 계열에서
    # 429를, 구버전 모델에서 404를 낸다. 미리 1건씩 찔러 걸러내지 않으면 14문항을
    # 전부 실패로 태우고 나서야 알게 된다.
    alive = []
    for name in runnable:
        try:
            generate("짧게 답하세요.", "1+1은?", provider_name=name)
            alive.append(name)
        except Exception as e:
            reason = ("무료 한도 초과 (결제 등록 필요)" if "429" in str(e)
                      else "모델 사용 불가" if "404" in str(e) else str(e)[:70])
            print(f"건너뜀: {name} — {reason}")

    if not alive:
        print("\n실행 가능한 벤더가 없습니다. .env에 키를 넣어주세요.")
        return
    runnable = alive
    print(f"비교 대상: {', '.join(runnable)}\n")

    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    cases = [{"id": q["id"], "question": q["question"], "answerable": True} for q in questions]
    cases += [{"id": f"U{i:02d}", "question": q, "answerable": False}
              for i, q in enumerate(UNANSWERABLE, 1)]

    repository = get_vector_repository()

    # 검색은 문항당 한 번. 모든 모델이 같은 근거를 본다.
    print(f"[1] 검색 ({len(cases)}문항)")
    contexts = {}
    for case in cases:
        candidates = hybrid_search(
            repository, case["question"], embed_query(case["question"]),
            policy_id=args.policy_id,
            top_k=settings.top_k * settings.mmr_candidate_multiplier,
        )
        hits = mmr_select(candidates, top_k=settings.top_k, lambda_=settings.mmr_lambda)
        hits = attach_related_chunks(hits, repository)
        contexts[case["id"]] = build_user_message(case["question"], hits)
    print("  완료\n")

    print("[2] 답변 생성")
    results = {name: [] for name in runnable}
    for name in runnable:
        started = time.time()
        for case in cases:
            try:
                answer, elapsed = generate(system_prompt, contexts[case["id"]], provider_name=name)
            except Exception as e:
                answer, elapsed = f"{FAILURE_MARK} {e}", 0.0
            results[name].append({**case, "answer": answer, "seconds": elapsed})
        print(f"  {name:14} {len(cases)}문항 {time.time() - started:.0f}초")

    print("\n[3] 자동 채점")
    print(f"  {'모델':14} {'인용률':>7} {'근거활용':>9} {'환각방지':>8} {'평균응답':>9} {'실패':>6}")
    print("  " + "-" * 60)
    summary = []
    for name in runnable:
        rows = results[name]
        # 호출 실패는 채점에서 뺀다. 답변으로 세면 인용률이 깎이고, 실패 시각을
        # 0초로 기록하므로 평균 응답시간까지 실제보다 빠르게 나온다.
        ok = [r for r in rows if not r["answer"].startswith(FAILURE_MARK)]
        failed = len(rows) - len(ok)

        answerable = [r for r in ok if r["answerable"]]
        unanswerable = [r for r in ok if not r["answerable"]]

        cite_rate = (sum(cites_evidence(r["answer"]) for r in answerable) / len(answerable)
                     if answerable else 0.0)
        refuse_rate = (sum(refuses(r["answer"]) for r in unanswerable) / len(unanswerable)
                       if unanswerable else 0.0)
        avg = sum(r["seconds"] for r in ok) / len(ok) if ok else 0.0

        pairs = [evidence_usage(r["answer"], contexts[r["id"]]) for r in answerable]
        usage = (sum(u for u, _ in pairs) / sum(p for _, p in pairs)) if pairs else 0.0
        avg_used = sum(u for u, _ in pairs) / len(pairs) if pairs else 0.0

        mark = f"{failed}건" if failed else "-"
        print(f"  {name:14} {cite_rate:>6.0%} {usage:>8.0%} {refuse_rate:>8.0%} "
              f"{avg:>8.1f}초 {mark:>6}")
        summary.append({"provider": name, "citation_rate": round(cite_rate, 3),
                        "evidence_usage_rate": round(usage, 3),
                        "avg_evidence_used": round(avg_used, 1),
                        "refusal_rate": round(refuse_rate, 3), "avg_seconds": round(avg, 2),
                        "failed_calls": failed, "scored_calls": len(ok)})

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / f"answer_comparison_{suffix}.json").write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 사람이 읽고 판단할 부분. 자동 채점으로는 답변의 정확성과 어투를 볼 수 없다.
    lines = ["# 답변 모델 비교 — 사람 검토용\n"]
    for case in cases:
        mark = "" if case["answerable"] else "  ← 약관에 답이 없는 질문"
        lines.append(f"\n## {case['id']} {case['question']}{mark}\n")
        for name in runnable:
            row = next(r for r in results[name] if r["id"] == case["id"])
            lines.append(f"\n### {name} ({row['seconds']:.1f}초)\n\n{row['answer']}\n")
    (OUTPUT_DIR / f"answer_comparison_{suffix}.md").write_text("\n".join(lines), encoding="utf-8")

    print("\n저장:")
    print(f"  data/eval/answer_comparison_{suffix}.json  (점수)")
    print(f"  data/eval/answer_comparison_{suffix}.md    (답변 전문)")


if __name__ == "__main__":
    main()
