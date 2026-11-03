# 코드 리뷰 결과: 저장소 전체

- 검토일: 2026-11-03
- 기준 커밋: `f2ebf4a` (main)
- 검토자: code-reviewer 에이전트 (읽기 전용, 코드 미실행)
- 범위: `src/` 전체, 설정 파일, `requirements.txt`, `.gitignore`, PRD 대조
- 제외: `.env`, `.streamlit/secrets.toml` (비밀 키 보호를 위해 열지 않음), `docs/`의 PDF, 스크린샷 이미지

높음 4건 / 중간 12건 / 낮음 13건. 높음 1·2번은 소스에서 직접 재확인했다. 나머지는 코드를 읽고 판단한 결과이며 실행으로 검증하지 않았다. 확신이 낮은 항목은 "확인 필요"로 표시했다.

---

## 비밀 키 점검 (문제 없음)

- `OPENROUTER_API_KEY`는 `src/app.py:101-108`의 `get_api_key()`(환경 변수 → `st.secrets`)로만 읽는다. `DATABASE_URL`도 `src/db.py:16-23`의 `_database_url()`에서 같은 순서로 읽는다.
- 소스에 하드코딩된 키가 없고, 키를 출력하거나 로그로 남기는 코드도 없다.
- 에러 표시에 쓰는 `response.text`와 `str(e)`에는 키가 들어가지 않는다. 키는 요청 헤더에만 들어간다.
- `.gitignore`에 `.env`, `.env.*`(`!.env.example` 예외), `.streamlit/secrets.toml`이 모두 있다. `.git/index`에 `secrets.toml`은 등록되어 있지 않다.
- 단, DB 연결 실패 시 `init_db()` 예외가 그대로 화면에 노출될 수 있다(낮음 6번).

---

## 심각도: 높음

### 1. 타임아웃·연결 오류 시 모델 폴백이 중단됨 (재확인함)
- 위치: `src/vision.py:182`, `src/recipe.py:181`
- 상황: `requests.post(timeout=60)`가 try 블록 밖에 있어 `Timeout`/`ConnectionError`가 루프를 빠져나가 호출부(`src/app.py:318-325`, `483-494`)까지 올라간다.
- 결과: 첫 모델이 60초 타임아웃으로 실패하면 나머지 모델을 시도하지 않는다. 무료 모델에서 가장 흔한 실패 유형에서 폴백이 작동하지 않는다.
- 제안: 루프 안에서 `requests.exceptions.RequestException`을 잡아 다음 모델로 넘어가고, 마지막 예외를 기록해 호출부에 전달한다. 타임아웃은 상수로 빼고 전체 소요 상한을 둔다(현재 최악 약 3분).

### 2. `content`가 null이거나 형식이 다르면 앱이 죽음 (재확인함)
- 위치: `src/vision.py:188`, `src/recipe.py:188`
- 상황: 추론형 모델이나 빈 응답에서 `choices[0]["message"]["content"]`가 `None`이면 `parse_ingredients(None)`/`parse_recipes(None)`이 `re.search`에서 `TypeError`를 던진다. `message` 키가 없으면 `KeyError`.
- 결과: 루프는 `except ValueError`만 잡으므로 트레이스백이 사용자에게 보인다. `vision.py:51-56` 주석에 빈 응답 실측 기록이 있어 실제로 발생하는 상황이다.
- 제안: `content`가 문자열인지 확인 후 파싱하고, `(KeyError, IndexError, TypeError, ValueError)`를 함께 잡아 다음 모델로 진행한다.

### 3. 200 응답이 JSON이 아닐 때 `response.json()`이 가드 없이 재호출됨
- 위치: `src/app.py:339`, `src/app.py:516`
- 상황: 프록시·게이트웨이가 HTML 본문에 200을 반환하면 폴백 루프는 `ValueError`를 삼키고 끝나지만, 호출부에서 다시 `response.json()`을 불러 처리되지 않은 `JSONDecodeError`가 난다.
- 제안: 루프에서 파싱 결과(성공 여부, 원인, 마지막 응답)를 구조화해 반환하고 호출부는 재파싱하지 않는다. 최소한 `try/except ValueError`로 감싼다.

### 4. 실패 원인이 마지막 응답 하나로만 보고되어 부정확함
- 위치: `src/vision.py:157-195`, `src/recipe.py:158-196`, `src/app.py:330-348`, `499-530`
- 상황 1: 1·2차가 빈 응답/파싱 실패이고 3차만 429이면 `app.py:73`이 "무료 모델 3곳이 모두 혼잡합니다 (429)"라고 안내한다. 반대로 3차가 500이면 앞선 429가 보이지 않는다.
- 상황 2: 사용자가 모델을 1~2개만 선택해도 `app.py:348`, `530`은 "서로 다른 모델 3차 시도 모두", `app.py:73`은 "3곳"으로 하드코딩되어 있다.
- 상황 3: 401(키 오류), 402(크레딧), 403, 404(모델 삭제)가 모두 "요청이 실패했습니다 (status N)"로만 표시된다. 비전 쪽 5xx는 기술 정보도 저장하지 않는다(`app.py:333-334`).
- 제안: 시도별 `(model, 상태코드/오류유형)`을 기록해 반환하고 요약("1차 timeout, 2차 429, 3차 파싱 실패")을 expander에 표시한다. 시도 횟수 문구는 실제 값을 쓰고 401/402는 별도 안내한다.

> 높음 4건은 모두 OpenRouter 호출 경로에 있어, 시도별 결과를 기록해 반환하도록 바꾸면 함께 해결된다.

---

## 심각도: 중간

### 5. 재시도해도 소용없는 오류에도 모델 3개를 끝까지 호출함
- 위치: `src/vision.py:192-194`, `src/recipe.py:193-195`
- 상황: 401, 402, 일일 한도 429(`free-models-per-day`)는 모든 모델에서 똑같이 실패하는데도 2초+4초를 기다리며 3번 호출한다.
- 제안: 401/402/403과 일일 한도는 즉시 중단한다. `rate_limit_message`의 일일 한도 판별 로직을 공용으로 뺀다.

### 6. 모델이 준 레시피 항목의 타입을 검증하지 않음
- 위치: `src/recipe.py:202-207`, `src/app.py:545-577`, `src/pages/1_내_레시피.py:29`
- 상황: `recipes`에 dict가 아닌 항목이 있으면 `_is_korean_enough`의 `r.get`에서 `AttributeError`. `steps`/`used_ingredients`에 비문자열이 있으면 `join`에서 `TypeError`. `steps`가 문자열이면 글자 단위로 나뉘어 표시된다.
- 결과: DB에 한 번 저장되면 "내 레시피" 페이지에서도 같은 오류가 반복된다.
- 제안: `parse_recipes`에서 정규화한다. dict가 아닌 항목 제외, 필드는 문자열 리스트로 변환, `cook_time_minutes`는 정수 또는 None.

### 7. 레시피 저장에 예외 처리와 중복 방지가 없음
- 위치: `src/app.py:584`, `src/db.py:224-240`
- 상황: `cook_time_minutes`가 "15~20" 같은 문자열이면 INTEGER 컬럼에서 `DataError`(확인 필요). "레시피 저장" 버튼을 반복해 누르면 같은 레시피가 계속 쌓인다. `1_내_레시피.py:28`은 값이 None이면 "약 None분"으로 표시된다.
- 제안: 저장 전 정수 변환, try/except로 한국어 오류 안내, 같은 사용자·같은 레시피 중복 저장 방지.

### 8. 이미지 변환 오류 미처리, EXIF 회전 미적용
- 위치: `src/app.py:306`, `src/vision.py:128`
- 상황: 손상된 파일이면 `to_resized_data_uri`가 `PIL.UnidentifiedImageError`를 던져 트레이스백이 노출된다. `ImageOps.exif_transpose`를 적용하지 않아 스마트폰 세로 사진이 회전된 채 전달된다(인식률 영향은 확인 필요).
- 제안: try/except로 "이미지를 열 수 없습니다" 안내, `exif_transpose` 추가.

### 9. 72바이트를 넘는 비밀번호에서 bcrypt 예외 가능
- 위치: `src/auth.py:13`, `src/auth.py:17`
- 상황: `requirements.txt`에서 `bcrypt` 버전이 고정되어 있지 않다. bcrypt 5.x는 72바이트 초과 입력에 `ValueError`를 낸다. 한글은 글자당 3바이트라 25자만 넘어도 해당된다.
- 결과: 회원가입·로그인이 트레이스백으로 끝난다.
- 제안: `signup`에서 바이트 기준 최대 길이 검증, `verify_password`는 `ValueError`를 잡아 False 처리, 의존성 버전 고정.

### 10. 커넥션 풀 고갈과 풀 폐기 동시성 문제
- 위치: `src/db.py:59`, `src/db.py:66-69`
- 상황 1: `ThreadedConnectionPool(1, 5)`의 `getconn()`은 6번째 동시 요청에서 `PoolError("connection pool exhausted")`를 던지며, 이 호출이 try 밖이다.
- 상황 2: 죽은 커넥션을 발견하면 `pool.closeall()`로 풀 전체를 닫아, 다른 스레드의 `putconn`에서 `PoolError("connection pool is closed")`가 난다.
- 제안: `maxconn` 증가 또는 고갈 시 짧은 재시도, 풀 폐기를 락으로 보호, 이전 풀의 `putconn` 오류 무시. (psycopg2 동작 지식 기반, 실행 미확인)

### 11. 재료 이름을 이스케이프하지 않고 HTML로 렌더링
- 위치: `src/app.py:419`
- 상황: 재료 이름은 사용자 입력이거나 AI 출력(사진 속 글자에 의한 프롬프트 인젝션 포함)인데 `unsafe_allow_html=True`로 그대로 표시한다.
- 제안: `html.escape(name)` 적용. `auth_ui.py:79`의 닉네임도 마크다운으로 해석된다(낮음).

### 12. 로그아웃 시 초기화하는 키 목록이 서로 어긋남
- 위치: `src/auth_ui.py:10-26`, `src/app.py:37-53`
- 상황: `used_recipe_model`이 `APP_FLOW_STATE_KEYS`에는 없고 `RESET_STATE_KEYS`에만 있어, 이메일 계정 로그아웃 후 이전 사용자의 "실제 응답 모델" 캡션이 남는다. 재료 위젯 키(`ingredient_*`)도 남는다.
- 제안: 키 목록을 한 모듈의 상수로 두고 양쪽에서 import. 위젯 키 접두사(`ingredient_`, `delete_`)도 함께 정리.

### 13. Google 로그인이 이메일 검증 없이 기존 계정에 자동 연결됨
- 위치: `src/auth_ui.py:45`, `src/db.py:164-170`
- 상황: 이메일/비밀번호 가입에 소유 확인이 없어, 누군가 피해자 이메일로 먼저 가입하면 피해자가 Google로 로그인할 때 같은 계정에 연결된다. 영향 데이터는 레시피 수준이라 위험도는 중간~낮음.
- 추가: Google 이메일을 소문자로 정규화하지 않아 중복 계정이나 UNIQUE 위반이 날 수 있다.
- 제안: 이메일 정규화(필수), `email_verified` 확인, 가입 시 이메일 확인 절차 검토.

### 14. 단위가 다른 같은 이름 재료를 수량만 더해 합침
- 위치: `src/vision.py:198-212`
- 상황: "우유 1 팩"과 "우유 500 ml"이 "우유 501 팩"이 된다. `app.py:192`가 매 리런마다 실행하므로 사용자가 이름을 고치는 순간 조용히 병합된다.
- 제안: 이름과 단위가 모두 같을 때만 합친다.

### 15. 모델 목록 필터가 부족함
- 위치: `src/vision.py:79`, `src/recipe.py:61-67`
- 상황: 레시피 후보는 `:free`로 끝나고 제외 키워드(safety/rerank/guard)만 아니면 모두 포함된다. 텍스트 출력이 아닌 모델이 섞이면 실패한다(실제 목록에 있는지 확인 필요).
- 제안: `architecture.output_modalities`에 "text"가 있는지 확인. 비전 필터도 동일.

### 16. 모델 선택을 표시 이름으로 id에 매핑함
- 위치: `src/pages/2_프로필.py:112-118`
- 상황: 표시 이름이 같은 모델이 둘이면 `name_to_id` 딕셔너리가 덮여 엉뚱한 id가 저장된다. 또한 `2_프로필.py:75`의 "무작위로 시도합니다"는 실제 동작(우선순위 순서 뒤에 무작위)과 다르다.
- 제안: 행 위치로 매핑하거나 숨김 id 컬럼 사용.

---

## 심각도: 낮음

1. `src/db.py:38-48` — `connect_timeout`이 없어 첫 연결에서 오래 매달릴 수 있다. `tcp_user_timeout`은 Linux 전용이라 Windows에서는 효과가 없다(확인 필요).
2. `src/db.py:51-71` — 모든 쿼리 앞에 `SELECT 1`이 한 번 더 나간다. 오래 유휴였던 경우에만 점검하도록 줄일 수 있다.
3. `src/db.py:101` — `init_db()`가 시작할 때마다 `ALTER TABLE ... DROP NOT NULL`을 실행한다. 이미 적용된 변경이며 테이블 락이 걸린다.
4. `src/auth.py:31-34` — `email_exists`와 `create_user` 사이 경쟁 조건. 동시 가입 시 UNIQUE 위반이 처리되지 않는다.
5. `src/auth.py:44-47` — 존재하지 않는 이메일은 bcrypt 검증을 건너뛰어 응답 시간으로 계정 존재를 추측할 수 있다. 로그인 시도 횟수 제한도 없다.
6. `src/auth_ui.py:6` — `db.init_db()`가 import 시점에 예외 처리 없이 실행된다. DB가 내려가 있으면 호스트·사용자 정보가 담긴 psycopg2 오류가 화면에 나온다.
7. `src/auth_ui.py:82-84` — Google 로그아웃에서 `st.logout()` 후 `session_state.current_user`가 남아 있을 가능성(확인 필요).
8. `src/pages/2_프로필.py:46-47` — `get_user_by_id`가 None이면 `saved.get`에서 `AttributeError`.
9. `src/app.py:416-421`, `src/recipe.py:112` — 수량·단위를 입력받지만 프롬프트에는 이름만 들어간다. 수량 0인 항목도 포함된다.
10. `src/app.py:545` — 모델이 레시피를 5개 이상 주면 열이 그만큼 좁아진다. PRD(레시피 3개)에 맞춰 상한을 둔다.
11. 중복 코드 — `_fetch_models_payload`, `prioritize_models`, `_EXCLUDE_KEYWORDS`, `API_URL`/`MODELS_URL`, 폴백 루프, `list_*_detailed`가 `vision.py`와 `recipe.py`에 복제되어 있고 캐시도 따로라 모델 목록 호출이 두 번 나간다. 매직 넘버(`timeout=60`, `attempts=3`, `2 * (i + 1)` 등)도 상수로 뺀다.
12. `src/app.py` — 약 585줄이고 모듈 레벨 코드가 길다. 1·2·3단계 화면을 함수로 분리한다. `app.py:330-348`과 `499-530`의 상태코드 분기는 거의 같은 구조다.
13. `src/test_api.py` — import 시점에 키를 읽고, 모델이 앱의 실제 폴백 풀과 다르게 고정되어 있어 API 검증 용도로서 가치가 줄었다.

---

## PRD / CLAUDE.md 정합성

- **PRD_step1 F1-2, PRD_step2 F2-2**: 모델을 `google/gemma-4-31b-it:free`, `openai/gpt-oss-20b:free`로 고정하라고 되어 있으나 구현은 무료 모델 목록을 동적으로 조회한다. PRD 갱신 필요.
- **PRD_step1 5.3**: 출력 스키마가 문자열 배열인데 구현은 `{_id, name, quantity, unit}` 객체다.
- **PRD_step1 7**: 429/5xx 실패 시 "재시도 버튼"이 필요하지만 식재료 인식 화면에는 재시도 전용 버튼이 없다. 레시피 화면은 "다시 추천받기"로 충족한다.
- **PRD_step3 6**: 세션을 JWT/쿠키로 관리하라고 되어 있으나 이메일 로그인은 `st.session_state`에만 있어 새로고침 시 로그아웃된다. PRD_step3 10은 소셜 로그인을 범위 외로 규정하지만 구현되어 있다. 저장소 예시는 SQLite인데 실제는 Postgres(Supabase)다.
- **PRD_step3 7 / PRD_step2 6.4**: 저장 완료는 토스트 메시지여야 하는데 `st.success`를 쓴다(사소함).
- **PRD_step2 7**: 쿨다운 5초는 구현되어 있지만(`app.py:33`) 요청 시작 시각 기준이라 생성에 10초 이상 걸리면 사실상 의미가 없다.
- **PRD_step3 8**: `list_recipes`/`delete_recipe`의 `user_id` 조건, bcrypt 해시, 파라미터 바인딩 SQL — 충족.
- **CLAUDE.md**: "src/ — 현재는 test_api.py만 있다"와 실행 명령 `python3 src/test_api.py`는 오래된 내용이다. 의존성 설치(`pip install -r requirements.txt`), 실행(`streamlit run src/app.py`), PRD 단계와 모듈의 대응이 반영되지 않았다.

---

## 검토한 파일

- `src/app.py`, `src/vision.py`, `src/recipe.py`, `src/db.py`, `src/auth.py`, `src/auth_ui.py`, `src/test_api.py`
- `src/pages/1_내_레시피.py`, `src/pages/2_프로필.py`
- `.streamlit/config.toml`, `.gitignore`, `requirements.txt`, `.env.example`, `.claude/launch.json`, `CLAUDE.md`
- `docs/PRD_step1.md`, `docs/PRD_step2.md`, `docs/PRD_step3.md`
- `.git/index`는 파일명 검색으로만 확인
