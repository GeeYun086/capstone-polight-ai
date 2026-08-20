"""DB가 허용하는 enum 값과, 우리 내부 값에서 그 값으로의 번역.

백엔드 스키마의 컬럼들에는 CHECK 제약이 걸려 있고 Java 쪽은 @Enumerated(STRING)이라
대소문자까지 정확히 일치해야 한다. 우리가 쓰던 값(included, PARTIAL, HIGH 등)을
그대로 넣으면 제약 위반으로 INSERT가 실패한다.

내부 값을 바꾸지 않고 경계에서만 번역하는 이유:

    included/excluded는 청킹, 면책 짝짓기, 검색, 프롬프트, 평가셋이 모두 쓰는 값이다.
    전부 대문자로 바꾸면 이미 저장된 청크 파일 7개(1,915조각)까지 다시 만들어야 한다.
    DB에 쓸 때와 콜백을 보낼 때만 변환하면 고칠 파일이 두 개로 끝난다.

번역표의 근거는 백엔드가 회신한 V1__baseline_schema.sql의 CHECK 제약이다.
값이 바뀌면 이 파일만 고치면 된다.
"""

# ── policy_chunks.clause_type ────────────────────────────────
# GENERAL | COVERAGE | EXCLUSION | CONDITION | LIMIT | DEFINITION | PROCEDURE | REQUIRED_DOCUMENT
CLAUSE_TYPE = {
    "general": "GENERAL",
    "included": "COVERAGE",
    "excluded": "EXCLUSION",
    "procedure": "PROCEDURE",
    "definition": "DEFINITION",
}
CLAUSE_TYPE_FALLBACK = "GENERAL"

# ── policy_chunks.source_content_type ───────────────────────
# TEXT | TABLE | OCR_TEXT | IMAGE_CAPTION
#
# 이 컬럼은 "어떤 방식으로 추출한 텍스트인가"를 뜻한다. 우리가 쓰던 heading/list는
# "문서 구조상 무엇인가"라서 축이 다르므로 셋 다 TEXT로 접는다.
# 구조 정보는 section_title과 clause_path에 이미 담고 있어 손실이 없다.
SOURCE_CONTENT_TYPE = {
    "paragraph": "TEXT",
    "heading": "TEXT",
    "list": "TEXT",
    "table": "TABLE",
}
SOURCE_CONTENT_TYPE_FALLBACK = "TEXT"

# ── coverage_items.coverage_status ──────────────────────────
# COVERED | PARTIALLY_COVERED | NOT_COVERED | EXCLUDED
#
# UNKNOWN은 허용값에 없다. 판단이 안 되면 NOT_COVERED로 보낸다.
# NOT_COVERED(약관에 조항이 없음)와 EXCLUDED(약관이 명시적으로 배제)는 화면 문구가
# 달라지는 구분이라, 우리는 근거 조항의 성격으로 구분해 살린다.
COVERAGE_STATUS = {
    "COVERED": "COVERED",
    "PARTIAL": "PARTIALLY_COVERED",
    "PARTIALLY_COVERED": "PARTIALLY_COVERED",
    "NOT_COVERED": "NOT_COVERED",
    "EXCLUDED": "EXCLUDED",
    "UNKNOWN": "NOT_COVERED",
}
COVERAGE_STATUS_FALLBACK = "NOT_COVERED"

# ── coverage_item_sources.source_role ──────────────────────
# PRIMARY | CONDITION | EXCLUSION | LIMIT | PROCEDURE | REQUIRED_DOCUMENT | DEFINITION
SOURCE_ROLE = {
    "COVERAGE": "PRIMARY",
    "PRIMARY": "PRIMARY",
    "EXCLUSION": "EXCLUSION",
    "LIMIT": "LIMIT",
    "DOCUMENT": "REQUIRED_DOCUMENT",
    "REQUIRED_DOCUMENT": "REQUIRED_DOCUMENT",
    "CONDITION": "CONDITION",
    "PROCEDURE": "PROCEDURE",
    "DEFINITION": "DEFINITION",
}
SOURCE_ROLE_FALLBACK = "PRIMARY"

# ── exclusion_conditions.severity ──────────────────────────
# GENERAL | WARNING | CRITICAL
SEVERITY = {
    "LOW": "GENERAL",
    "MEDIUM": "WARNING",
    "HIGH": "CRITICAL",
    "GENERAL": "GENERAL",
    "WARNING": "WARNING",
    "CRITICAL": "CRITICAL",
}
SEVERITY_FALLBACK = "WARNING"


def _translate(table: dict[str, str], fallback: str, value: str | None) -> str:
    """모르는 값이 와도 예외를 내지 않고 fallback으로 보낸다.

    여기서 예외를 던지면 분석 전체가 실패한다. 값 하나가 이상해서 어렵게 만든
    policy_chunks까지 무의미해지는 것보다, 안전한 값으로 저장하고 넘어가는 편이 낫다.
    """
    if value is None:
        return fallback
    return table.get(value, table.get(value.upper(), fallback))


def clause_type(value: str | None) -> str:
    return _translate(CLAUSE_TYPE, CLAUSE_TYPE_FALLBACK, value)


# DB 값에서 내부 값으로 되돌린다.
#
# 저장할 때만 번역하고 끝내면 안 된다. 읽어온 값을 그대로 쓰면 프롬프트의
# 조항 라벨(COVERAGE_TYPE_LABELS)과 면책 짝짓기 로직이 전부 어긋난다.
# 저장 경로와 조회 경로에서 같은 표를 양방향으로 쓴다.
CLAUSE_TYPE_REVERSE = {db: internal for internal, db in CLAUSE_TYPE.items()}


def clause_type_to_internal(value: str | None) -> str:
    """DB의 COVERAGE를 내부 included로 되돌린다. 매핑에 없는 값은 그대로 둔다."""
    if value is None:
        return "included"
    return CLAUSE_TYPE_REVERSE.get(value, value)


def source_content_type(value: str | None) -> str:
    return _translate(SOURCE_CONTENT_TYPE, SOURCE_CONTENT_TYPE_FALLBACK, value)


def coverage_status(value: str | None) -> str:
    return _translate(COVERAGE_STATUS, COVERAGE_STATUS_FALLBACK, value)


def source_role(value: str | None) -> str:
    return _translate(SOURCE_ROLE, SOURCE_ROLE_FALLBACK, value)


def severity(value: str | None) -> str:
    return _translate(SEVERITY, SEVERITY_FALLBACK, value)
