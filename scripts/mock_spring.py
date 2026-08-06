"""로컬 개발용 Mock Spring 서버.

app/clients/spring_client.py는 SPRING_BASE_URL이 비어 있으면 콜백을 건너뛰고 로그만 남긴다.
그래서 콜백 경로와 payload 형식이 실제로 맞는지 한 번도 검증되지 않은 상태가 된다.
이 서버를 띄우고 SPRING_BASE_URL을 여기로 돌리면, Spring 없이도 콜백을 확인할 수 있다.

    .venv/Scripts/python -m uvicorn scripts.mock_spring:app --port 8081

그 다음 .env에 SPRING_BASE_URL=http://127.0.0.1:8081 을 설정한다.
"""

import json
import logging

from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("mock_spring")

app = FastAPI(title="mock-spring", description="Python RAG 서버의 콜백 수신 확인용")

# 받은 콜백을 순서대로 보관한다. GET /received 로 조회할 수 있다.
received: list[dict] = []


def _record(kind: str, analysis_result_id: str, payload: dict) -> dict:
    entry = {"kind": kind, "analysisResultId": analysis_result_id, "payload": payload}
    received.append(entry)
    logger.info("%s 수신 (%s)\n%s", kind, analysis_result_id, json.dumps(payload, ensure_ascii=False, indent=2))
    return {"status": "ok"}


@app.post("/internal/analysis-results/{analysis_result_id}/complete")
async def complete(analysis_result_id: str, request: Request) -> dict:
    return _record("complete", analysis_result_id, await request.json())


@app.post("/internal/analysis-results/{analysis_result_id}/fail")
async def fail(analysis_result_id: str, request: Request) -> dict:
    return _record("fail", analysis_result_id, await request.json())


@app.get("/received")
def list_received() -> list[dict]:
    return received


# 등록되지 않은 경로로 콜백이 오면 조용히 404가 되는 대신 눈에 띄게 남긴다.
# 경로 오타를 잡아내는 것이 이 서버의 핵심 목적이다.
@app.api_route("/{path:path}", methods=["POST"])
async def catch_all(path: str, request: Request) -> dict:
    body = await request.body()
    logger.warning("등록되지 않은 경로로 POST 수신: /%s\n%s", path, body.decode("utf-8", "replace"))
    received.append({"kind": "unmatched", "path": f"/{path}", "payload": body.decode("utf-8", "replace")})
    return {"status": "unmatched"}
