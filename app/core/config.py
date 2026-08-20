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

    # 보장항목 추출용 벤더. 답변용과 요구사항이 정반대라 따로 둔다.
    #   답변: 실시간, 지연시간 중요, 오류는 그 대화 한 번으로 끝
    #   추출: 비동기 배치, 지연 덜 중요, 오류가 DB에 저장돼 계속 노출됨
    #
    # 7개 카테고리로 비교해 claude-opus로 정했다.
    #
    #   모델          면책   환각   JSON
    #   gpt-4o-mini     6건    0건   100%
    #   gpt-4.1-mini   29건   23건   100%   <- 많이 뽑았지만 인용문이 원문에 없음
    #   gpt-4.1        28건    3건    86%
    #   claude-opus    69건    2건   100%   <- 채택
    #
    # 면책조건을 11배 많이 뽑으면서 환각 경고는 2건뿐이다. exclusion_conditions는
    # 여러 행을 기대하는 테이블인데 gpt-4o-mini로는 카테고리당 1건도 못 채웠다.
    #
    # 한도 해석도 유일하게 맞았다. 휴대품손해의 20만원은 "1개 또는 1조당" 세부
    # 한도인데 OpenAI 계열은 전체 한도(limitAmount)에 넣었다. 화면에 "최대 20만원"으로
    # 뜨면 사용자가 총액을 오해한다. claude-opus만 전체 한도를 비우고 세부 한도로 잡았다.
    #
    # 다만 MVP 단계에서는 gpt-4.1을 쓴다. claude-opus가 문서당 $1.09로 비싸고,
    # 지금 급한 것은 추출 품질이 아니라 백엔드 연동을 끝내는 일이기 때문이다.
    #
    #   claude-opus  면책 69건(조항 내용)  $1.093/문서
    #   gpt-4.1      면책 28건(조항 제목)  $0.286/문서   <- 현재
    #   gpt-4o-mini  면책  6건            $0.018/문서
    #
    # gpt-4o-mini까지 내리지 않는 이유는 한도를 틀리게 넣기 때문이다. 휴대품손해의
    # "1개당 20만원"을 전체 한도로 올려 화면에 "최대 200,000원"으로 띄운다.
    # gpt-4.1은 면책이 조항 제목 수준이라 아쉬울 뿐, 틀린 값을 만들지는 않는다.
    #
    # 실서비스 전에 claude-opus로 되돌린다. 이 한 줄만 바꾸면 되고 코드 변경은 없다.
    # 단, 이미 저장된 결과는 바뀌지 않으므로 해당 문서를 재분석해야 한다.
    extraction_provider: str = "openai-41"

    # 아래 extraction_model은 client를 직접 주입하는 옛 경로에서만 쓰인다.
    extraction_model: str = "gpt-4o-mini"

    # Claude / Gemini 비교 실험용. 없으면 해당 벤더만 건너뛴다.
    anthropic_api_key: str = ""
    google_api_key: str = ""

    # Upstage 문서 파싱. heading 계층과 요소 타입을 제공해
    # policy_chunks의 clause_path와 source_content_type(NOT NULL)을 채운다.
    #
    # 증권 분석(Studio Agent)도 같은 키를 쓴다. 도메인이 같고 제품만 다르다.
    upstage_api_key: str = ""

    # 증권 분석 에이전트. Upstage Studio에서 만든 파싱->분류->추출 다단계 파이프라인이다.
    #
    # 약관 파싱(/v1/document-digitization)과 다른 API다. 이쪽은 비동기 잡이라
    # 업로드 -> 생성 -> 폴링 순서로 돈다. 스키마는 에이전트에 저장돼 있어 우리가 들고
    # 있을 필요가 없다. 비어 있으면 증권 분석 경로가 꺼진다.
    upstage_agent_id: str = ""
    upstage_agent_base_url: str = "https://api.upstage.ai/v2"

    # 에이전트 호출용 키. 비우면 UPSTAGE_API_KEY를 쓴다.
    #
    # 보통은 한 키로 모든 제품을 쓰지만, 그건 같은 계정 안에서만이다. 실제로 약관
    # 파싱에 쓰던 키로 에이전트를 부르니 404(Resource not found)가 났다. 인증은
    # 통과하는데 그 계정에 그 에이전트가 없어서다. 계정이 갈려 있을 때 키를 통째로
    # 바꾸면 약관 파싱·임베딩 과금까지 옮겨가므로, 증권만 따로 지정할 수 있게 둔다.
    upstage_agent_api_key: str = ""

    # 에이전트 설정 버전. 비우면 최신을 쓴다.
    #
    # 기본을 최신으로 둔 이유: Studio에서 에이전트를 고치는 중이라, 고정해두면
    # 개선한 것이 서버에 반영되지 않아 원인을 찾기 어렵다. 다만 평가 수치를 재현해야
    # 할 때는 버전을 박아야 한다. Studio를 건드리면 코드 변경 없이 결과가 바뀐다.
    upstage_agent_config_id: str = ""

    # 폴링 설정. 증권은 1~2페이지라 대개 수십 초 안에 끝난다.
    certificate_poll_interval: float = 2.0
    certificate_timeout: float = 180.0

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

    # DB 연결 풀 크기.
    #
    # 하이브리드 검색은 한 질문에 연결을 3번 쓴다(벡터·키워드·면책 조회).
    # 동시 대화 5건을 기준으로 여유를 둬 10으로 잡았다. RDS의 max_connections를
    # 넘기면 연결 자체가 거부되므로 무작정 키우면 안 된다.
    db_pool_min: int = 1
    db_pool_max: int = 10

    # Spring 콜백 대상 (완료/실패 알림). 아직 미확정이면 비워둔 채로 로컬 테스트.
    spring_base_url: str | None = None

    # 콜백을 보낼 경로. Spring 쪽에 아직 /internal 엔드포인트가 없어 우리가 정한 값이라,
    # 그쪽이 다른 경로로 만들면 404가 나고 상태가 PROCESSING에 고착된다.
    # 설정으로 빼두면 이미지를 다시 만들지 않고 환경변수만 바꿔 맞출 수 있다.
    # {id} 자리에 analysis_result_id가 들어간다.
    callback_complete_path: str = "/internal/analysis-results/{id}/complete"
    callback_fail_path: str = "/internal/analysis-results/{id}/fail"

    # Spring과 공유하는 내부 API 시크릿. 양방향으로 같은 키를 쓴다.
    #   Spring -> Python  요청 헤더를 검증
    #   Python -> Spring  콜백 헤더에 실어 보냄
    #
    # 비어 있으면 인증 없이 통과시킨다. 로컬 개발 편의를 위한 것이고,
    # 배포 환경(SPRING_BASE_URL이 있는 상태)에서는 기동을 막는다.
    # 생성: openssl rand -base64 32
    internal_api_key: str = ""

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
