from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "polight-ai-rag"
    api_prefix: str = "/internal"

    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"

    # 임베딩 벤더. app/services/embedding_providers.py의 PROVIDERS 키.
    # 비교 실험 결과 upstage-1536 채택 (MRR 0.7875 vs openai-small 0.5147).
    embedding_provider: str = "upstage-1536"

    # 답변 생성 벤더. app/services/answer_providers.py의 PROVIDERS 키.
    #
    # 6개 모델 x 프롬프트 2종을 14문항으로 비교해 gpt-4.1로 정했다.
    # 판단 기준은 근거 활용률(제공한 근거 중 실제 인용 비율)이다. 인용률과
    # 환각방지는 모든 모델이 100%라 변별이 되지 않았고, 실제 차이는
    # 조건과 예외를 얼마나 챙기느냐에 있었다.
    #
    #   gpt-4o-mini   39%   1000건 $0.95   TTFT 1.2초
    #   gpt-4.1       71%   1000건 $15.21  TTFT 0.7초   <- 채택
    #   claude-opus   73%   1000건 $74.55  TTFT 3.1초
    #
    # Opus와 2%p 차이인데 5배 싸고 TTFT가 4배 빠르다. 저가 모델은 프롬프트를
    # 고쳐도 39%에서 올라가지 않아, 이건 프롬프트가 아니라 모델 역량 문제다.
    answer_provider: str = "openai-41"

    # 아래 llm_model은 client를 직접 주입하는 옛 경로에서만 쓰인다.
    # 실제 모델 선택은 answer_provider가 한다.
    llm_model: str = "gpt-4o-mini"

    # 보장항목 추출용 모델. 답변용과 요구사항이 달라 따로 둔다.
    #   답변: 실시간, 지연시간 중요, 오류는 일회성
    #   추출: 비동기 배치, 지연 덜 중요, 오류가 DB에 저장돼 계속 노출됨
    # 그래서 추출 쪽은 느려도 정확한 모델을 쓰는 선택이 가능해야 한다.
    extraction_model: str = "gpt-4o-mini"

    # Claude / Gemini 비교 실험용. 없으면 해당 벤더만 건너뛴다.
    anthropic_api_key: str = ""
    google_api_key: str = ""

    # Upstage 문서 파싱. heading 계층과 요소 타입을 제공해
    # policy_chunks의 clause_path와 source_content_type(NOT NULL)을 채운다.
    upstage_api_key: str = ""

    # 임베딩 모델 비교용. 없으면 해당 벤더는 비교에서 자동으로 빠진다.
    qwen_api_key: str = ""

    # 검색 시 가져올 청크 수. related_chunk_id로 딸려오는 면책 조항은 이 수에 포함되지 않는다.
    top_k: int = 8

    # MMR 재순위 설정.
    # 약관은 같은 표준 조항이 특약마다 반복돼, 유사도 정렬만으로는 top_k가 중복본으로 채워진다.
    # 후보를 top_k의 배수만큼 넉넉히 뽑은 뒤 다양성을 고려해 top_k로 줄인다.
    mmr_candidate_multiplier: int = 4
    mmr_lambda: float = 0.6

    # DB 스키마 확정 전까지는 비워둠. 내일 연결 시 값 채우면 repository 구현체가 사용.
    database_url: str | None = None

    # Spring 콜백 대상 (완료/실패 알림). 아직 미확정이면 비워둔 채로 로컬 테스트.
    spring_base_url: str | None = None

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
