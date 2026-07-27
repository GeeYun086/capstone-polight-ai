"""
[파일 개요 - 코드리뷰]
chunk_policy.py가 만든 청크(*_chunks.json)들을 OpenAI 임베딩 API로 벡터화하는 스크립트.
결과는 {chunk_id: [벡터...]} 형태의 딕셔너리로 저장되어(*_embeddings.json),
이후 벡터 검색(유사도 기반 조항 검색)에 사용된다.

특징:
- 배치(BATCH_SIZE=50개씩) 단위로 API를 호출해 비용/속도를 절충.
- 이미 임베딩된 chunk_id는 건너뛰어서, 중간에 중단돼도 이어서 실행 가능(idempotent).
- 배치 하나 끝날 때마다 즉시 파일에 저장(중단 내성).

파이프라인 순서: extract_pdf_text.py -> chunk_policy.py -> embed_chunks.py
"""

import argparse
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from tqdm import tqdm

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "embeddings"

# 한 번에 API에 보낼 chunk 수 (OpenAI 배치 상한: 2048)
BATCH_SIZE = 50

# [코드리뷰] build_embed_text
# 역할: 청크 하나를 임베딩할 때 실제로 API에 보낼 텍스트를 조립한다.
# 동작: 섹션 제목(section_title)이 본문(text)에 이미 포함돼 있지 않으면 "제목\n본문"
#   형태로 앞에 붙여서 반환. 이미 포함돼 있으면 중복을 피하기 위해 본문만 반환.
# 왜 필요한가: 제목만으로도 검색 질의와의 의미적 연관성이 커지는 경우가 많아서
#   (예: "여행중단" 이라는 질의가 본문보다 제목과 더 잘 매칭), 임베딩 벡터에 제목
#   정보를 포함시켜 검색 정확도를 높이려는 목적.
def build_embed_text(chunk: dict) -> str:
    title = chunk.get("section_title") or ""
    text = chunk.get("text") or ""
    if title and title not in text:
        return f"{title}\n{text}"
    return text


# [코드리뷰] embed_batch
# 역할: 텍스트 리스트를 한 번의 API 호출로 임베딩 벡터 리스트로 변환한다.
# 주의점: OpenAI 응답의 response.data는 순서가 보장되지 않을 수 있어(문서상으로는
#   index 순서 그대로 오지만, 방어적으로) item.index 기준으로 정렬 후 반환해서
#   입력 texts 리스트와 출력 벡터 리스트의 순서가 반드시 1:1로 맞도록 보장한다.
def embed_batch(client: OpenAI, texts: list[str], model: str) -> list[list[float]]:
    """텍스트 배치를 임베딩 벡터로 변환한다."""
    response = client.embeddings.create(input=texts, model=model)
    # API 응답은 index 순서가 보장됨
    return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]


# [코드리뷰] embed_chunks_file  (핵심 로직)
# 역할: 청크 파일 하나를 처리해서 임베딩 결과 파일을 만든다.
# 동작 상세:
#   1. 출력 파일(*_embeddings.json)이 이미 존재하면 로드해서 기존 결과(existing)로 사용.
#   2. chunks 중 existing에 없는 chunk_id만 pending으로 추려서, 이미 처리한 건 재요청하지 않음
#      (재실행해도 비용이 중복되지 않는 "재개 가능한(resumable)" 설계).
#   3. pending을 BATCH_SIZE 단위로 잘라 embed_batch 호출. API 오류 시 3초 대기 후 1회 재시도.
#   4. 배치가 끝날 때마다 results를 즉시 파일에 덮어써서, 중간에 프로세스가 죽어도
#      그동안 처리된 배치는 보존되도록 함.
# 인터뷰 포인트: "왜 배치마다 저장하나요?" -> 대량의 청크를 처리하다 네트워크 오류나
#   Ctrl+C로 중단될 경우, 처음부터 다시 돌리면 비용과 시간이 낭비되므로 체크포인트를
#   남기는 방식으로 설계함.
def embed_chunks_file(
    chunks_path: Path,
    output_path: Path,
    client: OpenAI,
    model: str,
) -> dict:
    with chunks_path.open("r", encoding="utf-8") as f:
        chunks = json.load(f)

    # 이미 처리된 파일이면 기존 결과 로드 후 미완료분만 처리
    existing: dict[str, list[float]] = {}
    if output_path.exists():
        with output_path.open("r", encoding="utf-8") as f:
            existing = json.load(f)

    # 임베딩 안 된 chunk만 추출
    pending = [c for c in chunks if c["chunk_id"] not in existing]

    if not pending:
        print(f"  이미 완료됨: {chunks_path.name} ({len(existing)}개)")
        return existing

    print(f"  {chunks_path.name}: 총 {len(chunks)}개 중 {len(pending)}개 임베딩")

    results = dict(existing)

    for i in tqdm(range(0, len(pending), BATCH_SIZE), desc=f"  embedding"):
        batch = pending[i : i + BATCH_SIZE]
        texts = [build_embed_text(c) for c in batch]

        try:
            vectors = embed_batch(client, texts, model)
        except Exception as e:
            print(f"\n  API 오류 (batch {i}): {e}")
            print("  3초 후 재시도...")
            time.sleep(3)
            vectors = embed_batch(client, texts, model)

        for chunk, vector in zip(batch, vectors):
            results[chunk["chunk_id"]] = vector

        # 배치마다 중간 저장 (중단돼도 재시작 가능)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False)

    return results


# [코드리뷰] main (CLI 진입점)
# 역할: chunks 디렉토리(또는 --file로 특정 파일)를 대상으로 embed_chunks_file을 반복 호출.
# 주의: OPENAI_API_KEY가 .env에 없으면 즉시 예외를 던져 조기에 실패하도록 함
#   (배치 처리 도중이 아니라 시작 시점에 검증).
# 실행 예: python scripts/embed_chunks.py --file example_chunks.json
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate embeddings for all chunk JSON files."
    )
    parser.add_argument(
        "--chunks-dir",
        type=str,
        default=str(DEFAULT_CHUNKS_DIR),
        help="청크 JSON이 있는 디렉토리",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR),
        help="임베딩 결과를 저장할 디렉토리",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help="특정 파일만 처리. 예: kakao_travel_2025_chunks.json",
    )

    args = parser.parse_args()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(".env에 OPENAI_API_KEY가 설정되지 않았습니다.")

    client = OpenAI(api_key=api_key)
    chunks_dir = Path(args.chunks_dir)
    output_dir = Path(args.output_dir)

    if args.file:
        chunk_files = [chunks_dir / args.file]
    else:
        chunk_files = sorted(chunks_dir.glob("*_chunks.json"))

    if not chunk_files:
        raise FileNotFoundError(f"청크 파일이 없습니다: {chunks_dir}")

    total_embedded = 0

    for chunks_path in chunk_files:
        output_path = output_dir / chunks_path.name.replace("_chunks.json", "_embeddings.json")
        results = embed_chunks_file(chunks_path, output_path, client, args.model)
        total_embedded += len(results)

    print(f"\n완료: 총 {total_embedded}개 chunk 임베딩 저장됨")
    print(f"저장 위치: {output_dir}")


if __name__ == "__main__":
    main()
