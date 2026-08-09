import json
import logging
from pathlib import Path

import numpy as np

from app.repositories.base import ChunkHit
from app.services.bm25 import BM25Index

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"
EMBEDDINGS_DIR = PROJECT_ROOT / "data" / "embeddings"


# pgvector 연결 전까지 쓰는 로컬 구현체.
#
# pgvector의 <=> 연산자가 하는 일은 결국 정규화된 벡터 간의 내적(코사인 유사도)이라,
# numpy 행렬 곱으로 동일한 결과를 얻을 수 있다. 오히려 pgvector의 HNSW 인덱스는 근사 검색이고
# 이쪽은 전수 계산이라 정확한 최근접 이웃을 반환한다.
# 청크 수천 개 규모에서는 행렬 곱 한 번이 1ms 미만이라 성능도 문제되지 않는다.
class FileVectorRepository:
    def __init__(
        self,
        chunks_dir: Path | None = None,
        embeddings_dir: Path | None = None,
    ) -> None:
        self._chunks_dir = chunks_dir or CHUNKS_DIR
        self._embeddings_dir = embeddings_dir or EMBEDDINGS_DIR

        self._chunks: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self._matrix: np.ndarray | None = None
        self._bm25: BM25Index | None = None
        self._loaded = False

    # ── 색인 (파이프라인 A의 ④) ────────────────────────────────

    # analysis_result_id와 scope는 pgvector 저장소에서만 쓰인다.
    # 파일 저장소는 문서 stem으로 파일을 나누므로 받기만 하고 쓰지 않는다.
    def save(
        self,
        chunks: list[dict],
        embeddings: dict[str, list[float]],
        analysis_result_id: str | None = None,
        scope=None,
    ) -> None:
        if not chunks:
            return

        # 파일명은 문서 단위로 잡는다. chunk_id가 "{PDF stem}_0001" 형식이라
        # source_file에서 문서 식별자를 그대로 얻을 수 있다.
        stem = Path(chunks[0]["source_file"]).stem

        self._chunks_dir.mkdir(parents=True, exist_ok=True)
        self._embeddings_dir.mkdir(parents=True, exist_ok=True)

        with (self._chunks_dir / f"{stem}_chunks.json").open("w", encoding="utf-8") as f:
            json.dump(chunks, f, ensure_ascii=False, indent=2)

        with (self._embeddings_dir / f"{stem}_embeddings.json").open("w", encoding="utf-8") as f:
            json.dump(embeddings, f, ensure_ascii=False)

        # 새로 저장했으니 다음 검색 때 다시 읽도록 캐시를 비운다
        self._loaded = False

    # ── 질의 (파이프라인 B의 ⑥) ────────────────────────────────

    def search(
        self,
        query_vector: list[float],
        policy_id: str | None = None,
        top_k: int = 8,
    ) -> list[ChunkHit]:
        self._ensure_loaded()

        if self._matrix is None or not self._chunks:
            logger.warning("검색 가능한 임베딩이 없습니다. embed_chunks.py를 먼저 실행하세요.")
            return []

        query = np.asarray(query_vector, dtype=np.float32)
        norm = np.linalg.norm(query)
        if norm == 0:
            return []
        query = query / norm

        scores = self._matrix @ query  # (N,) — 전체 청크 유사도를 한 번에 계산

        # policy_id 필터는 점수 계산 후 마스킹으로 처리한다.
        # 청크 수가 적어 전수 계산 비용이 무시할 수준이고, 코드가 단순해진다.
        if policy_id is not None:
            mask = np.array(
                [c.get("policy_id") == policy_id for c in self._chunks],
                dtype=bool,
            )
            if not mask.any():
                logger.warning("policy_id=%s 에 해당하는 청크가 없습니다.", policy_id)
                return []
            scores = np.where(mask, scores, -np.inf)

        top_k = min(top_k, len(self._chunks))
        order = np.argpartition(-scores, top_k - 1)[:top_k]
        order = order[np.argsort(-scores[order])]

        return [
            self._to_hit(self._chunks[i], float(scores[i]), self._matrix[i].tolist())
            for i in order
            if np.isfinite(scores[i])
        ]

    def search_text(
        self,
        query: str,
        policy_id: str | None = None,
        top_k: int = 8,
    ) -> list[ChunkHit]:
        self._ensure_loaded()
        if not self._chunks or self._bm25 is None:
            return []

        scores = np.asarray(self._bm25.scores(query), dtype=np.float32)

        if policy_id is not None:
            mask = np.array([c.get("policy_id") == policy_id for c in self._chunks], dtype=bool)
            if not mask.any():
                return []
            scores = np.where(mask, scores, -np.inf)

        top_k = min(top_k, len(self._chunks))
        order = np.argpartition(-scores, top_k - 1)[:top_k]
        order = order[np.argsort(-scores[order])]

        # 점수가 0이면 질의 토큰이 하나도 안 걸린 문서라 근거로 볼 수 없다
        return [
            self._to_hit(self._chunks[i], float(scores[i]), self._matrix[i].tolist())
            for i in order
            if np.isfinite(scores[i]) and scores[i] > 0
        ]

    def get_by_ids(self, chunk_ids: list[str]) -> list[ChunkHit]:
        self._ensure_loaded()
        # score는 유사도 검색으로 얻은 값이 아니므로 0.0으로 둔다
        return [self._to_hit(self._by_id[cid], 0.0) for cid in chunk_ids if cid in self._by_id]

    # ── 내부 ─────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return

        chunks: list[dict] = []
        vectors: list[list[float]] = []

        for chunks_path in sorted(self._chunks_dir.glob("*_chunks.json")):
            stem = chunks_path.name.removesuffix("_chunks.json")
            emb_path = self._embeddings_dir / f"{stem}_embeddings.json"

            # 임베딩이 아직 없는 문서는 검색 대상에 넣을 수 없으므로 건너뛴다
            if not emb_path.exists():
                logger.info("임베딩 없음, 검색 대상에서 제외: %s", chunks_path.name)
                continue

            with chunks_path.open("r", encoding="utf-8") as f:
                doc_chunks = json.load(f)
            with emb_path.open("r", encoding="utf-8") as f:
                doc_embeddings = json.load(f)

            for chunk in doc_chunks:
                vector = doc_embeddings.get(chunk["chunk_id"])
                if vector is None:
                    continue
                chunks.append(self._with_local_scope(chunk, stem))
                vectors.append(vector)

        self._chunks = chunks
        self._by_id = {c["chunk_id"]: c for c in chunks}

        if vectors:
            matrix = np.asarray(vectors, dtype=np.float32)
            # 미리 정규화해두면 이후 검색은 내적 한 번으로 끝난다
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._matrix = matrix / norms
        else:
            self._matrix = None

        # BM25는 임베딩과 무관하게 원문만 있으면 만들 수 있다.
        # 임베딩할 때와 같은 텍스트(특약명 + 제목 + 본문)를 색인해야 두 검색의 대상이 일치한다.
        if chunks:
            self._bm25 = BM25Index(
                [
                    f'{c.get("clause_path") or ""}\n{c["section_title"]}\n{c["text"]}'
                    for c in chunks
                ]
            )
        else:
            self._bm25 = None

        self._loaded = True
        logger.info("로컬 벡터 인덱스 로드 완료: 청크 %d개", len(chunks))

    # CLI(scripts/run_pipeline.py)로 만든 청크에는 스코프 필드가 없다.
    # 로컬 개발에서 policy_id 필터를 그대로 시험할 수 있도록 문서 stem을 합성 식별자로 채운다.
    # API 경로로 들어온 청크는 이미 실제 값을 갖고 있으므로 건드리지 않는다.
    @staticmethod
    def _with_local_scope(chunk: dict, stem: str) -> dict:
        if chunk.get("policy_id"):
            return chunk
        return {
            **chunk,
            "user_id": chunk.get("user_id") or f"local-user-{stem}",
            "trip_id": chunk.get("trip_id") or f"local-trip-{stem}",
            "policy_id": chunk.get("policy_id") or stem,
            "document_id": chunk.get("document_id") or stem,
        }

    @staticmethod
    def _to_hit(chunk: dict, score: float, embedding: list[float] | None = None) -> ChunkHit:
        return ChunkHit(
            chunk_id=chunk["chunk_id"],
            document_id=chunk.get("document_id") or Path(chunk["source_file"]).stem,
            page_start=chunk["page_start"],
            page_end=chunk["page_end"],
            section_title=chunk["section_title"],
            coverage_type=chunk["coverage_type"],
            text=chunk["text"],
            matched_category=chunk.get("matched_category"),
            related_chunk_id=chunk.get("related_chunk_id"),
            score=score,
            embedding=embedding,
        )
