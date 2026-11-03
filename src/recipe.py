import json
import random
import re
import time

import requests

from openrouter import API_URL, REQUEST_TIMEOUT_SECONDS, fetch_models_payload, should_stop_retrying

CUISINE_OPTIONS = ["상관없음", "한식", "양식", "중식", "일식"]
DIFFICULTY_OPTIONS = ["상관없음", "초급", "중급", "고급"]
TIME_OPTIONS = ["상관없음", "15분 이내", "30분 이내", "30분 이상"]
SERVINGS_OPTIONS = ["상관없음", "1인분", "2인분", "3인분", "4인분"]

# 오픈라우터 무료 모델 목록 조회 자체가 실패할 경우를 위한 최소 안전망 (2026-10-31 동작 확인).
SAFETY_NET_RECIPE_MODELS = [
    "nvidia/nemotron-3.5-super-vl:free",
    "cohere/north-mini-code:free",
]
_EXCLUDE_KEYWORDS = ["safety", "rerank", "guard"]

# 2026-10-31 재료 7종으로 다시 실측한 우선순위. 앞쪽일수록 먼저 시도한다.
# minimax-m3(2026-10-22)와 nemotron-3-super(2026-10-30)는 무료 목록에서 빠졌다.
# 이미지 입력 모델도 텍스트 전용 프롬프트에 정상 응답하므로 후보에서 제외하지 않는다.
#   nemotron-3.5-super-vl  성공 2/2, 레시피 3개, 조리 3.8단계, 10~15초. 가장 빠름
#   nemotron-3-ultra       성공 2/2, 레시피 3개, 조리 5.5단계, 25~39초. 조리 순서가 가장 상세
#                          (2026-10-16 녹화 중 5분 넘게 응답하지 않은 적이 있어 2순위에 둠)
#   north-mini-code        성공 2/2, 레시피 3개, 조리 3.0단계, 15~62초
# 목록에 없는 모델은 이 뒤에 무작위 순서로 붙는다 (새로 등장한 무료 모델도 기회를 얻도록).
PREFERRED_RECIPE_MODELS = [
    "nvidia/nemotron-3.5-super-vl:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "cohere/north-mini-code:free",
]

def _fetch_free_text_model_entries():
    """오픈라우터에서 레시피 생성에 쓸 수 있는 무료 모델의 원본 정보를 조회한다(5분 캐시).

    이미지 입력을 함께 받는 모델도 텍스트 프롬프트에 정상 응답하므로 제외하지 않는다.
    """
    try:
        data = fetch_models_payload()
    except (requests.exceptions.RequestException, ValueError):
        return []

    entries = []
    for m in data:
        model_id = m.get("id", "")
        if not model_id.endswith(":free"):
            continue
        if any(keyword in model_id.lower() for keyword in _EXCLUDE_KEYWORDS):
            continue
        entries.append(m)
    return entries


def list_free_text_models(allowed_models=None):
    """현재 사용 가능한 무료 텍스트 모델 id 목록. allowed_models가 주어지면 그 안에서만 고른다.

    allowed_models로 걸러낸 결과가 비어있으면(사용자가 골라둔 모델이 전부 사라진 경우)
    안전하게 전체 목록으로 되돌아간다.
    """
    entries = _fetch_free_text_model_entries()
    models = [m.get("id", "") for m in entries] or list(SAFETY_NET_RECIPE_MODELS)
    if allowed_models:
        filtered = [m for m in models if m in allowed_models]
        if filtered:
            return filtered
    return models


def prioritize_models(pool):
    """실측 우선순위(PREFERRED_RECIPE_MODELS) 순서로 후보를 정렬한다.

    우선순위 목록에 없는 모델은 뒤쪽에 무작위 순서로 배치한다. 무료 모델 라인업은
    수시로 바뀌므로, 아직 실측하지 않은 새 모델도 뒤에서는 시도될 수 있게 남겨둔다.
    """
    ranked = [m for m in PREFERRED_RECIPE_MODELS if m in pool]
    rest = [m for m in pool if m not in ranked]
    random.shuffle(rest)
    return ranked + rest


def list_free_text_models_detailed():
    """프로필 화면의 모델 선택 UI에 쓸 상세 정보(id/name/context_length/created)를 반환."""
    entries = _fetch_free_text_model_entries()
    return [
        {
            "id": m.get("id", ""),
            "name": m.get("name") or m.get("id", ""),
            "context_length": m.get("context_length"),
            "created": m.get("created"),
        }
        for m in entries
    ]


def build_prompt(ingredient_names, cuisine=None, difficulty=None, time_pref=None, servings=None):
    ingredients_text = ", ".join(ingredient_names)
    conditions = []
    if cuisine and cuisine != "상관없음":
        conditions.append(f"요리 종류: {cuisine}")
    if difficulty and difficulty != "상관없음":
        conditions.append(f"난이도: {difficulty}")
    if time_pref and time_pref != "상관없음":
        conditions.append(f"조리 시간: {time_pref}")
    if servings and servings != "상관없음":
        conditions.append(f"인원: {servings}")
    condition_text = ""
    if conditions:
        condition_text = "다음 조건에 맞춰줘: " + ", ".join(conditions) + ".\n"

    return (
        f"다음 재료로 만들 수 있는 요리 3가지를 추천해줘: {ingredients_text}.\n"
        f"{condition_text}"
        "요리명, 조리 순서 등 모든 텍스트는 반드시 한국어로만 작성해줘. 영어를 섞지 마.\n"
        "각 레시피는 아래 JSON 형식으로만 답해줘.\n"
        "{\n"
        '  "recipes": [\n'
        "    {\n"
        '      "name": "요리명",\n'
        '      "used_ingredients": ["보유 재료 중 사용되는 것"],\n'
        '      "missing_ingredients": ["추가로 필요한 재료"],\n'
        '      "steps": ["조리 순서 1", "조리 순서 2"],\n'
        '      "cook_time_minutes": 20\n'
        "    }\n"
        "  ]\n"
        "}"
    )


def call_recipe_model(model, ingredient_names, api_key, **options):
    return requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": build_prompt(ingredient_names, **options)}],
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def generate_recipes(ingredient_names, api_key, on_attempt=None, attempts=3, allowed_models=None, **options):
    """매 시도마다 오픈라우터의 현재 무료 텍스트 모델 중 서로 다른 모델을 순서대로 시도한다.

    실측 우선순위(PREFERRED_RECIPE_MODELS)가 앞에 오고, 아직 실측하지 않은 모델이
    무작위 순서로 뒤를 잇는다. 429든 JSON 파싱 실패든 한국어가 아닌 응답이든 실패로
    간주하고, 성공할 때까지 또는 attempts 횟수만큼 다음 모델로 넘어간다 (별도의 고정
    폴백 모델 없이, 매 시도 자체가 폴백 역할을 겸함).
    allowed_models: 사용자가 프로필에서 선택한 모델 id 목록. 없으면 전체 무료 모델을 대상으로 한다.
    """
    pool = prioritize_models(list_free_text_models(allowed_models=allowed_models))
    if pool:
        # 풀이 attempts보다 적으면(예: 사용자가 모델을 1~2개만 선택한 경우) 안전망 모델로
        # 채우지 않고 풀 안에서 순환한다 - 사용자가 고르지 않은 모델을 몰래 끼워넣지 않기 위함.
        models = [pool[i % len(pool)] for i in range(attempts)]
    else:
        models = [random.choice(SAFETY_NET_RECIPE_MODELS) for _ in range(attempts)]

    response = None
    used_model = models[0]
    for i, model in enumerate(models):
        label = f"{i + 1}차 시도"
        if on_attempt is not None:
            on_attempt(label, model)
        response = call_recipe_model(model, ingredient_names, api_key, **options)
        used_model = model
        if response.status_code == 200:
            try:
                body = response.json()
                choices = body.get("choices")
                if choices:
                    recipes = parse_recipes(choices[0]["message"]["content"])
                    if recipes and _is_korean_enough(recipes):
                        break
            except ValueError:
                pass
        elif should_stop_retrying(response):
            break
        if i < len(models) - 1:
            # 429는 모델별 순간 혼잡이라 1초로는 잘 풀리지 않는다. 시도마다 대기를 늘린다.
            time.sleep(2 * (i + 1))
    return response, used_model

_HANGUL_RE = re.compile(r"[가-힣]")


def _is_korean_enough(recipes):
    """레시피명/조리 순서에 한글이 전혀 없으면(영어로만 응답한 경우) 실패로 간주한다."""
    combined = " ".join(
        (r.get("name") or "") + " " + " ".join(r.get("steps") or []) for r in recipes
    )
    return bool(_HANGUL_RE.search(combined))


def parse_recipes(content):
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    recipes = data.get("recipes")
    if not isinstance(recipes, list):
        return None
    return recipes
