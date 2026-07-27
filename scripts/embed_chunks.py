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

# 임베딩할 텍스트: 제목 + 본문 합쳐서 의미 강화
def build_embed_text(chunk: dict) -> str:
    title = chunk.get("section_title") or ""
    text = chunk.get("text") or ""
    if title and title not in text:
        return f"{title}\n{text}"
    return text


def embed_batch(client: OpenAI, texts: list[str], model: str) -> list[list[float]]:
    """텍스트 배치를 임베딩 벡터로 변환한다."""
    response = client.embeddings.create(input=texts, model=model)
    # API 응답은 index 순서가 보장됨
    return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]


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
