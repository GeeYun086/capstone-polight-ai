"""분석 실패를 사용자에게 보일 문구와 내부 진단으로 나눠 들고 다니는 예외.

실패 콜백의 errorMessage는 analysis_results.failure_reason에 그대로 저장되고
프론트 화면까지 내려간다. 그래서 예외 메시지를 그대로 실어 보내면 두 가지가
새어 나간다.

  presigned URL   다운로드 실패 메시지에 서명이 붙은 주소가 통째로 들어 있었다.
                  DB에 영구 저장되고 화면에도 뜬다
  내부 지시문     "에이전트 출력 형식을 확인하십시오"는 사용자가 할 수 있는 일이
                  아니다. 실제로 이 문구가 사용자 화면까지 노출됐다

그래서 나눈다. user_message만 콜백에 실리고, 원인 추적용 문자열은 예외 메시지에
둬서 AI 로그에만 남는다. 둘을 잇는 열쇠는 analysisResultId다 - 그 값은 양쪽
로그에 모두 찍히고 백엔드도 갖고 있다.

내부 사유를 DB에도 남기고 싶으면 콜백에 필드를 하나 더 두는 것이 맞다.
지금은 계약을 바꾸지 않고 로그로 충분하다.
"""


class AnalysisFailure(RuntimeError):
    """분석 실패. process_analysis가 잡아 실패 콜백으로 바꾼다.

    RuntimeError를 상속하는 이유는 기존 실패 지점들이 전부 RuntimeError였기
    때문이다. 상위 타입을 바꾸면 이 예외를 잡던 자리가 조용히 새어 나간다.

    첫 인자는 사용자에게 보내지 않는다. 로그에만 남으므로 필요한 만큼 자세히
    적어도 된다. 단 증권 원문은 넣지 말 것 - 피보험자 이름·생년월일·증권번호가
    들어 있다. 모양(키 이름·개수)까지가 안전한 경계다.
    """

    # 하위 클래스가 클래스 속성으로 기본 문구를 갈아끼운다.
    user_message = "분석에 실패했습니다. 잠시 후 다시 시도해 주세요."

    def __init__(self, detail: str, user_message: str | None = None):
        super().__init__(detail)
        if user_message is not None:
            self.user_message = user_message
