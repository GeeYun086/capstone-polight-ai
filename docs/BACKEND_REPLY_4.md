# AI 서버 연동 — AI 회신 (4차)

기준: AI `main` @ 최신 + `feat/coverage-detail-from-terms`
대상: `약관 저장소 분리 — AI 서버 싱크` (백엔드 `claude/policy-terms-refactor-v33t5n` @ 7c05c2c, PR #36)

약관 저장소를 공용/사용자로 가른 설계, 동의합니다. `coverage_items`가 "가입한 담보"와
"약관상 보장 규칙" 두 뜻으로 쓰이던 문제를 테이블로 가른 것이 정확합니다. 저희가
BACKEND_INTERFACE 3-2에서 지적한 "주인 없는 공용 약관을 넣을 자리가 없다"를 그대로
해결해 주셨습니다.

🔴 3가지는 모두 저희가 맞추겠습니다. 회신 요청 5건에 답합니다.

---

## 0. 🔴 3가지 — 전부 저희가 맞춥니다

### ① source='SEEDED' → 'OFFICIAL'로 넣겠습니다

`source`가 "수집 경로"가 아니라 "검수된 공식 약관인가"를 뜻하고 `verification_status`와
짝이라는 설명, 납득했습니다. `SEEDED/SOURCED`와 축이 다릅니다.

8건 전부 이렇게 넣겠습니다.

```
source = 'OFFICIAL'
verification_status = 'VERIFIED'
owner_user_id = NULL
```

수집 경로 컬럼은 **필요 없습니다**(회신 2번). 이 8건은 저희가 손으로 준비한 것이라
경로 구분이 의미가 없습니다. 나중에 크롤링(②)을 하게 되면 그때 요청드리겠습니다.

### ② coverage_item_sources → policy_terms_coverage_sources

이름·컬럼·부모가 바뀐 것 확인했습니다. 근거 조항을 공용 영역으로 옮긴 이유(같은 상품
가입자 모두에게 같은 조항이 근거인데 이전엔 가입자 수만큼 복제)도 맞습니다.

저희 적재 코드가 새 테이블·컬럼(`terms_coverage_id`, `terms_chunk_id`)을 쓰도록
맞추겠습니다. `docker/initdb/01_schema.sql`(로컬 사본)도 함께 동기화하겠습니다.

### ③ 약관 콜백의 자식 배열이 목적지가 바뀐 것

`coverageItems[].exclusions/requiredDocuments/subLimits/detailItems`가 이제
`policy_terms_coverages`로 가야 한다는 것 확인했습니다.

**증권 경로는 영향 없습니다** — `to_payloads`가 이 값들을 채우지 않아 원래 빈 배열로
나갑니다. 약관 경로만 목적지를 바꾸면 됩니다. 조용히 버리지 않고 경고를 남기신 판단이
저희에게도 도움이 됐습니다(계약 어긋남이 로그에 드러남).

다만 지금 약관 경로는 **콜백이 아니라 직접 INSERT**로 가는 구조라(1-7 합의), 저희가
`policy_terms_coverages`에 직접 넣겠습니다. 아래 3절.

---

## 1. severity / source_role — 이미 맞습니다

⚠️로 표시해 주신 두 가지는 저희 쪽에 이미 변환이 있어 손댈 것이 없습니다.

| 백엔드 허용값 | 저희 매핑 (db_enums) |
| --- | --- |
| severity `GENERAL/WARNING/CRITICAL` | `HIGH→CRITICAL`, `MEDIUM→WARNING`, `LOW→GENERAL` |
| source_role `PRIMARY/REQUIRED_DOCUMENT/...` | `COVERAGE→PRIMARY`, `DOCUMENT→REQUIRED_DOCUMENT` |

내부 값을 경계에서만 번역하는 구조라, DB에 나가는 값은 이미 CHECK를 통과합니다.
`ai-server-contract-answers.md` 2절 매핑표 그대로입니다.

`policy_terms_coverages`에 `limit_amount`(정수)를 두지 않고 `limit_label`에 원문만
넣는 판단도 맞습니다. 약관 예시값이 가입금액으로 읽히면 안 됩니다 — 저희도 증권 경로에서
정수 한도(`limit_amount`)와 원문(`limit_label`)을 나눠 다룹니다.

타임스탬프가 `exclusion_conditions`에만 있고 나머지 셋에 없다는 것, `sort_order` UNIQUE,
자식 4종의 부모가 `terms_coverage_id`로 바뀐 것 — 적재 코드에 반영하겠습니다.

---

## 2. 회신 요청 5건

| # | 내용 | 답 |
| --- | --- | --- |
| 1 | source를 OFFICIAL로 넣는 데 동의? | **동의** (0-①) |
| 2 | 수집 경로 컬럼 필요? | **불필요** (0-①) |
| 3 | policy_terms_coverages 적재 일정 | **권한 열리면 착수.** 단 성능 주의 (2-3) |
| 4 | 개정판 둘 이상 상품에 effective_date 채울 수 있나 | **가능. 해당 상품 없음** (2-4) |
| 5 | policy_chunks A안 / B안 | **A안 지지** (2-5) |

### 2-3. policy_terms_coverages 적재 — 됩니다, 단 시간이 걸립니다

프로토타입으로 확인했습니다. 현대 약관에서 카테고리별로 세부항목·세부한도·면책·
청구서류가 나옵니다.

```
baggage          detail 1  subLimit 2  doc 3  excl 4
medical_expense  detail 6  subLimit 9  doc 3  excl 6
flight_delay     detail 3  subLimit 0  doc 6  excl 7
liability        detail 2  subLimit 1  doc 4  excl 4
death_disability detail 3  subLimit 1  doc 0  excl 2
```

권한이 열리면 이 결과를 `policy_terms_coverages` + 자식 4종에 적재하겠습니다.

**성능 주의:** 적재는 약관 1건당 카테고리 수(5~8)만큼 LLM을 부릅니다. 8건이면 수십 회고,
현재 OpenAI·Anthropic 크레딧이 소진돼 Gemini 무료 티어로 도는데 분당 요청 제한(RPM)이
있습니다. **한 번만 하면 되는 작업**이라 나눠서 천천히 돌리면 되지만, 8건 전체 적재가
즉시 끝나진 않습니다. 순차로 채워지는 것으로 봐주세요.

### 2-4. effective_date — 채울 수 있고, 지금은 걸릴 상품이 없습니다

현재 8건은 **상품명이 서로 달라 개정판 충돌이 없습니다.** 현대해상이 2건이지만
`해외여행보험`(2022)과 `다이렉트 해외여행보험`(2025)으로 상품명이 달라, 매칭 규칙상
별개 상품입니다.

그래도 `effective_date`는 전부 채우겠습니다 — 이미 개정 시점을 갖고 있습니다.

```
현대 해외여행보험         2022-07-18
현대 다이렉트 해외여행보험  2025-06-30
KB 해외여행보험           2018-04-01
메리츠 해외여행 실손의료보험 2022-08-01
삼성화재 해외여행보험      2024-01-01
캐롯 해외여행보험         2025-09-01
DB 프로미 해외여행보험Ⅰ/Ⅳ  (개정 시점 미상 → 표지 확인 후 채움)
```

DB손보 2건만 개정 시점이 비어 있어 약관 표지에서 파싱해 채우겠습니다. 없으면 NULL로
두되, 위 설명대로 상품명이 달라 연결은 끊기지 않습니다.

### 2-5. policy_chunks — A안(개인 약관도 policy_terms) 지지합니다

개인 약관을 `policy_terms`의 `UNVERIFIED` + `owner_user_id`로 처리하는 A안에
동의합니다. 이유:

- 검색 코드가 **한 테이블(`policy_terms_chunks`)만** 보면 됩니다. 두 테이블을 UNION하면
  스코프 조건이 갈라져 유지가 어렵습니다
- 승격이 컬럼 하나(`verification_status`) 바꾸는 일이 됩니다
- BACKEND_REPLY_2 1-6의 "두 테이블 모두 보기"는 철회합니다 — A안이 그 필요를 없앱니다

`policy_chunks`를 아직 지우지 않으신 것(재시도 게이트가 조각 존재 확인에 사용)도
확인했습니다. 저희 검색은 A안 기준으로 `policy_terms_chunks`만 보도록 맞추겠습니다.

---

## 3. 저희 쪽 작업 — 권한 열리면 착수

PR #36 배포로 권한이 열리면 순서대로 진행합니다. 스키마가 확정돼 코드는 미리 짭니다.

| 작업 | 상태 |
| --- | --- |
| `01_schema.sql` 로컬 사본 동기화 | 착수 가능 (권한 무관) |
| 약관 8건 → `policy_terms` + `policy_terms_chunks` 적재 | 권한 후 |
| 검색 스코프를 `terms_id`로 확장 | 권한 후 |
| 보장 규칙 → `policy_terms_coverages` + 자식 4종 적재 | 권한 후 (2-3) |
| `title`을 약관 인쇄명 그대로(수식 없이) 넣기 | 적재 시 반영 |

6절의 매칭 규칙(EXACT/QUALIFIED/NONE, 4글자 미만 QUALIFIED 제외, title 중복 금지)에
맞춰 `title`을 넣겠습니다. 담보명에 금액·범위 수식을 붙이지 않겠습니다.

`DELETE 후 INSERT` 재적재, FK 삭제 순서, `UNIQUE(terms_id, chunk_index)`도 반영합니다.
`UPDATE` 권한을 뺀 이유(UNVERIFIED→VERIFIED 오승격 방지)도 납득했습니다 — 저희는
DELETE 후 INSERT만 씁니다.

---

## 4. 그밖에 싱크 확인

- **documentKind 기본값 CERTIFICATE (V8)** — 동작 변화 없음 확인. 프론트 명시 전송
  일정은 BACKEND_REPLY_3 4절 순서대로 진행합니다.
- **policy_chunks.document_id 인덱스 (V6)** — 이미 있는 것 확인했습니다. 회신 요청에서
  빼겠습니다. 계속 올려 죄송합니다.
- **startDate/endDate** — 지금은 `raw_result_json`에만 남고 버려지는 것 확인. 받는 작업이
  다음 PR이라는 것도 확인했습니다. `policies` 연동과 개정판 기준일(실제 보험 시작일)에
  쓰이는 것 좋습니다.

---

## 5. 회신 부탁드리는 것

| # | 내용 | 없으면 |
| --- | --- | --- |
| 1 | PR #36 배포·권한 개방 시점 | 적재 착수 시점을 잡을 수 없음 |
| 2 | 챗봇 프록시(프론트→AI `/internal/rag/query` 중계) 구현 일정 | 약관 적재해도 프론트에서 챗봇이 안 뜸 |

1번은 배포되면 알려주신다고 하셨으니 대기합니다. 2번은 약관 적재가 끝나도 프론트까지
닿으려면 필요한 중계라, 계획을 알려주시면 저희 검색 확장 일정을 맞추겠습니다.
