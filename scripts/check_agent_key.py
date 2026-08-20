"""증권 분석 에이전트를 부를 수 있는 키인지 확인한다.

콘솔에 키가 여러 개 보일 때 어느 것을 넣어야 하는지 판단하려고 만들었다.
키를 화면에 찍지 않으므로 그대로 붙여넣어도 남지 않는다.

인증이 되는 것과 에이전트가 보이는 것은 다르다. 실제로 약관 파싱에 쓰던 키로
잡을 만드니 404(Resource not found)가 났다. 인증은 통과하는데 그 계정에 그
에이전트가 없어서다. 그래서 두 가지를 나눠서 본다.

사용법
    # .env에 설정한 키로 확인
    python scripts/check_agent_key.py

    # 후보 키를 직접 넣어 확인 (환경변수로 넘기면 셸 기록에도 안 남는다)
    UPSTAGE_AGENT_API_KEY=up_xxx python scripts/check_agent_key.py

잡을 실제로 만들지 않으므로 분석 비용이 들지 않는다. 빈 PDF 한 장을 올려
에이전트 접근 권한만 확인하고 바로 지운다.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import fitz  # noqa: E402
import httpx  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.services.certificate_analyzer import agent_api_key  # noqa: E402


def main() -> None:
    settings = get_settings()
    key = agent_api_key()
    agent_id = settings.upstage_agent_id

    print(f"에이전트 ID : {agent_id or '(비어 있음)'}")
    print(f"사용 키     : {'UPSTAGE_AGENT_API_KEY' if settings.upstage_agent_api_key else 'UPSTAGE_API_KEY'}"
          f" ({len(key)}자, {key[:3]}...)")
    print()

    if not key or not agent_id:
        print("UPSTAGE_AGENT_ID와 키를 .env에 먼저 채우십시오.")
        return

    headers = {"Authorization": f"Bearer {key}"}

    # 1. 인증
    r = httpx.get(f"{settings.upstage_agent_base_url}/files", headers=headers, timeout=30)
    if r.status_code != 200:
        print(f"[1] 인증 실패 ({r.status_code}) - 키가 잘못됐습니다")
        print(f"    {r.text[:200]}")
        return
    print("[1] 인증 성공")

    # 2. 에이전트 접근. 빈 PDF를 올려 잡 생성만 시도한다.
    doc = fitz.open()
    doc.new_page()
    blank = doc.tobytes()
    doc.close()

    upload = httpx.post(
        f"{settings.upstage_agent_base_url}/files",
        headers=headers,
        files={"file": ("probe.pdf", blank, "application/pdf")},
        data={"purpose": "user_data"},
        timeout=60,
    )
    if upload.status_code >= 400:
        print(f"[2] 파일 업로드 실패 ({upload.status_code}): {upload.text[:200]}")
        return
    file_id = upload.json()["id"]

    try:
        body = {
            "model": agent_id,
            "include": ["last"],
            "input": [{"role": "user", "content": [{"type": "input_file", "file_id": file_id}]}],
        }
        if settings.upstage_agent_config_id:
            body["config_id"] = settings.upstage_agent_config_id

        job = httpx.post(
            f"{settings.upstage_agent_base_url}/responses",
            headers=headers, json=body, timeout=60,
        )

        if job.status_code == 404:
            print("[2] 에이전트를 찾을 수 없습니다 (404)")
            print("    인증은 되는데 이 계정에 해당 에이전트가 없습니다.")
            print("    Studio에서 에이전트를 만든 계정의 키를 넣으십시오.")
        elif job.status_code >= 400:
            print(f"[2] 잡 생성 실패 ({job.status_code}): {job.text[:200]}")
        else:
            print("[2] 에이전트 접근 성공 - 이 키를 쓰면 됩니다")
            # 확인만 하려는 것이므로 만든 잡은 바로 취소를 시도한다.
            job_id = job.json().get("id")
            httpx.post(
                f"{settings.upstage_agent_base_url}/responses/{job_id}/cancel",
                headers=headers, timeout=30,
            )
    finally:
        httpx.delete(f"{settings.upstage_agent_base_url}/files/{file_id}", headers=headers, timeout=30)
        print("\n확인용 파일 삭제 완료")


if __name__ == "__main__":
    main()
