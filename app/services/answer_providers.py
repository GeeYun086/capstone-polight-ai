"""답변 생성 모델 벤더 추상화.

임베딩과 달리 세 벤더의 API 모양이 서로 다르다. Anthropic은 system을 별도 인자로
받고 응답이 content 블록 배열이며, Gemini는 system_instruction을 config에 넣는다.
OpenAI 호환 엔드포인트로 억지로 맞추면 각 벤더의 고유 기능(Claude의 thinking 등)을
못 쓰므로, 벤더별 공식 SDK를 그대로 쓰고 이 파일에서 입출력만 통일한다.

호출부(rag_service, coverage_extractor)는 generate()만 알면 되고,
어떤 벤더를 쓰는지는 .env의 answer_provider 한 줄로 바뀐다.
"""

import time
from dataclasses import dataclass

from app.core.config import get_settings


@dataclass(frozen=True)
class AnswerProvider:
    name: str
    vendor: str  # openai | anthropic | google
    model: str
    api_key_field: str
    # gpt-5 / gpt-5-mini는 temperature를 아예 받지 않고 400을 낸다.
    # 같은 벤더 안에서도 갈리므로 모델마다 표시한다.
    supports_temperature: bool = True


PROVIDERS: dict[str, AnswerProvider] = {
    # 현재 운영 기본값. 비교 실험의 기준선이다.
    "openai-mini": AnswerProvider(
        name="openai-mini", vendor="openai",
        model="gpt-4o-mini", api_key_field="openai_api_key",
    ),
    "openai-41mini": AnswerProvider(
        name="openai-41mini", vendor="openai",
        model="gpt-4.1-mini", api_key_field="openai_api_key",
    ),
    "openai-4o": AnswerProvider(
        name="openai-4o", vendor="openai",
        model="gpt-4o", api_key_field="openai_api_key",
    ),
    "openai-41": AnswerProvider(
        name="openai-41", vendor="openai",
        model="gpt-4.1", api_key_field="openai_api_key",
    ),
    "openai-5": AnswerProvider(
        name="openai-5", vendor="openai",
        model="gpt-5", api_key_field="openai_api_key",
        supports_temperature=False,
    ),
    "openai-51": AnswerProvider(
        name="openai-51", vendor="openai",
        model="gpt-5.1", api_key_field="openai_api_key",
    ),
    "openai-52": AnswerProvider(
        name="openai-52", vendor="openai",
        model="gpt-5.2", api_key_field="openai_api_key",
    ),
    "claude-opus": AnswerProvider(
        name="claude-opus", vendor="anthropic",
        model="claude-opus-5", api_key_field="anthropic_api_key",
    ),
    # 같은 벤더의 저가 모델도 넣는다. 약관 QA처럼 근거가 이미 주어진 작업은
    # 상위 모델과 품질 차이가 작을 수 있어, 비용 대비 효과를 봐야 한다.
    "claude-sonnet": AnswerProvider(
        name="claude-sonnet", vendor="anthropic",
        model="claude-sonnet-5", api_key_field="anthropic_api_key",
    ),
    # Gemini는 무료 등급에서 flash 계열만 돌아간다. pro는 결제를 등록해야
    # 429가 나지 않는다. 2.5 계열은 신규 사용자에게 더 이상 열리지 않아 404가 난다.
    "gemini-flash": AnswerProvider(
        name="gemini-flash", vendor="google",
        model="gemini-3.6-flash", api_key_field="google_api_key",
    ),
    "gemini-flash-35": AnswerProvider(
        name="gemini-flash-35", vendor="google",
        model="gemini-3.5-flash", api_key_field="google_api_key",
    ),
    "gemini-pro": AnswerProvider(
        name="gemini-pro", vendor="google",
        model="gemini-3.1-pro-preview", api_key_field="google_api_key",
    ),
}


def resolve(provider_name: str | None = None) -> AnswerProvider:
    settings = get_settings()
    name = provider_name or settings.answer_provider
    if name not in PROVIDERS:
        raise ValueError(
            f"알 수 없는 답변 모델 '{name}'. 사용 가능: {', '.join(PROVIDERS)}"
        )
    return PROVIDERS[name]


def _api_key(provider: AnswerProvider) -> str:
    key = getattr(get_settings(), provider.api_key_field, "")
    if not key:
        raise ValueError(
            f"{provider.name}을(를) 쓰려면 .env에 "
            f"{provider.api_key_field.upper()}가 있어야 합니다."
        )
    return key


def _generate_openai(provider: AnswerProvider, system: str, user: str) -> str:
    from openai import OpenAI

    # 약관 해석은 표현이 흔들리면 안 되므로 결정적으로 생성한다.
    # 다만 이를 받지 않는 모델이 있어, 지원하는 모델에만 넘긴다.
    extra = {"temperature": 0} if provider.supports_temperature else {}

    response = OpenAI(api_key=_api_key(provider)).chat.completions.create(
        model=provider.model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        **extra,
    )
    return (response.choices[0].message.content or "").strip()


def _generate_anthropic(provider: AnswerProvider, system: str, user: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=_api_key(provider))

    # temperature를 쓰지 않는다. Claude 5 계열은 이 파라미터를 받지 않고 400을 낸다.
    # 대신 thinking을 켜고 effort를 낮춰 비용과 지연을 잡는다. thinking을 끄면
    # 내부 태그가 답변에 새는 문제가 알려져 있어, 끄는 쪽이 오히려 위험하다.
    #
    # 근거가 이미 프롬프트에 주어진 작업이라 깊은 추론이 필요 없어 effort는 low로 둔다.
    response = client.messages.create(
        model=provider.model,
        max_tokens=2000,
        system=system,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": user}],
    )

    # 응답은 블록 배열이다. thinking 블록이 섞여 오므로 text만 골라야 한다.
    # content[0]을 그대로 읽으면 thinking 블록을 집어 빈 문자열이 나온다.
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude가 응답을 거부했습니다: {response.stop_details}")

    return "".join(b.text for b in response.content if b.type == "text").strip()


def _generate_google(provider: AnswerProvider, system: str, user: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=_api_key(provider))
    response = client.models.generate_content(
        model=provider.model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
        ),
    )
    return (response.text or "").strip()


_DISPATCH = {
    "openai": _generate_openai,
    "anthropic": _generate_anthropic,
    "google": _generate_google,
}


def _json_openai(provider: AnswerProvider, system: str, user: str) -> str:
    from openai import OpenAI

    extra = {"temperature": 0} if provider.supports_temperature else {}
    response = OpenAI(api_key=_api_key(provider)).chat.completions.create(
        model=provider.model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        **extra,
    )
    return (response.choices[0].message.content or "{}").strip()


def _json_anthropic(provider: AnswerProvider, system: str, user: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=_api_key(provider))
    # OpenAI의 response_format 같은 스위치는 없다. 프롬프트로 지시하고
    # 코드 펜스를 벗겨 파싱한다. JSON을 얼마나 안정적으로 내는지 자체가
    # 비교 항목이므로, 스키마를 강제하지 않고 그대로 재는 편이 낫다.
    #
    # 추출은 비동기 배치라 지연이 덜 중요하다. 답변용과 달리 effort를 올린다.
    response = client.messages.create(
        model=provider.model, max_tokens=8000, system=system,
        thinking={"type": "adaptive"}, output_config={"effort": "medium"},
        messages=[{"role": "user", "content": user}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude가 응답을 거부했습니다: {response.stop_details}")
    return "".join(b.text for b in response.content if b.type == "text").strip()


def _json_google(provider: AnswerProvider, system: str, user: str) -> str:
    from google import genai
    from google.genai import types

    # Client를 반드시 변수에 담는다. genai.Client(...).models.generate_content(...)처럼
    # 체인으로 쓰면 Client에 남는 참조가 없어 호출 도중 GC가 내부 연결을 닫고
    # "Cannot send a request, as the client has been closed"로 실패한다.
    client = genai.Client(api_key=_api_key(provider))
    response = client.models.generate_content(
        model=provider.model, contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system, temperature=0,
            response_mime_type="application/json",
        ),
    )
    return (response.text or "{}").strip()


_JSON_DISPATCH = {
    "openai": _json_openai,
    "anthropic": _json_anthropic,
    "google": _json_google,
}


def strip_code_fence(text: str) -> str:
    """```json ... ``` 로 감싸 오는 경우를 벗긴다.

    OpenAI와 Gemini는 JSON 전용 모드가 있어 순수 JSON이 오지만,
    Anthropic은 프롬프트 지시에 의존하므로 코드 펜스가 붙어 올 수 있다.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    return body.rsplit("```", 1)[0].strip()


def generate_json(
    system: str,
    user: str,
    provider_name: str | None = None,
) -> tuple[str, float]:
    """JSON 문자열과 소요 시간을 돌려준다. 파싱은 호출부가 한다.

    파싱 실패 자체가 모델 비교 항목이라 여기서 예외로 바꾸지 않는다.
    """
    provider = resolve(provider_name)
    started = time.time()
    raw = _JSON_DISPATCH[provider.vendor](provider, system, user)
    return strip_code_fence(raw), time.time() - started


def generate(
    system: str,
    user: str,
    provider_name: str | None = None,
) -> tuple[str, float]:
    """답변 텍스트와 소요 시간(초)을 함께 돌려준다.

    시간을 같이 재는 이유는 비교 실험에서 품질만큼 응답 속도가 중요하기 때문이다.
    챗봇은 사용자가 기다리는 화면이라 정확도가 조금 높아도 너무 느리면 못 쓴다.
    """
    provider = resolve(provider_name)
    started = time.time()
    answer = _DISPATCH[provider.vendor](provider, system, user)
    return answer, time.time() - started
