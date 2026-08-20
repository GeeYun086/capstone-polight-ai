"""증권 분석 결과에서 개인정보를 지운다.

증권에는 피보험자 이름·생년월일·증권번호·연락처가 들어 있다. 이건 어댑터를 짜는 데도,
평가 정답을 만드는 데도 필요 없다. 필요한 것은 담보명·한도·가입여부뿐이다.

지우는 것과 남기는 것을 키 이름으로 가른다. 값을 보고 판단하면(예: 숫자면 지운다)
한도 금액까지 날아가고, 반대로 전부 남기면 이름이 그대로 따라온다.

사용법
    python scripts/redact_certificate.py 증권결과.json
    python scripts/redact_certificate.py 증권결과.json --out data/eval/cert_001.json

    # 어떤 키가 지워지는지 먼저 확인
    python scripts/redact_certificate.py 증권결과.json --dry-run

지운 뒤에는 반드시 눈으로 한 번 확인하십시오. 스튜디오가 어떤 키 이름을 쓰는지
모르는 상태라, 아래 목록에 없는 이름으로 개인정보가 들어 있을 수 있습니다.
--dry-run으로 남는 키를 훑어보는 것이 안전합니다.
"""

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

MASK = "***"

# 짧고 흔한 낱말은 키 이름 전체가 일치할 때만 지운다.
#
# 부분 일치로 두면 엉뚱한 것이 걸린다. 실제로 "age"를 부분 일치로 뒀다가
# "coverages"("cover-age-s")가 통째로 지워졌다. 어댑터를 짜는 데 꼭 필요한
# 담보 목록이 사라지는데, 개인정보를 지웠다고 생각하고 넘어가기 쉬운 종류의 사고다.
EXACT = frozenset({
    "age", "name", "gender", "sex", "tel", "zip", "card", "account",
    "나이", "성별", "이름", "주소", "전화", "메일", "우편번호",
})

# 길고 구별되는 낱말은 키 이름 어디에 있어도 지운다.
SENSITIVE = (
    # 사람을 가리키는 접두어.
    #
    # "name"만 전체 일치로 검사했더니 insured_person_name, payer_name이 빠져나가
    # 실명이 그대로 남았다. 실제 증권을 처리하다 노출됐다. 사람을 가리키는 낱말이
    # 앞에 붙는 형태가 흔하므로 접두어 자체를 잡는다.
    #
    # "insured"는 "insurer"(보험사)와 다른 문자열이라 insurer_name은 걸리지 않는다.
    # 보험사·상품명은 어댑터에 필요하므로 남아야 한다.
    "insured", "payer", "holder", "applicant", "beneficiary", "representative",
    "성명", "피보험자", "계약자", "수익자", "가입자", "대표자",
    "birth", "생년", "생일",
    "ssn", "주민", "resident", "여권", "passport",
    "phone", "mobile", "연락처", "휴대",
    "email", "address", "우편",
    "policyno", "policynumber", "증권번호", "계약번호", "certno", "certificateno",
    "계좌", "카드번호",
)

# 지우면 안 되는 것. 위 목록과 겹치는 이름이 있어 예외로 둔다.
#   productName, insurerName, coverageName 은 "name"을 포함하지만 상품 정보다.
KEEP = ("productname", "insurername", "coveragename", "companyname", "planname", "특약명", "담보명")

# 본문 텍스트 안에 남는 것들. 스튜디오가 원문을 함께 실어줄 때를 대비한다.
TEXT_PATTERNS = (
    (re.compile(r"\d{6}\s*-\s*\d{7}"), "******-*******"),      # 주민등록번호
    (re.compile(r"01[016-9]-?\d{3,4}-?\d{4}"), "010-****-****"),  # 휴대전화
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "***@***"),          # 이메일
)


def is_sensitive(key: str) -> bool:
    lowered = re.sub(r"[\s_-]", "", key).lower()
    if any(k in lowered for k in KEEP):
        return False
    if lowered in EXACT:
        return True
    return any(s in lowered for s in SENSITIVE)


def scrub_text(value: str) -> str:
    for pattern, replacement in TEXT_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def redact(node, removed: list[str], path: str = ""):
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if is_sensitive(key):
                removed.append(here)
                result[key] = MASK
            else:
                result[key] = redact(value, removed, here)
        return result
    if isinstance(node, list):
        return [redact(item, removed, f"{path}[{i}]") for i, item in enumerate(node)]
    if isinstance(node, str):
        return scrub_text(node)
    return node


def collect_keys(node, into: set, path: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            into.add(here)
            collect_keys(value, into, here)
    elif isinstance(node, list) and node:
        collect_keys(node[0], into, f"{path}[]")


def main() -> None:
    parser = argparse.ArgumentParser(description="증권 분석 결과에서 개인정보 제거")
    parser.add_argument("source", type=str)
    parser.add_argument("--out", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true", help="지울 키와 남는 키만 보여준다")
    args = parser.parse_args()

    source = Path(args.source)
    with source.open("r", encoding="utf-8") as f:
        data = json.load(f)

    removed: list[str] = []
    cleaned = redact(data, removed)

    print(f"지운 필드 {len(removed)}개")
    for path in removed[:40]:
        print(f"  - {path}")
    if len(removed) > 40:
        print(f"  ... 외 {len(removed) - 40}개")

    if args.dry_run:
        keys: set = set()
        collect_keys(cleaned, keys)
        print(f"\n남는 키 {len(keys)}개 (개인정보가 섞여 있지 않은지 확인하십시오)")
        for key in sorted(keys):
            print(f"  {key}")
        return

    out = Path(args.out) if args.out else source.with_name(f"{source.stem}_redacted.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)

    print(f"\n저장: {out}")
    print("공유하기 전에 파일을 직접 열어 확인하십시오. 키 이름을 기준으로 지우므로")
    print("예상 못 한 이름의 필드는 그대로 남아 있을 수 있습니다.")


if __name__ == "__main__":
    main()
