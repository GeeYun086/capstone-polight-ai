# 검색 평가셋

모델이나 파서를 바꿀 때 "좋아졌다"를 숫자로 말하기 위한 고정 질문 세트다.
이게 없으면 개선 여부를 눈대중으로 판단하게 되고, 실제로 그렇게 하다가
정답 판정을 잘못한 적이 있다.

## 시작하기

```bash
cp data/eval/questions.example.json data/eval/questions.json
```

복사한 파일에 질문 20개를 채운다. 유형별 권장 배분은 다음과 같다.

| 유형 | 개수 | 예 |
|---|---|---|
| `coverage` | 8 | "항공편이 5시간 지연되면 보상되나요?" |
| `exclusion` | 5 | "임신·출산 치료비도 보상되나요?" |
| `procedure` | 4 | "병원비 청구하려면 뭐가 필요해요?" |
| `limit` | 3 | "휴대품 도난 한도가 얼마예요?" |

## 형식

```json
{
  "id": "Q01",
  "type": "exclusion",
  "question": "사용자가 실제로 물어볼 법한 문장",
  "gold": [
    { "page": 22, "contains": "약관 본문에 실제로 있는 문구" }
  ],
  "expected_points": ["답변에 반드시 들어가야 할 내용"]
}
```

- **`gold`는 여러 개 넣을 수 있다.** 그중 **하나라도** 검색되면 정답으로 친다.
- **`expected_points`는 검색 평가에는 쓰이지 않는다.** 나중에 응답 LLM을 비교할 때
  채점 기준으로 쓴다.

### ⚠️ `page`는 웬만하면 생략하라

같은 표준 조항이 보통약관과 여러 특약에 **반복 수록**되기 때문이다. 실제로 겪은 사례:

- "임신·출산" 면책 조항이 p5, p22, p23, **p64** 등에 반복돼 있다
- 검색은 p64 것을 찾아왔는데 `gold`에 p22만 적어둬서 **정상 동작을 실패로 오판**했다

`page`를 빼면 본문 문구만으로 판정하므로 이런 오판이 없다.
페이지를 특정할 수 있는 고유한 조항(예: 항공기 지연 특약)에만 `page`를 쓰는 게 안전하다.

## ⚠️ chunk_id로 라벨링하지 말 것

`chunk_id`는 청킹 방식이나 파서를 바꾸면 전부 달라진다. Upstage 파싱으로 교체하면
평가셋이 통째로 무효화된다. **페이지와 본문 문구는 같은 PDF를 쓰는 한 바뀌지 않으므로**
이 둘로 라벨링한다.

## 라벨 찾기

278개 청크를 눈으로 뒤질 필요 없다. 키워드로 후보를 찾아준다.

```bash
python scripts/eval_retrieval.py --find "임신"
```

출력에 나온 `page`와 본문 문구를 그대로 `gold`에 옮겨 적으면 된다.

## 평가 실행

```bash
# 기본 (MMR 적용)
python scripts/eval_retrieval.py

# MMR 효과 비교
python scripts/eval_retrieval.py --no-mmr

# 임베딩 모델 비교 (모델별로 임베딩을 따로 저장한 경우)
python scripts/eval_retrieval.py --embeddings-dir data/embeddings_large
```

Recall@1/3/5/8과 MRR, 그리고 실패한 질문 목록이 나온다.
