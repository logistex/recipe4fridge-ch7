"""vision.py와 recipe.py가 함께 쓰는 오픈라우터 공용 설정과 모델 목록 캐시."""
import time

import requests
import streamlit as st
import urllib3

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"
MODELS_TIMEOUT_SECONDS = 10
CONNECT_TIMEOUT_SECONDS = 10
# 모델 하나에 주는 응답 시간 상한. 실측 최장 정상 응답(dots-3-note-preview 71초)보다 여유를 둔다.
REQUEST_TIMEOUT_SECONDS = 90
# 폴백 전체(시도 + 대기)에 주는 시간 상한. 남은 시간이 MIN_ATTEMPT_SECONDS보다 적으면
# 다음 모델을 시도하지 않는다 (응답을 기대하기 어려운 짧은 시도로 시간을 낭비하지 않도록).
TOTAL_TIMEOUT_SECONDS = 180
MIN_ATTEMPT_SECONDS = 15

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


def post_chat(payload, api_key, deadline_seconds):
    """chat completions를 호출하되, 응답이 끝날 때까지의 전체 시간에 상한을 둔다.

    requests의 timeout은 바이트 수신 간격의 상한일 뿐이다. 오픈라우터는 처리 중에 공백을
    조금씩 보내 연결을 유지하므로, 모델이 늦으면 timeout에 걸리지 않고 몇 분씩 기다리게
    된다 (실측 199초). 그래서 본문을 스트림으로 받으며 경과 시간을 직접 확인한다.
    """
    started = time.monotonic()
    response = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=(CONNECT_TIMEOUT_SECONDS, deadline_seconds),
        stream=True,
    )
    chunks = []
    try:
        for chunk in response.iter_content(chunk_size=None):
            chunks.append(chunk)
            if time.monotonic() - started > deadline_seconds:
                raise requests.exceptions.ReadTimeout(
                    f"응답이 {deadline_seconds:.0f}초 안에 끝나지 않았습니다."
                )
    except requests.exceptions.ConnectionError as e:
        # 본문을 읽다 난 수신 시간 초과는 requests가 ConnectionError로 감싸므로 Timeout으로 되돌린다.
        if e.args and isinstance(e.args[0], urllib3.exceptions.ReadTimeoutError):
            raise requests.exceptions.ReadTimeout(*e.args) from e
        raise
    finally:
        response.close()
    response._content = b"".join(chunks)
    return response


def run_with_fallback(models, send, is_success, on_attempt=None):
    """models를 순서대로 시도해 is_success(response)가 참인 첫 응답을 돌려준다.

    send(model, deadline_seconds)는 요청을 보내 응답을 돌려주는 함수다. 시간 초과·연결
    오류도 실패한 시도로 보고 다음 모델로 넘어가며, 폴백 전체는 TOTAL_TIMEOUT_SECONDS 안에
    끝낸다. 마지막 시도가 예외로 끝났으면 그 예외를 올려 호출부가 원인을 보여주게 한다.

    on_attempt: 선택적 콜백. (label, model) 을 인자로 호출되어 진행 상황을 UI에 전달할 수 있다.
    반환값: (마지막 응답, 마지막으로 시도한 모델)
    """
    started = time.monotonic()
    response = None
    last_error = None
    used_model = models[0]
    for i, model in enumerate(models):
        remaining = TOTAL_TIMEOUT_SECONDS - (time.monotonic() - started)
        if i > 0 and remaining < MIN_ATTEMPT_SECONDS:
            break
        if on_attempt is not None:
            on_attempt(f"{i + 1}차 시도", model)
        used_model = model
        try:
            response = send(model, min(REQUEST_TIMEOUT_SECONDS, remaining))
        except requests.exceptions.RequestException as e:
            # 시간 초과는 이미 충분히 기다렸으므로 대기 없이 바로 다음 모델로 넘어간다.
            last_error = e
            continue
        last_error = None
        if response.status_code == 200 and is_success(response):
            break
        if should_stop_retrying(response):
            break
        if i < len(models) - 1:
            # 429는 모델별 순간 혼잡이라 1초로는 잘 풀리지 않는다. 시도마다 대기를 늘린다.
            time.sleep(2 * (i + 1))
    if last_error is not None:
        raise last_error
    return response, used_model
