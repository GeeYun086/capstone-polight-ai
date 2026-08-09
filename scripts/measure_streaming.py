"""스트리밍 체감 속도와 실제 비용 측정 (Stage 4-A 보조).

    python scripts/measure_streaming.py

총 응답시간만 보면 모델 선택을 잘못하게 된다. 챗봇에서 사용자가 느끼는 건
'답이 다 나올 때까지'가 아니라 '첫 글자가 뜰 때까지'(TTFT)이기 때문이다.
스트리밍을 켜면 총 14초짜리 답변도 1~2초 만에 읽기 시작할 수 있다.

단, Claude는 thinking이 끝나야 본문이 나오기 시작한다. 그래서 thinking을 켠 채
스트리밍하면 TTFT가 생각만큼 짧지 않을 수 있다. 이걸 확인하는 게 이 스크립트의 목적이다.

비용도 함께 낸다. 토큰 수를 추정하지 않고 API가 돌려주는 usage를 그대로 쓴다.
"""

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import json  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.repositories import get_vector_repository  # noqa: E402
from app.services.answer_providers import PROVIDERS  # noqa: E402
from app.services.embedding_service import embed_query  # noqa: E402
from app.services.prompt_builder import SYSTEM_PROMPT  # noqa: E402
from app.services.rag_service import (  # noqa: E402
    attach_related_chunks,
    build_user_message,
    hybrid_search,
)
from app.services.reranker import mmr_select  # noqa: E402

# 100만 토큰당 단가 (입력, 출력). 공식 가격표 기준.
# 출력 단가가 입력보다 5배 비싸므로, 답변이 긴 모델은 비용이 그만큼 더 든다.
PRICING = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o": (2.50, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
}

QUESTIONS = [
    "보험금을 청구하려면 어떤 서류가 필요한가요?",
    "항공편이 5시간 이상 지연되면 보상되나요?",
    "임신이나 출산으로 인한 치료비도 보상되나요?",
]


def stream_openai(model: str, system: str, user: str) -> dict:
    from openai import OpenAI

    client = OpenAI(api_key=get_settings().openai_api_key)
    started = time.time()
    ttft = None
    text = []
    usage = None

    stream = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0,
        stream=True,
        # 스트리밍에서는 usage가 기본으로 오지 않는다. 명시해야 마지막 청크에 실린다.
        stream_options={"include_usage": True},
    )
    for chunk in stream:
        if chunk.usage:
            usage = chunk.usage
        if chunk.choices and chunk.choices[0].delta.content:
            if ttft is None:
                ttft = time.time() - started
            text.append(chunk.choices[0].delta.content)

    return {
        "ttft": ttft, "total": time.time() - started, "text": "".join(text),
        "in_tokens": usage.prompt_tokens if usage else 0,
        "out_tokens": usage.completion_tokens if usage else 0,
    }


def stream_anthropic(model: str, system: str, user: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
    started = time.time()
    ttft = None
    text = []

    # text_stream은 본문 텍스트만 흘려준다. thinking 블록은 여기 안 실리므로,
    # 여기서 잰 TTFT가 곧 '사용자가 첫 글자를 보기까지'의 시간이다.
    with client.messages.stream(
        model=model, max_tokens=2000, system=system,
        thinking={"type": "adaptive"}, output_config={"effort": "low"},
        messages=[{"role": "user", "content": user}],
    ) as stream:
        for delta in stream.text_stream:
            if ttft is None:
                ttft = time.time() - started
            text.append(delta)
        final = stream.get_final_message()

    return {
        "ttft": ttft, "total": time.time() - started, "text": "".join(text),
        "in_tokens": final.usage.input_tokens,
        # thinking 토큰도 출력으로 과금된다. output_tokens에 이미 포함돼 있다.
        "out_tokens": final.usage.output_tokens,
    }


CANDIDATES = [
    ("openai-mini", "gpt-4o-mini", stream_openai),
    ("openai-41mini", "gpt-4.1-mini", stream_openai),
    ("openai-41", "gpt-4.1", stream_openai),
    ("claude-opus", "claude-opus-5", stream_anthropic),
]


def main() -> None:
    settings = get_settings()
    repository = get_vector_repository()

    print(f"[1] 검색 ({len(QUESTIONS)}문항)")
    contexts = []
    for q in QUESTIONS:
        candidates = hybrid_search(
            repository, q, embed_query(q), policy_id=None,
            top_k=settings.top_k * settings.mmr_candidate_multiplier,
        )
        hits = mmr_select(candidates, top_k=settings.top_k, lambda_=settings.mmr_lambda)
        contexts.append(build_user_message(q, attach_related_chunks(hits, repository)))
    print("  완료\n")

    print("[2] 스트리밍 측정")
    rows = []
    for label, model, fn in CANDIDATES:
        runs = []
        for ctx in contexts:
            try:
                runs.append(fn(model, SYSTEM_PROMPT, ctx))
            except Exception as e:
                print(f"  {label:14} 실패: {str(e)[:80]}")
                break
        if not runs:
            continue

        n = len(runs)
        ttft = sum(r["ttft"] or 0 for r in runs) / n
        total = sum(r["total"] for r in runs) / n
        out_tok = sum(r["out_tokens"] for r in runs) / n
        in_tok = sum(r["in_tokens"] for r in runs) / n
        chars = sum(len(r["text"]) for r in runs) / n
        # 첫 글자가 뜬 뒤의 생성 속도. 이게 느리면 글이 끊겨 보인다.
        tps = out_tok / (total - (ttft or 0)) if total > (ttft or 0) else 0

        pin, pout = PRICING[model]
        per_1k = (in_tok * pin + out_tok * pout) / 1000  # 질문 1,000건 비용($)

        rows.append((label, ttft, total, tps, chars, in_tok, out_tok, per_1k))
        print(f"  {label:14} TTFT {ttft:4.1f}초  총 {total:5.1f}초  "
              f"{tps:5.1f} tok/s  {chars:4.0f}자")

    print(f"\n[3] 정리\n")
    print(f"  {'모델':15}{'TTFT':>7}{'총시간':>8}{'생성속도':>10}{'답변길이':>9}"
          f"{'출력토큰':>9}{'1000건 비용':>12}")
    print("  " + "-" * 71)
    for label, ttft, total, tps, chars, in_tok, out_tok, per_1k in rows:
        print(f"  {label:15}{ttft:6.1f}초{total:7.1f}초{tps:8.0f}tok/s"
              f"{chars:7.0f}자{out_tok:8.0f}{per_1k:>10.2f}$")

    base = next((r for r in rows if r[0] == "openai-mini"), None)
    if base:
        print(f"\n  gpt-4o-mini 대비 비용")
        for label, *_, per_1k in rows:
            print(f"    {label:15} {per_1k / base[7]:5.1f}배")

    out = PROJECT_ROOT / "data" / "eval" / "streaming_measurement.json"
    out.write_text(json.dumps([
        {"provider": r[0], "ttft_sec": round(r[1], 2), "total_sec": round(r[2], 2),
         "tokens_per_sec": round(r[3], 1), "chars": round(r[4]),
         "in_tokens": round(r[5]), "out_tokens": round(r[6]),
         "usd_per_1000_questions": round(r[7], 2)} for r in rows
    ], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: data/eval/streaming_measurement.json")


if __name__ == "__main__":
    main()
