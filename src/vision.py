import base64
import io
import json
import random
import re
import uuid

import requests
from PIL import Image

from openrouter import fetch_models_payload, post_chat, run_with_fallback

MAX_IMAGE_DIMENSION = 1024
# 화면 표시용 사진의 최대 폭. st.image가 표시 전에 줄이는 최대 폭(2 * 730)과 같게 맞춘다.
DISPLAY_IMAGE_MAX_WIDTH = 1460
UNIT_OPTIONS = ["개", "병", "봉지", "팩", "단", "g", "kg", "ml", "L", "기타"]
RECOGNITION_PROMPT = (
    '이 냉장고 사진에서 보이는 식재료를 한국어로 인식해서 JSON 배열 형식으로만 답해줘. '
    '각 항목은 name(이름), quantity(수량, 숫자), unit(단위: 개/병/봉지/팩/단/g/kg/ml/L 중 하나)을 포함해줘.\n'
    '수량은 눈에 보이는 낱개를 하나씩 세어서 적어줘. '
    '달걀판처럼 여러 개가 담긴 것도 팩이 아니라 보이는 알의 개수로 세어줘. '
    '가려져서 셀 수 없을 때만 quantity를 1, unit을 "개"로 적어줘.\n'
    '음식이 아닌 물건(보관용기, 그릇, 포장재)은 목록에 넣지 마.\n'
    '예: [{"name": "계란", "quantity": 6, "unit": "개"}, {"name": "우유", "quantity": 1, "unit": "개"}]'
)

# 오픈라우터 무료 모델 목록 조회 자체가 실패할 경우를 위한 최소 안전망 (2026-10-31 동작 확인).
SAFETY_NET_VISION_MODELS = [
    "nvidia/nemotron-3.5-super-vl:free",
    "dots-studio/dots-3-note-preview:free",
]
_EXCLUDE_KEYWORDS = ["safety", "rerank", "guard"]

# 2026-10-31 냉장고 사진으로 다시 실측한 우선순위. 앞쪽일수록 먼저 시도한다.
# minimax-m3는 2026-10-22 무료 목록에서 빠졌다.
# 무료 모델은 응답 자체가 거부되는 경우가 잦아, 성공률을 인식 정확도보다 앞에 둔다.
#   nemotron-3.5-super-vl  성공 3/3, 품목 7/7, 19~52초
#   dots-3-note-preview    성공 2/3, 품목 7/7, 52~71초 (실패 1회는 10분 뒤 빈 응답)
#   gemma-4-26b            성공 0/3, 429 (2026-08-27에는 성공 2/13, 개수 7.0/7)
#   gemma-4-31b            성공 0/3, 429 (2026-08-27에는 성공 1/13, 개수 6.0/7)
# 목록에 없는 모델은 이 뒤에 무작위 순서로 붙는다 (새로 등장한 무료 모델도 기회를 얻도록).
PREFERRED_VISION_MODELS = [
    "nvidia/nemotron-3.5-super-vl:free",
    "dots-studio/dots-3-note-preview:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
]

def _fetch_free_vision_model_entries():
    """오픈라우터에서 현재 사용 가능한 무료 비전(이미지 입력) 모델의 원본 정보를 조회한다.

    무료 모델 라인업은 시간에 따라 계속 바뀌므로 5분 TTL로 다시 조회한다.
    조회 자체가 실패하면 빈 리스트를 반환한다 (호출부에서 안전망으로 대체).
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
        modality = m.get("architecture", {}).get("input_modalities", [])
        if "image" in modality:
            entries.append(m)
    return entries


def list_free_vision_models(allowed_models=None):
    """현재 사용 가능한 무료 비전 모델 id 목록. allowed_models가 주어지면 그 안에서만 고른다.

    allowed_models로 걸러낸 결과가 비어있으면(예: 사용자가 골라둔 모델이 전부 목록에서
    사라진 경우) 안전하게 전체 목록으로 되돌아간다.
    """
    entries = _fetch_free_vision_model_entries()
    models = [m.get("id", "") for m in entries] or list(SAFETY_NET_VISION_MODELS)
    if allowed_models:
        filtered = [m for m in models if m in allowed_models]
        if filtered:
            return filtered
    return models


def prioritize_models(pool):
    """실측 우선순위(PREFERRED_VISION_MODELS) 순서로 후보를 정렬한다.

    우선순위 목록에 없는 모델은 뒤쪽에 무작위 순서로 배치한다. 무료 모델 라인업은
    수시로 바뀌므로, 아직 실측하지 않은 새 모델도 뒤에서는 시도될 수 있게 남겨둔다.
    """
    ranked = [m for m in PREFERRED_VISION_MODELS if m in pool]
    rest = [m for m in pool if m not in ranked]
    random.shuffle(rest)
    return ranked + rest


def list_free_vision_models_detailed():
    """프로필 화면의 모델 선택 UI에 쓸 상세 정보(id/name/context_length/created)를 반환."""
    entries = _fetch_free_vision_model_entries()
    return [
        {
            "id": m.get("id", ""),
            "name": m.get("name") or m.get("id", ""),
            "context_length": m.get("context_length"),
            "created": m.get("created"),
        }
        for m in entries
    ]


def to_resized_data_uri(file_like):
    """이미지를 긴 변 기준 MAX_IMAGE_DIMENSION 이하로 축소해 base64 data URI로 변환한다."""
    image = Image.open(file_like)
    # JPEG는 디코딩 단계에서 1/2~1/8로 줄여 읽는다 (원본 전체를 RGB로 풀지 않아 몇 배 빠름).
    # 목표 크기 이상으로만 줄이므로 최종 축소는 아래 thumbnail이 그대로 담당한다. PNG는 영향 없음.
    image.draft("RGB", (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
    image = image.convert("RGB")
    image.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    resized_bytes = buffer.getvalue()
    b64 = base64.b64encode(resized_bytes).decode()
    return f"data:image/jpeg;base64,{b64}", len(resized_bytes)


def to_display_bytes(image_bytes):
    """화면 표시용 사진을 폭 DISPLAY_IMAGE_MAX_WIDTH 이하로 미리 줄여둔다.

    st.image는 원본 bytes를 받으면 리런마다 디코딩·축소·재인코딩을 반복한다(12MP 사진 기준
    수백 ms). 2단계에서는 재료를 고칠 때마다 리런되므로, 한 번만 줄여 session_state에 둔다.
    줄인 결과는 st.image가 손대지 않고 그대로 표시한다 (폭과 형식이 이미 맞으므로).
    """
    image = Image.open(io.BytesIO(image_bytes))
    if image.width <= DISPLAY_IMAGE_MAX_WIDTH:
        return image_bytes
    new_height = int(image.height * DISPLAY_IMAGE_MAX_WIDTH / image.width)
    # st.image와 같은 기준으로 형식을 고른다 (투명도가 있을 수 있으면 PNG, 아니면 JPEG).
    if image.mode in {"RGBA", "LA", "P"}:
        image_format = "PNG"
    else:
        image_format = "JPEG"
        image.draft("RGB", (DISPLAY_IMAGE_MAX_WIDTH, new_height))
        image = image.convert("RGB")
    image = image.resize((DISPLAY_IMAGE_MAX_WIDTH, new_height), resample=Image.BILINEAR)
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, quality=90)
    return buffer.getvalue()


def call_vision_model(model, data_uri, api_key, deadline_seconds):
    return post_chat(
        {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": RECOGNITION_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
        },
        api_key,
        deadline_seconds,
    )


def _has_ingredients(response):
    try:
        choices = response.json().get("choices")
        return bool(choices) and parse_ingredients(choices[0]["message"]["content"]) is not None
    except ValueError:
        return False


def recognize_ingredients(data_uri, api_key, on_attempt=None, attempts=3, allowed_models=None):
    """매 시도마다 오픈라우터의 현재 무료 비전 모델 중 서로 다른 모델을 순서대로 시도한다.

    실측 우선순위(PREFERRED_VISION_MODELS)가 앞에 오고, 아직 실측하지 않은 모델이
    무작위 순서로 뒤를 잇는다. 429(요청 폭주)든 JSON 파싱 실패든 시간 초과든 실패로 간주하고,
    성공(재료 목록을 얻을 때)할 때까지 또는 attempts 횟수만큼 다음 모델로 넘어간다.
    고정된 "폴백 모델" 개념 대신, 매 시도 자체가 서로 다른 모델이라 폴백 역할을 겸한다.

    on_attempt: 선택적 콜백. (label, model) 을 인자로 호출되어 진행 상황을 UI에 전달할 수 있다.
    allowed_models: 사용자가 프로필에서 선택한 모델 id 목록. 없으면 전체 무료 모델을 대상으로 한다.
    """
    pool = prioritize_models(list_free_vision_models(allowed_models=allowed_models))
    if pool:
        # 풀이 attempts보다 적으면(예: 사용자가 모델을 1~2개만 선택한 경우) 안전망 모델로
        # 채우지 않고 풀 안에서 순환한다 - 사용자가 고르지 않은 모델을 몰래 끼워넣지 않기 위함.
        models = [pool[i % len(pool)] for i in range(attempts)]
    else:
        models = [random.choice(SAFETY_NET_VISION_MODELS) for _ in range(attempts)]

    return run_with_fallback(
        models,
        lambda model, deadline: call_vision_model(model, data_uri, api_key, deadline),
        _has_ingredients,
        on_attempt,
    )


def merge_duplicate_ingredients(ingredients):
    """이름이 같은(공백 제거, 대소문자 무시) 재료는 수량을 합쳐 하나로 합친다."""
    merged = {}
    order = []
    for item in ingredients:
        key = item["name"].strip().lower()
        if key not in merged:
            merged[key] = dict(item)
            order.append(key)
        else:
            try:
                merged[key]["quantity"] = str(int(merged[key]["quantity"]) + int(item["quantity"]))
            except (TypeError, ValueError):
                pass
    return [merged[key] for key in order]


def parse_ingredients(content):
    """모델 응답을 [{"name", "quantity", "unit"}, ...] 형태로 파싱한다.

    모델이 예전 방식대로 문자열 배열(["계란", "우유"])로 답한 경우도
    수량 1, 단위 "개"로 채워 동일한 형태로 반환한다 (하위 호환).
    동일한 이름의 재료가 중복 인식된 경우 수량을 합쳐 하나로 정리한다.
    """
    match = re.search(r"\[.*\]", content, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None

    ingredients = []
    for item in data:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            try:
                quantity = int(float(item.get("quantity", 1)))
            except (TypeError, ValueError):
                quantity = 1
            unit = str(item.get("unit") or "개").strip() or "개"
        else:
            name = str(item).strip()
            if not name:
                continue
            quantity = 1
            unit = "개"
        ingredients.append({"_id": uuid.uuid4().hex, "name": name, "quantity": str(quantity), "unit": unit})
    if not ingredients:
        return None
    return merge_duplicate_ingredients(ingredients)
