# 배포 안내

**Spring과 같은 EC2에 올리되, compose는 따로 둔다.**

```
[ EC2 ]
  ├─ polight-backend (Spring)      ─┐
  ├─ polight-postgres (pgvector)    ├─ docker network: polight_polight-network
  ├─ polight-ai      (우리)         ─┘
  └─ 우리만 compose 프로젝트가 다르다
```

DB는 RDS가 아니라 **같은 EC2의 `pgvector/pgvector:pg16` 컨테이너**다. 네트워크도
Spring compose가 이미 만들어 둔 것이 있어 **새로 만들 필요가 없다.** 우리는 거기
합류만 하므로 남의 컨테이너 설정을 건드리지 않는다.

## 왜 같은 호스트인가

처음에는 CPU 경합을 우려해 별도 EC2를 계획했으나, **실측해보니 근거가 없었다.**

```
분석 1건(182초) 동안의 우리 프로세스
  CPU     평균 0.1%  최대 6.5%  (14코어 기준)
  메모리   최대 189MB
```

182초 중 대부분이 Upstage·OpenAI **API 응답 대기**라 실제 연산이 거의 없다.
t3.medium(2 vCPU)으로 환산해도 평균 1% 미만이라 Spring이 느려질 일이 없다.

같은 호스트를 쓰면 **DB 컨테이너에 같은 도커 네트워크로 그대로 붙고**, 포트를
밖으로 열 필요도 없다. VPC·보안그룹을 맞추는 작업이 통째로 사라진다.

**단, 인스턴스 사양이 선행 조건이다.** 1GB 인스턴스에서는 여유 메모리가 200MB
남짓이라, 우리 컨테이너를 올리면 OOM 킬러가 가장 큰 프로세스인 **JVM(Spring)을
먼저 죽인다.** t3.medium(4GB) 이상에서만 올린다. 디스크도 8GB로는 빠듯해
20GB를 권한다. 디스크가 차도 Spring이 같이 죽는다.

## 왜 compose는 따로인가

같은 compose에 넣으면 **Spring을 배포할 때마다 우리 컨테이너도 재시작된다.**

분석은 3~4분간 프로세스 안에서 돈다(`BackgroundTasks`). 그 사이 재시작되면
작업이 통째로 날아가고, 콜백을 못 보내 `analysis_results`가 `PROCESSING`에
고착된다. **에러도 로그도 남지 않아 알아채기 어렵다.**

`docker network`만 공유하면 컨테이너끼리 이름으로 통신하면서 배포는 독립적이다.

## 백엔드에서 받아야 하는 것

| 항목 | 왜 필요한가 |
| --- | --- |
| **콜백 경로** | 우리가 정한 경로로 만들어 뒀다. 다르면 알려주면 환경변수로 맞춘다 |
| `INTERNAL_API_KEY` | **아직 Spring 쪽에 없다.** 새로 만들어 양쪽이 같은 값을 쓴다. 문서·커밋에 넣지 않고 별도 채널로 주고받는다 |
| DB 비밀번호 | `polight-postgres` 컨테이너 환경변수에 있다 |
| 인스턴스 상향 | t3.medium + EBS 20GB. 중지가 필요하니 트래픽 없는 시간에 |

**VPC ID도 RDS 보안그룹도 필요 없다.** DB가 같은 호스트의 컨테이너이기 때문이다.
`SPRING_BASE_URL`도 물어볼 필요 없이 `http://polight-backend:8080`으로 고정이다.

## 환경변수

`.env` 또는 컨테이너 환경변수로 넣는다.

```bash
# 필수
OPENAI_API_KEY=...            # 답변 생성, 보장항목 추출
UPSTAGE_API_KEY=...           # 문서 파싱, 임베딩, 증권 분석
UPSTAGE_AGENT_ID=agt_...      # 증권 분석 에이전트. 없으면 증권 경로가 꺼진다
SPRING_BASE_URL=http://polight-backend:8080
INTERNAL_API_KEY=...          # openssl rand -base64 32, Spring과 동일

# 선택 (기본값 있음)
UPSTAGE_AGENT_API_KEY=        # 에이전트가 다른 콘솔 계정에 있을 때만. 비우면 위 키를 쓴다
UPSTAGE_AGENT_CONFIG_ID=      # 에이전트 설정 버전 고정. 비우면 Studio의 최신
ANSWER_PROVIDER=openai-41     # 답변 모델
EXTRACTION_PROVIDER=openai-41 # 추출 모델. 실서비스 전 claude-opus로 올릴 예정
EMBEDDING_PROVIDER=upstage-1536
LOG_LEVEL=INFO

# 지금은 비워둔다 (아래 설명)
DATABASE_URL=
```

`SPRING_BASE_URL`이 설정된 상태에서 `INTERNAL_API_KEY`가 없으면 **기동이 실패한다.**
설정을 잊었는데 조용히 열려 있는 상태가 가장 위험하기 때문에 일부러 그렇게 했다.

`UPSTAGE_AGENT_ID`가 비어 있으면 증권 분석만 실패하고 약관 경로는 정상 동작한다.
증권을 올렸는데 실패 콜백이 오면 이 값부터 확인한다.

### DATABASE_URL을 지금 채우면 챗봇이 답을 못 한다

값이 있으면 저장소가 pgvector로 바뀌고 검색이 `policy_chunks`를 본다.
그런데 **약관은 거기 없다.** 우리가 미리 색인해 파일로 들고 있기 때문이다.

```
DATABASE_URL 비움   ->  파일 저장소     스코프가 맞으면 검색됨
DATABASE_URL 채움   ->  policy_chunks  약관 0건 (아직 비어 있다)
```

**단, 비워둔다고 바로 검색되는 것은 아니다.** 아래 "스코프 제약"을 함께 읽는다.

`policy_chunks`는 user_id / analysis_result_id / document_id가 NOT NULL이라
주인 없는 공유 약관을 넣을 수 없다. 백엔드에 요청한 `policy_terms` 테이블이 생기고
저장소가 그쪽을 보게 된 뒤에 채운다. `docs/BACKEND_INTERFACE.md` 3-2 참고.

증권 경로는 DB를 쓰지 않으므로 이 설정과 무관하다.

## 실행

**사전 준비는 없다.** Spring compose가 만든 `polight_polight-network`에 합류하므로
네트워크를 만들 필요도, 백엔드 compose를 고칠 필요도 없다.

```bash
DOCKER_BUILDKIT=0 docker build -t polight-ai:latest .
docker compose -f docker-compose.prod.yml up -d
```

**`up -d --build`는 이 서버에서 쓸 수 없다.** compose v5는 빌드에 buildx 0.17 이상을
요구하는데 설치돼 있지 않고, `docker-buildx-plugin` 패키지도 저장소에 없다. 백엔드는
ECR에서 완성된 이미지를 받아 쓰기 때문에 서버에서 빌드할 일이 없어 깔리지 않은 것이다.

그래서 예전 빌더로 이미지를 먼저 만들고 compose는 그것을 가져다 쓰게 한다.
compose 파일에 `image: polight-ai:latest`가 적혀 있어 `--build` 없이 실행하면
방금 만든 이미지를 그대로 쓴다.

백엔드가 `docker compose down`을 하면 우리 컨테이너가 붙어 있어 네트워크 삭제에
실패했다는 경고가 뜬다. 무해하다. 네트워크는 남고 양쪽 다 정상 동작한다.

Spring을 배포해도 이 컨테이너는 건드려지지 않는다. 반대도 마찬가지다.

## 약관 색인 데이터를 넣는다 (첫 배포에 반드시)

**이걸 빼면 컨테이너는 정상 기동하는데 챗봇이 "약관에서 확인할 수 없습니다"만 답한다.**
에러도 로그도 남지 않아 알아채기 어렵다.

약관은 사용자가 올리지 않는다. 우리가 미리 분석해 파일로 들고 있고, 그 파일은
이미지에 들어가지 않는다. `.dockerignore`가 `data/`를 빼기 때문이다(수백 MB라
이미지에 구울 수 없다). 컨테이너의 `/app/data`는 `ai-data` 볼륨이고 **처음에는 비어 있다.**

로컬에서 색인한 결과를 복사해 넣는다.

```bash
# 로컬에서 (아직 색인 안 했다면)
python scripts/ingest_terms.py

# 서버로 옮긴 뒤
docker cp data/chunks      polight-ai:/app/data/
docker cp data/embeddings  polight-ai:/app/data/
docker restart polight-ai
```

재시작이 필요한 이유: 검색 저장소가 임베딩 전체를 한 번만 메모리에 올린다.
파일만 넣고 재시작하지 않으면 반영되지 않는다.

확인:

```bash
docker exec polight-ai ls /app/data/chunks
docker exec polight-ai ls /app/data/embeddings
```

두 목록의 개수가 같아야 한다. 임베딩이 빠진 약관은 검색되지 않는다.

### 스코프 제약 — 넣어도 아직 검색되지 않는다

파일을 넣고 재시작해도 **Spring이 보내는 실제 요청으로는 0건이 나온다.** 배포 후
실측한 내용이다.

```
청크 2225건에 스코프 필터를 걸어본 결과

  scope 없음 (평가 스크립트 경로)        2225건 통과
  trip_id만 (Spring 실제 요청)              0건
  document_id까지 (Spring 실제 요청)        0건
```

`ingest_terms.py`가 만든 청크는 `user_id`·`trip_id`·`document_id`가 모두 비어 있다.
공유 약관이라 주인이 없기 때문이다. 그런데 `RagQueryRequest.trip_id`는 필수 필드라
Spring은 항상 값을 보내고, `_matches`가 `chunk["trip_id"] != scope.trip_id`로
전부 걸러낸다. 결과가 0건이면 정해진 문구로 답하므로 **에러도 로그도 남지 않는다.**

즉 현재 챗봇 근거 검색은 다음 중 하나가 되어야 동작한다.

- 백엔드가 약관 분석을 돌려 `policy_chunks`가 채워지고 `DATABASE_URL`을 연결한다
  (정상 경로로 들어온 청크는 스코프가 NOT NULL이라 문제가 없다)
- `policy_terms` 공유 테이블이 생기고 저장소가 그쪽을 본다
- 파일 저장소에서 스코프가 빈 청크를 공유 약관으로 보고 통과시킨다 (코드 수정)

증권 분석과 헬스체크는 이 제약과 무관하게 정상 동작한다.

`data/parsed_results`(파싱 캐시)는 옮기지 않아도 된다. 재분석할 때만 쓰이고
없으면 다시 파싱할 뿐이다. `data/raw_pdfs`도 옮기지 않는다. **증권 PDF가 섞여
들어가지 않도록 주의한다** — 개인정보다.

새 보험사 약관을 추가할 때도 같은 절차다. 로컬에서 `ingest_terms.py`를 돌리고
`config/terms_registry.json`에 등록한 뒤 복사해 넣는다. 등록을 빠뜨리면 증권에서
그 약관을 찾지 못한다. 레지스트리는 이미지에 들어가므로 **재빌드가 필요하다.**

## 서로 부르는 주소

컨테이너 이름으로 통신한다. IP를 알 필요가 없다.

```
Spring -> AI      http://polight-ai:8000/internal/rag/query
AI -> Spring      SPRING_BASE_URL=http://polight-backend:8080
AI -> DB          postgresql://polight:...@postgres:5432/polight
```

DB 호스트가 `polight-postgres`가 아니라 `postgres`인 것은 compose 서비스명 별칭이라
그렇다. Spring도 같은 주소를 쓰고 있다.

## 포트를 열지 않는다

`ports`가 아니라 `expose`를 쓴다. 같은 네트워크의 컨테이너만 접근하면 되므로
호스트에도, 외부에도 노출할 이유가 없다. **보안그룹으로 막는 것보다 아예 열지
않는 편이 확실하다.**

검증한 내용이다.

```
호스트에서 localhost:8000        연결 안 됨
같은 네트워크의 다른 컨테이너      http://polight-ai:8000/health -> 200
키 없이 /internal                401
맞는 키                          200
```

## 엔드포인트

| 경로 | 인증 | 용도 |
| --- | --- | --- |
| `GET /health` | 없음 | 로드밸런서·EC2 상태 확인 |
| `POST /internal/analysis` | 필요 | **증권** 분석 요청 (202 즉시 응답, 비동기 처리) |
| `POST /internal/rag/query` | 필요 | 챗봇 질의 |

`/health`만 열려 있다. 헬스체크가 키를 실어 보내지 않기 때문이다.

`/internal/analysis`는 약관도 받는다. `documentType`으로 갈리고, 없으면 페이지
수로 판별한다(10페이지 이하는 증권). 다만 **운영에서 들어오는 것은 증권뿐이다.**
약관은 사용자가 올리지 않고 우리가 미리 넣는다. 요청·응답 형식은
`docs/BACKEND_INTERFACE.md`에 정리해 뒀다.

## 알아둘 것

**증권 분석은 수십 초로 끝난다.** 1~2페이지라 파싱이 짧고, 담보는 Upstage Studio
에이전트가 뽑으므로 우리 쪽 LLM 호출이 없다. 폴링 제한은 180초다.

**약관 분석은 1건에 4~5분 걸린다.** 파싱·임베딩·추출을 모두 하기 때문이다.
운영 경로는 아니지만 이 요청이 들어오면 **Spring 쪽 콜백 타임아웃을 넉넉히** 잡아야 한다.

**증권 파일은 분석이 끝나면 지운다.** 피보험자 이름·생년월일·증권번호가 들어 있어
Upstage 서버와 로컬 디스크 양쪽에서 제거한다. 약관은 재분석에 대비해 남긴다.

**분석은 응답을 보낸 뒤에도 계속 돈다**(BackgroundTasks). 요청이 끝나면 컨테이너를
정리하는 서버리스 환경에 그대로 올리면 안 된다. EC2 상주 프로세스를 전제로 한다.

**이미지가 1GB 정도다.** pymupdf와 numpy 계열이 대부분이다. 빌드 시간이 아깝다면
`requirements.txt`가 바뀌지 않는 한 레이어 캐시가 재사용된다.

**EC2 권한 승인이 늦어지면** `ngrok`으로 터널을 열어 배포 없이 연동 테스트를
먼저 할 수 있다. 단 **DB 접속은 터널로 해결되지 않으므로** 그때까지는 로컬
pgvector(`docker compose up -d`)로 개발한다.

## 배포 체크리스트

순서대로 확인한다. 위 세 개를 빠뜨리면 컨테이너는 멀쩡히 뜨는데 기능이 조용히 죽는다.

- [ ] `.env`에 `UPSTAGE_AGENT_ID` — 없으면 증권 분석이 전부 실패
- [ ] `.env`의 `DATABASE_URL`이 **비어 있는지** — 채우면 챗봇이 약관을 못 찾음
- [ ] `data/chunks`·`data/embeddings`를 볼륨에 복사 후 재시작 — 없으면 챗봇 근거 0건
- [ ] `INTERNAL_API_KEY`가 Spring과 같은 값
- [ ] `config/terms_registry.json`에 약관이 다 등록됐는지 (이미지에 포함되므로 재빌드 필요)
- [ ] `GET /health` 200
- [ ] 증권 1건을 실제로 올려 콜백까지 확인

마지막 항목은 `scripts/mock_spring.py`로 로컬에서 먼저 해볼 수 있다.
8081로 띄우고 `SPRING_BASE_URL`을 거기로 돌리면 Spring 없이 콜백을 확인할 수 있다.

## 검증된 것

로컬에서 이미지를 빌드해 확인한 내용이다.

```
빌드          성공
기동          6초 이내
GET /health   200
인증          키 없이 401
config        terms_registry.json 등 4개 포함됨
data/         비어 있음 (볼륨. 위 절차대로 채워야 한다)
```

증권 경로는 이미지 밖에서 확인했다.

```
증권 PDF   POST /internal/analysis  ->  202
           Upstage Studio Agent     ->  담보 21건, 금액 21/21
           콜백 수신 (mock_spring)   ->  insurerName / productName 포함
           업로드 파일·로컬 PDF 삭제
챗봇       캐롯 약관 310청크에서 근거 9건, 답변 생성
```
