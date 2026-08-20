# AI/RAG 서버 이미지.
#
# 배포는 Spring과 같은 EC2에 한다. 분석 1건의 CPU를 실측해보니 평균 0.1%로,
# 대부분이 Upstage·OpenAI API 응답 대기라 경합이 없었다. 자세한 근거는 DEPLOY.md.

FROM python:3.14-slim

# pymupdf가 런타임에 필요로 하는 최소 라이브러리만 넣는다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성을 먼저 복사해 레이어 캐시를 살린다.
# 소스만 바뀌면 pip install을 다시 하지 않는다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY config ./config

# 분석 파이프라인이 쓰는 작업 디렉터리. 파싱 캐시와 청크가 여기 쌓인다.
RUN mkdir -p data/raw_pdfs data/parsed_results data/chunks data/embeddings

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

EXPOSE 8000

# 분석은 BackgroundTasks로 응답 후에도 계속 돌기 때문에,
# 요청이 끝나면 컨테이너를 정리하는 서버리스 환경(예: 설정 없는 Cloud Run)에는
# 그대로 올리면 안 된다. EC2에 상주 프로세스로 띄우는 것을 전제로 한다.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
