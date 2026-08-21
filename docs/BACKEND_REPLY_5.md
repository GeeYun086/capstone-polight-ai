# AI 서버 연동 — AI 회신 (5차)

기준: AI `main` + `feat/terms-id-scope`
대상: `약관 저장소 분리 — 4차 회신에 대한 백엔드 답변` (백엔드 develop @ f6ec44a, PR #36·#37·#38)

0절의 변경(startDate 받음·policies 삭제·챗봇 프록시 존재)을 저희 인식에 반영했습니다.
회신 요청 2건에 답합니다 — **둘 다 저희가 맞췄습니다.** 그리고 저희 쪽에서 정리한 것을
적습니다.

---

## 1. 🔴 회신 요청 2건

### 1-1. title에 한글 담보명 — 맞습니다. 프로토타입 출력이 오해를 샀습니다

지적하신 그대로입니다. `title`은 약관 인쇄명(한글), `category`는 슬러그입니다.

혼란의 원인은 저희 4차 회신에 실은 프로토타입 출력이었습니다.

```
baggage          detail 1  subLimit 2 ...
medical_expense  detail 6  subLimit 9 ...
```

이건 **데모 스크립트가 카테고리 슬러그를 줄 라벨로 찍은 것**이지 `title` 값이
아닙니다. 실제 추출 결과는 이렇게 들어갑니다.

| 컬럼 | 값 | 예 |
| --- | --- | --- |
| `title` | 약관 인쇄 담보명(한글), 수식 없이 | `상해의료비` |
| `category` | 카테고리 슬러그 | `medical_expense` |
| `limit_label` | 약관 한도 표기 원문 | `보험가입금액 한도` |

저희 적재 코드(`terms_mapper.coverage_rows`)가 `title=item.title`(LLM이 뽑은 한글명),
`category=item.category`(슬러그)로 넣습니다. 로컬 pgvector로 확인했을 때도
`title='휴대품손해(분실제외)'`로 저장됐습니다. 한 약관 안 title 중복 금지, 4글자 미만
QUALIFIED 제외도 반영합니다.

### 1-2. termsId — 붙였습니다

제안하신 대로 확정하고 저희 요청 스키마에 넣었습니다.

| 항목 | 값 |
| --- | --- |
| 필드명 | `termsId` |
| 타입 | UUID, **nullable** |
| null일 때 | **약관 검색을 건너뛰고 증권 `coverages`만으로 답합니다** |

null 동작을 이렇게 정한 이유: 공용 약관은 사용자·여행에 매이지 않아 `tripId`로
폴백해도 0건이고, 개인 약관을 잘못 끌어올 위험만 생깁니다. 그래서 `termsId`가 없으면
약관 근거 없이 증권 담보 정보로만 답합니다("가입하셨습니다/안 하셨습니다"는 되고,
"약관상 면책은…"은 안 됩니다).

`RagQueryRequest.terms_id`를 optional로 받으므로, 백엔드가 보내기 시작하면 바로
동작하고 안 보내도 기존과 같습니다.

**다만 검색이 실제로 `policy_terms_chunks`를 보는 전환은 권한 개방 후입니다.** 지금은
`policy_chunks`를 보고 있어 `terms_id` 컬럼이 없습니다. 스코프 키로 받는 준비는
끝났고, 약관 이관과 검색 전환을 같은 시점(권한 후)에 합니다. 그때까지 `termsId`를
보내주셔도 무해합니다(받아두고 아직 안 씀).

---

## 2. 0절 변경 — 반영했습니다

| 항목 | 반영 |
| --- | --- |
| `startDate`/`endDate` 받음 (V11) | 저희는 이미 보내는 중. 그대로 둡니다 |
| `policies` 삭제 (V12) | `verify_pgvector.py`의 INSERT 제거, 로컬 스키마 사본에서 테이블·FK 제거. `policy_id` 컬럼은 null로 유지 |
| 담보↔규칙 연결 (`terms_coverage_id`) | 저희 로컬 스키마에 반영 완료 |
| 마이그레이션 번호 V11·V12 | 확인 |
| 챗봇 프록시 (PR #38) | 확인. 아래 |

**챗봇 프록시가 이미 있다는 것**이 저희에게 가장 큰 소식이었습니다. 4차에서 "구현
일정?"으로 물었는데 이미 develop에 있어, 배포만 되면 프론트→백엔드→AI 경로가
완성됩니다. 무상태(history를 백엔드가 잘라 보냄)로 두신 것도 저희 설계와 맞습니다 —
`RagQueryRequest.history`로 그대로 받습니다.

프록시가 보내는 값(userId/tripId/sessionId/question/history/coverages)은 저희 스키마와
일치합니다. `coveragesComplete`가 현재 항상 false인 것, `documentId`/`policyId`/
`clausePaths`가 null·빈 배열인 것도 확인했습니다 — 전부 저희 쪽에서 그 상태로 정상
동작합니다.

---

## 3. 백필 — 백엔드가 맡아주셔서 감사합니다

"규칙 적재 후 기존 분석 재연결"을 백엔드가 만들고, 운영 73건을 이어주신다는 것
확인했습니다. 저희가 할 일은 **적재 완료를 알려드리는 것**뿐이라고 이해했습니다.

적재는 4차에 적은 대로 순차로 채워집니다(LLM RPM 제한). 8건이 다 끝나면 통보
드리겠습니다.

---

## 4. effective_date / severity / A안 — 확인

- **effective_date**: 현대 2건이 별개 상품으로 갈리는 것, 보험사-단독 매칭도 후보가
  하나일 때만 발동해 현대는 NONE이 되는 것 확인했습니다. 상품명 표기 정확도가
  중요하다는 점 유념하겠습니다. DB손보 2건은 표지에서 시행일을 채우겠습니다.
- **severity / source_role**: 경계 번역으로 CHECK 통과. 저희 쪽 추가 작업 없음.
- **A안**: 확정. 개인 약관도 `policy_terms`(UNVERIFIED + owner_user_id). `policy_chunks`는
  검색을 `policy_terms_chunks`로 옮긴 뒤 정리 시점을 함께 잡겠습니다.

---

## 5. 저희 쪽 남은 작업

| 작업 | 조건 |
| --- | --- |
| 약관 8건 → `policy_terms` 계열 적재 | **권한 개방** (develop→main 릴리스) |
| pg 검색 `policy_terms_chunks` 전환 (`terms_id` 스코프, 면책 JOIN 재작성) | 권한 개방 |
| 증권 분석 시 약관 세부정보 결합 (파이프라인 통합) | 위 둘 |
| 적재 완료 통보 → 백엔드 백필 | 적재 후 |

**권한 개방(릴리스) 통보만 기다립니다.** 그 외에 백엔드에서 받을 것은 없습니다.
`termsId`와 `title`은 이 회신으로 정리됐고, 나머지는 저희 몫입니다.

## 6. 회신 부탁드리는 것

| # | 내용 |
| --- | --- |
| 1 | develop→main 릴리스(권한 개방) 시점 — 정해지는 대로 |

이 하나뿐입니다. 릴리스되면 `role_table_grants` 쿼리로 확인하고 바로 착수하겠습니다.
