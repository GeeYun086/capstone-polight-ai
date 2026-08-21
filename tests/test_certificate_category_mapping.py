"""증권 담보명 -> 표준 카테고리 환산 (보장상세 연결용).

증권 담보명과 약관 규칙 title은 표기가 달라 이름으로 못 잇는다. 양쪽을 같은
표준 카테고리(medical_expense 등)로 환산해 백엔드가 그 키로 연결한다.
"""

from app.services.certificate_adapter import _standard_category


def test_증권_담보명이_표준_카테고리로_환산된다():
    # 실서버 coverage_items에 저장된 실제 메리츠 플랫폼 담보명 기준
    cases = {
        "(5세대)해외여행 상해_해외의료실비보장": "medical_expense",
        "(5세대)해외여행 질병_해외의료실비보장": "medical_expense",
        "휴대품손해(분실제외)": "baggage",
        "일괄배상 (해외여행중)": "liability",
        "항공기 및 수하물 지연비용": "flight_delay",
        "상해사망·후유장해 (해외여행중)": "death_disability",
        "해외여행중 중단사고 발생 추가비용": "trip_cancellation",
    }
    for title, expected in cases.items():
        assert _standard_category(title, None) == expected, title


def test_category는_항상_닫힌어휘_아니면_None():
    # 매핑 실패 시 에이전트 원값이 표준 어휘일 때만 채택, 아니면 None.
    # category 컬럼이 어휘 밖 값으로 오염되지 않도록 조인다(백엔드 계약).
    # 어휘 밖 한글 원값은 버린다
    assert _standard_category("여권분실후 재발급비용", "해외여행") is None
    # 표준 어휘 원값은 살린다(만에 하나 에이전트가 표준코드를 주면)
    assert _standard_category("여권분실후 재발급비용", "medical_expense") == "medical_expense"
    # 원값 없으면 None
    assert _standard_category("정체불명 담보 xyz", None) is None


def test_category_어휘_계약_증권과_약관이_같은_닫힌집합():
    """증권 담보 category와 약관 규칙 category는 동일한 닫힌 어휘를 써야 한다.

    백엔드가 category fallback으로 담보<->규칙을 연결하므로(title 우선, category 보조),
    양쪽 어휘가 어긋나면 연결이 조용히 실패한다. category_mapping이 뱉는 모든
    category가 standard_categories(단일 소스) 안에 있는지 못 박는다.
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "config"
    std = set(json.loads((root / "standard_categories.json").read_text(encoding="utf-8")))
    mapping = json.loads((root / "category_mapping.json").read_text(encoding="utf-8"))

    produced = set()
    for v in mapping.values():
        c = v if isinstance(v, str) else v.get("primary_category")
        if c:
            produced.add(c)

    outside = produced - std
    assert not outside, f"category_mapping이 표준 어휘 밖의 값을 뱉음: {outside}"
