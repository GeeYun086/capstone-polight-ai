"""증권 한 장으로 부품을 전부 이어 실제 답변까지 뽑아본다.

부품은 각각 테스트로 고정돼 있지만, 이어서 돌려본 적은 없었다. 단위로는 통과하는데
합쳐 놓으면 안 되는 경우가 있어서(스코프 키가 어긋난다든지, 담보명이 그대로 넘어가
매칭이 죽는다든지) 실물로 한 번은 확인해야 한다.

백엔드가 붙기 전까지 이 스크립트가 엔드투엔드 검증 수단이다. 운영에서는 아래 흐름 중
"약관 찾기"를 백엔드가 맡는다(docs/BACKEND_INTERFACE.md 3-1).

    증권 JSON
      -> certificate_adapter   담보·금액·가입여부
      -> terms_matcher         어느 약관인가          (운영에서는 백엔드)
      -> clause_matcher        어느 특약인가
      -> rag_service           검색 + 답변

사용법
    python scripts/demo_certificate_chat.py 증권.json
    python scripts/demo_certificate_chat.py 증권.json --age 10
    python scripts/demo_certificate_chat.py 증권.json --question "휴대품 한도가 얼마예요?"
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.repositories.base import SearchScope  # noqa: E402
from app.repositories.file_repository import FileVectorRepository, _matches  # noqa: E402
from app.schemas.rag import RagQueryRequest  # noqa: E402
from app.services.certificate_adapter import coverage_names, to_coverages, to_payloads  # noqa: E402
from app.services.clause_matcher import match_coverages  # noqa: E402
from app.services.rag_service import answer_question  # noqa: E402
from app.services.terms_matcher import find_terms  # noqa: E402

# 세 가지를 각각 확인하려고 고른 질문들이다.
#   증권과 약관의 숫자가 다를 때 둘 다 살아서 나오는가
#   약관에는 있지만 증권에 없는 담보를 미가입으로 답하는가
#   특정 담보에 속하지 않는 공통 조항(청구 절차)이 검색되는가
DEFAULT_QUESTIONS = [
    "휴대품이 도난당하면 얼마까지 보상받을 수 있나요?",
    "골프용품이 파손되면 보상되나요?",
    "항공기가 지연됐을 때 보험금 청구하려면 뭐가 필요해요?",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="증권 -> 약관 -> 챗봇 엔드투엔드 확인")
    parser.add_argument("certificate", type=str, help="마스킹된 증권 분석 결과 JSON")
    parser.add_argument("--age", type=int, default=30, help="피보험자 나이 (연령대 컬럼 선택)")
    parser.add_argument("--question", type=str, action="append", default=None)
    parser.add_argument("--product", type=str, default="해외여행보험", help="증권의 상품명")
    args = parser.parse_args()

    with Path(args.certificate).open("r", encoding="utf-8") as f:
        certificate = json.load(f)

    insurer = certificate.get("insurer_name") or ""
    print("=" * 78)
    print(f"증권: {insurer} / {args.product} / 피보험자 {args.age}세")
    print("=" * 78)

    # 1. 증권 -> 담보
    coverages = to_coverages(certificate, age=args.age)
    cards = to_payloads(certificate, age=args.age)
    covered = sum(1 for c in coverages if c.subscribed)
    with_amount = sum(1 for c in coverages if c.limit_amount is not None)
    print(f"\n[1] 증권 분석   담보 {len(coverages)}건 (가입 {covered}, 미가입 {len(coverages) - covered})")
    print(f"                한도 금액 {with_amount}/{len(coverages)}건 확보")
    print(f"                보장 카드 {len(cards)}건 생성")

    # 2. 어느 약관인가 (운영에서는 백엔드가 한다)
    match = find_terms(insurer, args.product)
    print(f"\n[2] 약관 매칭   {match.terms_id or '(없음)'} [{match.level}]")
    if match.notice:
        print(f"                {match.notice}")
    if not match.is_usable:
        print("\n약관이 없어 증권 정보만으로 답하게 된다. 여기서 멈춘다.")
        return

    repository = FileVectorRepository()
    repository._ensure_loaded()
    scope = SearchScope(document_id=match.terms_id)
    chunks = [c for c in repository._chunks if _matches(c, scope)]
    clause_paths_available = sorted({c["clause_path"] for c in chunks if c.get("clause_path")})
    print(f"                청크 {len(chunks)}개 / 특약 {len(clause_paths_available)}개 색인됨")

    # 3. 어느 특약인가
    names = coverage_names(certificate)
    clause_paths, report = match_coverages(names, clause_paths_available)
    print(f"\n[3] 특약 매칭   {len(report['matched'])}/{len(names)}건 ({report['match_ratio']:.0%}) — {report['reason']}")
    if clause_paths is None:
        print("                필터를 걸지 않고 약관 전체에서 검색한다")

    # 4. 챗봇
    questions = args.question or DEFAULT_QUESTIONS
    for question in questions:
        print("\n" + "-" * 78)
        print(f"Q. {question}")
        print("-" * 78)

        request = RagQueryRequest.model_validate(
            {
                "userId": "demo",
                "tripId": "demo",
                "documentId": match.terms_id,
                "question": question,
                "coverages": [c.model_dump(by_alias=True) for c in coverages],
                # 어댑터가 보장내용 표를 통째로 변환하므로 이 목록은 증권 전체다.
                # 알려주지 않으면 목록에 없는 담보를 물었을 때 "가입되어 있는 경우
                # 보상됩니다"라는 답이 나가고, 사용자는 보상된다고 읽는다.
                "coveragesComplete": True,
                "clausePaths": list(clause_paths or []),
            }
        )
        response = answer_question(request, repository)
        print(response.answer.strip())
        if response.sources:
            print(f"\n  근거 {len(response.sources)}건:")
            for source in response.sources[:3]:
                print(f"    p{source.page} {source.quote[:70]}...")


if __name__ == "__main__":
    main()
