"""vision.py와 recipe.py가 함께 쓰는 오픈라우터 공용 설정과 모델 목록 캐시."""
import requests
import streamlit as st

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"
MODELS_TIMEOUT_SECONDS = 10
REQUEST_TIMEOUT_SECONDS = 60

# 어느 모델로 바꿔 보내도 똑같이 실패하는 상태코드 (401 키 오류, 402 크레딧 부족).
# 403은 모델별 검열(moderation)에서도 나오므로 다음 모델로 넘어갈 여지를 남긴다.
NON_RETRYABLE_STATUS_CODES = {401, 402}


@st.cache_data(ttl=300, show_spinner=False)
def fetch_models_payload():
    """오픈라우터 모델 목록 원본(약 800KB)을 5분간 캐시한다.

    비전/레시피 모듈이 같은 캐시를 공유해 모델 목록 호출이 한 번만 나가게 한다.
    실패하면 예외를 그대로 올려서 실패 결과가 캐시에 남지 않게 한다
    (일시적인 네트워크 오류가 5분 동안 굳어지지 않도록).
    """
    response = requests.get(MODELS_URL, timeout=MODELS_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json().get("data", [])


def error_info(response):
    """오류 응답 본문의 error 객체를 꺼낸다. JSON이 아니면 빈 dict."""
    try:
        return (response.json() or {}).get("error") or {}
    except ValueError:
        return {}


def is_daily_rate_limit(error):
    """429 오류가 모델 순간 혼잡이 아니라 계정의 무료 호출 일일 한도 초과인지 판별한다."""
    metadata = error.get("metadata") or {}
    return (
        "free-models-per-day" in str(error.get("message") or "")
        or metadata.get("limit_source") == "openrouter_free_tier_daily"
    )


def should_stop_retrying(response):
    """다른 모델로 다시 시도해도 같은 이유로 실패할 응답이면 True.

    키 오류/크레딧 부족/일일 한도는 모든 무료 모델에 똑같이 걸리므로, 대기 시간과
    호출을 낭비하지 않고 바로 중단한다.
    """
    if response.status_code in NON_RETRYABLE_STATUS_CODES:
        return True
    return response.status_code == 429 and is_daily_rate_limit(error_info(response))

