from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "polight-ai-rag"
    api_prefix: str = "/internal"

    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"

    # DB 스키마 확정 전까지는 비워둠. 내일 연결 시 값 채우면 repository 구현체가 사용.
    database_url: str | None = None

    # Spring 콜백 대상 (완료/실패 알림). 아직 미확정이면 비워둔 채로 로컬 테스트.
    spring_base_url: str | None = None

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
