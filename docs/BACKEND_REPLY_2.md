# AI 서버 연동 — AI 회신 (백엔드 2차 회신에 대한 답)

기준: AI `main` @ `88dbdac`
대상: `AI 서버 연동 — 백엔드 회신 (2차)` (백엔드 develop @ `0206cc3`, 2026-08-19)

3단계 분기 구조를 공유해주셔서 감사합니다. `NONE`이 종점이 아니라 ②로 연결된다는 점이
저희 문서에 없던 설계라, 이 회신은 그 전제 위에서 씁니다.

먼저 **백엔드 작업 목록에서 지울 것이 두 개** 있습니다. 2차 회신 작성 이후에 배포가
끝나서 상황이 바뀌었습니다(0번). 그다음 회신 요청 11건에 답합니다(1번).

---

## 0. 먼저 — AI 서버는 배포가 끝났습니다

2차 회신의 4절 "정정" 중 AI 쪽 항목은 해소되었습니다.

| 4절에 적힌 것 | 현재 |
| --- | --- |
| AI 컨테이너가 같은 도커망에 없다 | **합류 완료.** `polight_polight-network` |
| `docker-compose.prod.yml`에 ai 서비스 없음 | **추가하지 않는 것이 설계입니다** (아래) |

검증한 내용입니다.

```
Spring 컨테이너에서
  curl http://polight-ai:8000/health           200
  POST /internal/rag/query  (키 없이)          401
  POST /internal/rag/query  (맞는 키)          200
```

`INTERNAL_API_KEY` 검증과 컨테이너 간 통신은 백엔드 구현 없이도 이미 확인된 상태라,
1단계(검증 필터 + 콜백 수신)를 곧바로 실서버에 붙여 확인하실 수 있습니다.

### 0-1. compose 분리는 의도한 설계입니다 — 단계 4는 불필요합니다

백엔드 작업 순서 4단계 `docker-compose.prod.yml`에 ai 서비스 추가는 **하지 않으셔도
됩니다.** 저희가 파일을 빠뜨린 것이 아니라 일부러 분리했습니다.

같은 compose에 넣으면 **Spring을 배포할 때마다 AI 컨테이너가 함께 재시작됩니다.**
분석은 응답을 보낸 뒤에도 프로세스 안에서 3~5분 더 돕니다(`BackgroundTasks`).
그 사이 재시작되면 작업이 통째로 사라지고 콜백을 못 보내
`analysis_results`가 `PROCESSING`에 고착됩니다. 에러도 로그도 남지 않습니다.

`docker network`만 공유하면 컨테이너끼리 이름으로 통신하면서 배포는 서로 독립적입니다.
AI가 재배포되어도 Spring은 영향받지 않고, 반대도 마찬가지입니다.

백엔드가 `docker compose down`을 하면 저희 컨테이너가 붙어 있어 네트워크 삭제에
실패했다는 경고가 뜹니다. 무해합니다. 네트워크는 남고 양쪽 다 정상 동작합니다.

### 0-2. 인스턴스는 이미 t3.medium입니다

"t3.medium 승격이 팀 결정 대기 중"이라고 적으셨는데 **이미 승격했습니다.**
EBS도 20GB로 늘렸습니다.

그리고 승격 전 사양이 t3.small(2GB)이 아니라 **1GB**였습니다.

```
승격 전   free -m  total 912   available 194   swap 0
승격 후   free -m  total 3835  available 2987
디스크    8GB (여유 4GB)  ->  20GB (여유 16GB)
```

여유 메모리가 194MB뿐이라 컨테이너를 하나 더 올리는 순간 OOM 킬러가 가장 큰
프로세스인 **JVM을 먼저 죽이는** 상태였습니다. 그래서 승격이 선택이 아니라
배포의 선행 조건이었습니다. 지금은 JVM 622MB + AI 189MB로 여유가 충분합니다.

---

## 1. 회신 요청 11건

| # | 항목 | 답 |
| --- | --- | --- |
| 1 | `/internal/terms/source` 신규 | **범위 결정 필요** (1-1) |
| 2 | 콜백에 `startDate`/`endDate` | **확인 중** — 에이전트 스키마에 달림 (1-2) |
| 3 | 완전성 플래그 | **수용**. 단 초기값은 보수적으로 (1-3) |
| 4 | 약관 색인에 `effective_date` | **수용** (1-4) |
| 5 | 벡터 거리 연산자 | **회신 완료**. 본문에 재기재 (1-5) |
| 6 | 검색이 두 소스를 보도록 | **수용** (1-6) |
| 7 | 약관 8건 이관 + 주체 | **수용**. AI가 직접 INSERT (1-7) |
| 8 | `stored_file_path` + S3 업로드 주체 | **수용**. presigned PUT 제안 (1-8) |
| 9 | 메모리 + 환경변수 | **회신** (1-9) |
| 10 | presigned 15분 | **충분합니다** (1-10) |
| 11 | `NONE` 의미 변경 | **확인** (1-11) |

### 1-1. `/internal/terms/source` — MVP 범위를 함께 정했으면 합니다

책임 분리 논리에 동의합니다. "외부 문서를 찾아 파싱·색인"은 문서 이해이고
백엔드에는 LLM·파서·임베딩이 하나도 없으니, 저희가 맡는 것이 맞습니다.

가드 3개도 그대로 받아들입니다. 특히 **"LLM이 기억에서 재구성한 내용은 틀렸는지
확인할 방법이 없다"**는 지적이 정확합니다. URL 탐색에만 LLM을 쓰고, 시행일과 내용은
원문에서 읽겠습니다.

다만 이건 **신규 개발이고 리드타임이 가장 깁니다.** 크롤링·출처 판별·시행일 파싱·
원문 확보·해시 기록이 전부 새로 들어갑니다. 그래서 범위를 먼저 정했으면 합니다.

| 안 | 내용 | 결과 |
| --- | --- | --- |
| A | MVP에 포함 | ②가 동작. 대신 다른 항목이 밀림 |
| B | MVP는 ①③만, ②는 이후 | 8건 밖 보험사는 ③(사용자 업로드)로 떨어짐. 서비스는 정상 동작 |

**B로 가도 기능이 죽지 않는다**는 점을 짚어둡니다. ③ 경로가 이미 있으므로
사용자 경험은 "약관을 올려주세요"로 이어지고, 보장 카드는 증권에서 나오므로
화면은 그대로 뜹니다. 팀 일정에 따라 판단해주시면 그에 맞추겠습니다.

> **[결정 필요]** A / B 중 어느 쪽으로 갈지

### 1-2. `startDate`/`endDate` — 에이전트 스키마 확인이 선행입니다

블로커라는 것에 동의합니다. `policies`의 `NOT NULL`을 채울 정보원이 증권뿐인 것도
맞고, ②의 시행판 선택 기준으로도 필요합니다.

다만 지금 저희 코드가 증권 결과에서 읽는 것은 다음뿐입니다.

```
coverage_by_age_table        담보명 · 가입금액 · 카테고리
coverage_description_table   담보 설명
insurer_name / product_name / document_title
```

**보험기간을 읽는 코드가 없습니다.** 스키마는 Upstage Studio 에이전트에 저장돼 있어
저희 저장소에 없고, 에이전트가 보험기간을 뽑고 있는지부터 확인해야 합니다.

- 뽑고 있으면 → 콜백에 싣는 것은 간단합니다. 코드 수정만
- 안 뽑고 있으면 → **Studio에서 스키마를 고치는 작업이 선행**입니다. 코드 변경은 없지만 에이전트 재검증이 필요합니다

확인 후 회신드리겠습니다. 필드명은 요청하신 대로 `startDate` / `endDate`,
형식은 `YYYY-MM-DD`로 하겠습니다.

증권번호·피보험자명을 요청하지 않으신 판단에 감사드립니다. 저희도 증권 PDF를
처리 직후 삭제하고 있어, 애초에 오래 들고 있지 않습니다.

### 1-3. 완전성 플래그 — 수용하되 초기값은 `false`입니다

**논리가 맞습니다.** 판단 주체와 정보 보유 주체가 일치해야 하고, 그 정보는 추출한
쪽에만 있습니다. 백엔드가 영구히 `false`를 보낼 수밖에 없다는 지적도 맞습니다.

그런데 솔직히 말씀드리면 **현재 에이전트 출력만으로는 저희도 완전성을 알 수 없습니다.**
원본 표에 몇 줄이 있었는지를 에이전트가 알려주지 않기 때문입니다. 이 상태에서
`true`를 보내면 근거 없는 값이 되고, 문서에 적어두신 "파싱이 빠뜨린 담보를
미가입이라고 답하는" 사고가 그대로 납니다.

그래서 이렇게 가겠습니다.

1. **콜백에 필드를 지금 추가합니다** — 판단 위치를 AI 쪽으로 옮겨둡니다
2. **값은 당분간 `false`** — 백엔드가 보내던 것과 결과는 같습니다
3. 에이전트가 표 행 수나 파싱 상태를 함께 내보내도록 고친 뒤 **값만 켭니다**

3번 시점에 백엔드 코드는 바뀌지 않습니다. 저장해두고 되돌려주는 구조만 만들어두시면
됩니다. 필드명은 RAG 요청과 통일해 `coveragesComplete`로 하겠습니다.

### 1-4. `effective_date` — 수용합니다

`revision` 자유 문자열로는 날짜 비교가 안 된다는 지적이 맞습니다.
`effective_date DATE`를 채우고 `revision`은 화면 표시용으로 남기겠습니다.

**시행일을 약관 원문 표지에서 파싱해야 한다**는 조건도 그대로 지키겠습니다.
LLM이 말한 값은 확인할 방법이 없다는 데 동의합니다.

"버저닝은 안 하지만 시행일은 기록한다", 그리고 UNIQUE 키에 시행일을 미리 포함시켜
나중에 여러 판을 담아도 마이그레이션이 없게 한다는 판단도 좋습니다.

### 1-5. 벡터 거리 연산자 (별도 전달분 재기재)

| 항목 | 값 |
| --- | --- |
| 임베딩 모델 | Upstage `embedding-passage`(색인) / `embedding-query`(질의) |
| 차원 | **1536** — 현재 DDL `vector(1536)`과 일치 |
| metric | **코사인**. pgvector `<=>` |
| 정규화 | 저장은 원본 그대로. 정규화는 조회 후 재순위용으로만 |
| SQL top-K | **32** (최종 8건 × 4배 후보) |
| 최종 반환 | **8건** — MMR 재순위로 축소 |
| threshold | **없음.** 유사도·거리 컷오프를 쓰지 않음 |

기본 4096차원을 1536으로 줄인 이유는 pgvector가 `vector` 타입에 2000차원까지만
인덱스를 지원하기 때문입니다. 백엔드 DDL을 그대로 쓸 수 있게 맞췄습니다.

실제 검색 SQL은 `policy_chunks` 기준으로 다음과 같습니다.

```sql
SELECT c.id, c.document_id, c.chunk_index, c.page_start, c.page_end,
       c.section_title, c.clause_path, c.coverage_category, c.clause_type,
       c.content, c.embedding,
       rel.id AS related_id,
       1 - (c.embedding <=> %(vector)s::vector) AS score
FROM policy_chunks c
LEFT JOIN policy_chunks rel
       ON rel.analysis_result_id = c.analysis_result_id
      AND rel.chunk_index = c.chunk_index + 1
      AND c.clause_type = 'COVERAGE'
      AND rel.clause_type = 'EXCLUSION'
      AND rel.coverage_category = c.coverage_category
WHERE c.embedding IS NOT NULL
  AND c.document_id = %(document_id)s
ORDER BY c.embedding <=> %(vector)s::vector
LIMIT %(top_k)s
```

`LEFT JOIN`은 보장 조항에 딸린 면책 조항을 함께 끌어오는 부분입니다. DDL에
`related_chunk_id`가 없어 조회 시점에 `chunk_index + 1`로 계산하며,
`UNIQUE (analysis_result_id, chunk_index)`가 순서를 보장해줍니다.

**`policy_terms_chunks`에서도 그대로 동작합니다.** `UNIQUE (terms_id, chunk_index)`가
있으므로 조인 기준만 `terms_id`로 바꾸면 됩니다. 저희 쪽 수정 사항이라 DDL은
그대로 두셔도 됩니다.

#### 벡터 인덱스는 지금 만들지 않기를 권합니다

DDL의 `idx_policy_terms_chunks_terms (terms_id, clause_path)`가 실질적으로 더
중요합니다. 검색이 항상 `terms_id`로 먼저 좁혀져 후보가 **약관 1건당 300건 안팎**이고
(현재 8건 = 2225청크), 이 규모에서는 전수 계산이 더 빠르고 정확합니다.

HNSW는 `WHERE` 조건을 모른 채 탐색하기 때문에 필터가 강하면 `LIMIT`을 못 채우거나
재현율이 떨어지는 문제가 알려져 있습니다. 한 스코프의 청크가 수천 건을 넘어가면
그때 붙이는 것을 제안합니다. 만들 때 opclass는 다음과 같습니다.

```sql
CREATE INDEX idx_policy_terms_chunks_embedding
  ON policy_terms_chunks USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
```

참고로 키워드 검색(BM25)은 스코프 안의 행을 전부 읽어 애플리케이션에서 계산합니다.
그래서 스코프 컬럼 인덱스가 벡터 인덱스보다 먼저입니다.

> **[참고]** `policy_chunks` 쪽은 현재 인덱스가 `(user_id, document_id)`,
> `(user_id, trip_id)` 복합인데 저희 조건에는 `user_id`가 없어 사용되지 않습니다.
> ③ 경로가 켜지기 전에 `document_id` 단독 인덱스를 추가해주시면 좋겠습니다.

### 1-6. 검색이 두 소스를 보도록 — 수용합니다

③으로 올라온 약관을 공용 테이블에 넣지 않는 판단에 동의합니다. 검수 없이 남의
약관 근거가 되면 안 된다는 이유가 타당합니다.

이관 시점에 저희 검색 코드가 `policy_terms_chunks`(공용)와 `policy_chunks`(개인)를
모두 보도록 함께 처리하겠습니다. 3-3에서 `termsId`를 새 필드로 받아달라고 하신 것과
맞물려, 검색 스코프도 `termsId` 기반으로 확장합니다.

`documentId` 재사용이 아니라 새 필드로 가자는 판단이 맞습니다. 한 필드에 UUID와
문자열 식별자를 섞으면 구분이 안 됩니다.

### 1-7. 약관 8건 이관 — AI가 직접 INSERT하겠습니다

`policy_chunks`와 같은 방식이 자연스럽다는 데 동의합니다. 백엔드 엔드포인트를
새로 여는 것보다 간단합니다.

필요한 것은 **`rag_service` 계정에 `policy_terms` / `policy_terms_chunks` INSERT
권한**입니다. 부여해주시면 이관하겠습니다.

`source` 값은 이 8건 전부 `SEEDED`로 넣겠습니다. 크롤링분(`SOURCED`)과 구분되어야
문제가 생겼을 때 크롤링분만 골라 지울 수 있다는 설계 의도에 동의합니다.

### 1-8. `stored_file_path`와 S3 업로드 주체

**S3 키(`policy-terms/<uuid>`)로 하겠습니다.** `policy_documents.stored_file_path`와
같은 규칙이고, 컨테이너 로컬 경로는 재생성 시 소실되므로 후보가 아닙니다.

업로드 주체는 **백엔드가 presigned PUT URL을 발급해주시는 쪽**을 제안합니다.
1번에서 "AI 쪽에 AWS 자격증명은 필요 없습니다"로 정리하셨는데, 업로드만 직접
하려면 그 결정을 뒤집어야 합니다. 다운로드와 같은 방식으로 맞추면 권한 협의가
계속 없는 상태로 유지됩니다.

### 1-9. AI 컨테이너 메모리와 환경변수

```
메모리
  실측 피크         189MB   (분석 1건 처리 중)
  mem_limit         1g      (누수 시 JVM을 굶기지 않기 위한 상한)
  mem_reservation   256m

CPU (분석 1건 = 182초 동안, 14코어 기준)
  평균 0.1%   최대 6.5%
  대부분이 Upstage·OpenAI 응답 대기라 실제 연산이 거의 없음
```

상한을 1GB로 둔 이유는 저희가 새어나가도 JVM을 굶기지 않기 위해서입니다.
실측 피크의 5배 여유입니다.

```
환경변수 — 필수
  UPSTAGE_API_KEY      문서 파싱 · 임베딩 · 증권 분석
  UPSTAGE_AGENT_ID     증권 분석 에이전트. 비면 증권 경로가 꺼짐
  GOOGLE_API_KEY       답변 · 추출 (임시. 추후 OPENAI_API_KEY로 전환)
  SPRING_BASE_URL      http://polight-backend:8080
  INTERNAL_API_KEY     Spring과 동일한 값

환경변수 — 선택 (기본값 있음)
  ANSWER_PROVIDER      기본 openai-41 (현재 gemini-flash)
  EXTRACTION_PROVIDER  기본 openai-41 (현재 gemini-flash)
  EMBEDDING_PROVIDER   기본 upstage-1536
  DATABASE_URL         현재 비움 (아래 1-12)
  LOG_LEVEL            기본 INFO
```

`SPRING_BASE_URL`이 설정된 상태에서 `INTERNAL_API_KEY`가 없으면 **기동이
실패합니다.** 설정을 잊었는데 조용히 열려 있는 상태가 가장 위험해서 일부러
그렇게 두었습니다.

`SPRING_BASE_URL`은 `http://polight-backend:8080`으로 설정해두었습니다.
`http://backend:8080`도 같은 컨테이너로 해석되니, 둘 중 어느 쪽이 팀 표준인지
알려주시면 맞추겠습니다.

### 1-10. presigned 15분 — 충분합니다

저희가 다루는 약관 PDF가 2.4MB 수준이라 다운로드는 수 초입니다. 증권은 1~2페이지로
더 작습니다. 15분이면 여유가 큽니다.

다만 다운로드는 분석 시작 시점에 한 번만 하므로, **큐에 밀려 처리가 늦어지는 경우**만
주의하면 됩니다. 현재는 요청을 받는 즉시 백그라운드로 시작하므로 지연이 없습니다.

### 1-11. `NONE` 의미 변경 — 확인했습니다

저희 4단계를 "매칭 결과"로 재정의하고, `NONE`을 종점이 아니라 ②로 넘기는 신호로
쓰는 데 동의합니다. 문서를 그렇게 고치겠습니다.

`REVISION`이 MVP에서 발동하지 않는다는 것, 그럼에도 시행일은 기록해야 한다는 것도
확인했습니다(1-4).

### 1-12. 함께 알아두실 것 — 챗봇 근거 검색은 이관 전까지 동작하지 않습니다

백엔드 작업 순서 6단계(`rag/query` 호출)를 붙이실 때 필요한 정보입니다.

현재 약관은 AI 컨테이너의 파일로 들고 있는데, 이 청크들은 **공용 약관이라
`user_id`·`trip_id`·`document_id`가 모두 비어 있습니다.** 그런데 `RagQueryRequest`의
`tripId`는 필수 필드라 항상 값이 실려 오고, 저희 스코프 필터가 전부 걸러냅니다.

```
청크 2225건에 스코프 필터를 걸어본 결과

  scope 없음 (평가 스크립트 경로)      2225건 통과
  tripId만 (실제 요청 형태)               0건
  documentId까지 (실제 요청 형태)         0건
```

결과가 0건이면 정해진 문구로 답하므로 **에러도 로그도 남지 않습니다.**
`rag/query`를 붙였는데 계속 "약관에서 확인할 수 없습니다"만 나온다면 이것 때문이니,
연동 문제로 오해하지 않으시도록 미리 적어둡니다.

`policy_terms` 이관(1-7)이 끝나면 `termsId` 스코프로 검색되어 해결됩니다.
증권 분석과 헬스체크는 이 제약과 무관하게 정상 동작합니다.

---

## 2. 저희 쪽 후속 작업

2차 회신 8절의 "병렬로 진행 가능한 것"에 대한 답입니다.

| 작업 | 상태 |
| --- | --- |
| 벡터 거리 연산자 회신 (3-4) | **완료** |
| AI 컨테이너 메모리·환경변수 회신 (8번) | **완료** (1-9) |
| 콜백에 완전성 플래그 추가 (3-2) | 착수 가능 |
| 콜백에 `startDate`/`endDate` (3-1) | 에이전트 스키마 확인 후 |
| 약관 색인에 `effective_date` (3-3) | 착수 가능 |
| 검색이 두 소스를 보도록 (3-6) | 이관과 함께 |
| `/internal/terms/source` (3-5) | **범위 결정 후** (1-1) |

백엔드 1~2단계(검증 필터 + 콜백 수신, presigned URL)는 저희 쪽 대기 없이
진행하실 수 있습니다. AI 서버가 이미 떠 있어 1단계는 실제 컨테이너로 확인
가능합니다.

---

## 3. 회신 요청

| # | 내용 | 없으면 |
| --- | --- | --- |
| 1 | `/internal/terms/source` MVP 포함 여부 (1-1) | 착수 시점을 정할 수 없음 |
| 2 | `rag_service` 계정에 `policy_terms` INSERT 권한 (1-7) | 약관 8건 이관 불가 |
| 3 | 약관 원문 S3 업로드용 presigned PUT (1-8) | `stored_file_path`를 채울 수 없음 |
| 4 | `SPRING_BASE_URL` 표준 표기 (`backend` / `polight-backend`) | 현재 값으로 진행 |
| 5 | `policy_chunks`에 `document_id` 단독 인덱스 (1-5) | ③ 경로에서 스코프 필터가 인덱스를 못 씀 |

`INTERNAL_API_KEY` 로테이션 제안에 동의합니다. 값이 채팅 채널을 거친 것이 맞으니,
백엔드 1단계 착수 시점에 맞춰 새로 만들어 교환하는 것을 제안합니다.
같은 이유로 `KAKAO_REST_API_KEY`도 한 번 확인해보시기를 권합니다.
