import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.services.embedding_providers import build_client, embed, get_provider  # noqa: E402

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "embeddings"

# 한 번에 API에 보낼 chunk 수 (OpenAI 배치 상한: 2048)
BATCH_SIZE = 50

# 임베딩할 텍스트: 상위 특약명 + 조항 제목 + 본문
#
# clause_path(상위 특약명)를 앞에 붙이는 이유: 조항 제목은 "제1조(보험금의 지급사유)"처럼
# 특약마다 똑같이 반복돼 변별력이 없다. 상위 특약명이 없으면 "항공기 지연" 질의가
# 수십 개의 동명 조항과 구분되지 않아 검색에서 밀린다.
# 제목이 본문에 이미 포함돼 있으면 중복 방지를 위해 본문만 사용한다.
def build_embed_text(chunk: dict) -> str:
    clause_path = chunk.get("clause_path") or ""
    title = chunk.get("section_title") or ""
    text = chunk.get("text") or ""

    parts = []
    if clause_path and clause_path not in text:
        parts.append(clause_path)
    if title and title not in text:
        parts.append(title)
    parts.append(text)

    return "\n".join(parts)


# 텍스트 배치 하나를 임베딩한다.
# 벤더는 settings.embedding_provider를 따른다. Upstage는 질의/문서 모델이 달라서
# 여기서는 반드시 문서 쪽(is_query=False)으로 호출해야 한다.
def embed_batch(client, texts: list[str], model: str | None = None) -> list[list[float]]:
    """텍스트 배치를 임베딩 벡터로 변환한다."""
    provider = get_provider(get_settings().embedding_provider)
    return embed(provider, texts, is_query=False, client=client)


# 청크 파일 하나를 처리해 임베딩 결과 파일을 생성/이어서 갱신
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

    # 임베딩 안 된 chunk만 추출 -> 재실행해도 비용/시간 중복되지 않음
    pending = [c for c in chunks if c["chunk_id"] not in existing]

    if not pending:
        print(f"  이미 완료됨: {chunks_path.name} ({len(existing)}개)")
        return existing

    print(f"  {chunks_path.name}: 총 {len(chunks)}개 중 {len(pending)}개 임베딩")

    results = dict(existing)

    # BATCH_SIZE 단위로 잘라서 API 호출
    for i in tqdm(range(0, len(pending), BATCH_SIZE), desc=f"  embedding"):
        batch = pending[i : i + BATCH_SIZE]
        texts = [build_embed_text(c) for c in batch]

        # 오류 시 3초 대기 후 1회 재시도
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


# CLI 진입점: chunks 디렉토리(또는 --file) 전체를 대상으로 임베딩 실행
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

    # 키가 없으면 배치 처리 도중이 아니라 시작 시점에 바로 실패시킴
    provider = get_provider(get_settings().embedding_provider)
    client = build_client(provider)
    print(f"임베딩 벤더: {provider.name} (문서 모델 {provider.doc_model})")
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
