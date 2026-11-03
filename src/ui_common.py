"""여러 화면(app.py, auth_ui.py, pages/)이 함께 쓰는 화면 표시용 도우미와 상수."""
import re

import streamlit as st

# DB(회원 정보·저장한 레시피) 연결이 실패했을 때 보여줄 안내. 오류 원문에는 접속 호스트·사용자
# 이름이 들어 있을 수 있으므로 화면에 그대로 내보내지 않는다.
DB_UNAVAILABLE_MESSAGE = (
    "회원 정보와 저장한 레시피를 보관하는 서버에 지금 연결할 수 없습니다. "
    "잠시 후 페이지를 새로고침해 주세요."
)

# "처음부터" 버튼과 로그아웃 시 함께 지우는 진행 상태 키. 두 곳이 같은 목록을 쓰도록 여기에만 둔다.
# app.py에서 새 session_state 키를 추가하면 여기도 갱신해야 한다.
APP_FLOW_STATE_KEYS = [
    "wizard_step",
    "uploaded_photo_bytes",
    "uploaded_photo_name",
    "ingredients",
    "recognition_error",
    "used_vision_model",
    "used_recipe_model",
    "recipes",
    "selected_recipe",
    "last_recipe_request_time",
    "recipe_cuisine",
    "recipe_difficulty",
    "recipe_time",
    "recipe_servings",
]
# 재료 편집기·레시피 카드의 위젯 key 접두사. 이전 사용자의 입력값이 남지 않도록 함께 지운다.
APP_FLOW_WIDGET_KEY_PREFIXES = ("ingredient_", "delete_", "select_recipe_")

_MARKDOWN_SPECIAL_RE = re.compile(r"([\\`*_{}\[\]()#+\-.!|~<>$:])")
_FIRST_NUMBER_RE = re.compile(r"\d+")


def clear_app_flow_state():
    """진행 중인 위저드 상태(재료/레시피/오류/위젯 값)를 모두 지우고 사진 업로더도 비운다."""
    for key in APP_FLOW_STATE_KEYS:
        st.session_state.pop(key, None)
    for key in [k for k in st.session_state.keys() if str(k).startswith(APP_FLOW_WIDGET_KEY_PREFIXES)]:
        st.session_state.pop(key, None)
    # 업로더 위젯 key를 바꿔야 이전에 올린 사진이 화면에서 사라진다.
    st.session_state.uploader_key = st.session_state.get("uploader_key", 0) + 1
    st.session_state.wizard_step = 1


def escape_markdown(text):
    """사용자 입력·AI 출력 문자열이 마크다운(굵게, 링크, :red[...] 색상 등)으로 해석되지 않게 한다."""
    return _MARKDOWN_SPECIAL_RE.sub(r"\\\1", str(text))


def cook_time_to_int(value):
    """조리 시간 값을 정수(분) 또는 None으로 맞춘다. "15~20분"처럼 오면 첫 숫자를 쓴다."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    match = _FIRST_NUMBER_RE.search(str(value or ""))
    return int(match.group(0)) if match and int(match.group(0)) > 0 else None


def format_cook_time(value):
    minutes = cook_time_to_int(value)
    return f"⏱ 약 {minutes}분" if minutes else "⏱ 조리 시간 정보 없음"


def as_text_list(value):
    """레시피의 재료/조리 순서 값을 화면에 그릴 수 있는 문자열 리스트로 맞춘다."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None and str(item).strip()]
    return [str(value)] if str(value).strip() else []
