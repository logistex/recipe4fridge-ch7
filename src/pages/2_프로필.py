from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import db
from auth_ui import current_user, render_sidebar_auth
from recipe import (
    CUISINE_OPTIONS,
    DIFFICULTY_OPTIONS,
    SERVINGS_OPTIONS,
    TIME_OPTIONS,
    list_free_text_models_detailed,
)
from ui_common import DB_UNAVAILABLE_MESSAGE, escape_markdown
from vision import list_free_vision_models_detailed

st.set_page_config(page_title="프로필", page_icon="👤")
render_sidebar_auth()

st.title("👤 프로필")

user = current_user()
if not user:
    st.info("로그인 후 프로필을 확인할 수 있습니다. 왼쪽 사이드바에서 로그인/회원가입해 주세요.")
    st.stop()

# 닉네임 변경처럼 저장 직후 st.rerun()하는 경우, 완료 알림을 다음 실행에서 보여준다.
profile_notice = st.session_state.pop("profile_notice", None)
if profile_notice:
    st.toast(profile_notice, icon="✅")

try:
    saved = db.get_user_by_id(user["id"])
except db.DatabaseError:
    st.error(DB_UNAVAILABLE_MESSAGE)
    st.stop()
if not saved:
    st.error("계정 정보를 찾을 수 없습니다. 사이드바에서 로그아웃한 뒤 다시 로그인해 주세요.")
    st.stop()

st.write(f"**이메일**: {escape_markdown(user['email'])}")

with st.form("profile_form"):
    new_nickname = st.text_input("닉네임", value=user["nickname"])
    submitted = st.form_submit_button("저장")
    if submitted:
        new_nickname = new_nickname.strip()
        if not new_nickname:
            st.error("닉네임을 입력해 주세요.")
        else:
            try:
                db.update_nickname(user["id"], new_nickname)
            except db.DatabaseError:
                st.error("닉네임을 변경하지 못했습니다. " + DB_UNAVAILABLE_MESSAGE)
            else:
                st.session_state.current_user["nickname"] = new_nickname
                st.session_state.profile_notice = "닉네임이 변경되었습니다."
                st.rerun()

st.divider()
st.subheader("🍳 레시피 추천 기본값")
st.caption("여기서 설정한 값이 레시피 추천 화면(3단계)의 기본 선택값으로 채워집니다. 그 화면에서 매번 다시 바꿀 수 있습니다.")


def _index(saved_value, options):
    return options.index(saved_value) if saved_value in options else 0


with st.form("recipe_preference_form"):
    pref_cols = st.columns(4)
    pref_cuisine = pref_cols[0].selectbox(
        "요리 종류", CUISINE_OPTIONS, index=_index(saved.get("default_cuisine"), CUISINE_OPTIONS)
    )
    pref_difficulty = pref_cols[1].selectbox(
        "난이도", DIFFICULTY_OPTIONS, index=_index(saved.get("default_difficulty"), DIFFICULTY_OPTIONS)
    )
    pref_time = pref_cols[2].selectbox(
        "조리 시간", TIME_OPTIONS, index=_index(saved.get("default_time"), TIME_OPTIONS)
    )
    pref_servings = pref_cols[3].selectbox(
        "인원", SERVINGS_OPTIONS, index=_index(saved.get("default_servings"), SERVINGS_OPTIONS)
    )
    pref_submitted = st.form_submit_button("기본값 저장")
    if pref_submitted:
        try:
            db.update_recipe_preferences(user["id"], pref_cuisine, pref_difficulty, pref_time, pref_servings)
        except db.DatabaseError:
            st.error("기본값을 저장하지 못했습니다. " + DB_UNAVAILABLE_MESSAGE)
        else:
            st.success("레시피 추천 기본값이 저장되었습니다.")

st.divider()
st.subheader("🤖 AI 모델 선택")
st.caption(
    "체크한 모델만 식재료 인식/레시피 생성에 사용합니다. 실제로 잘 동작한 것으로 확인된 모델을 먼저 시도하고, "
    "나머지는 무작위 순서로 시도합니다. "
    "하나도 선택하지 않고 저장하면 전체 무료 모델을 사용합니다. "
    "(오픈라우터가 공개하지 않는 응답 속도/처리량 정보는 표시할 수 없어 이름·컨텍스트·출시일만 보여줍니다.)"
)


def _relative_release(created_ts):
    if not created_ts:
        return "-"
    try:
        created = datetime.fromtimestamp(created_ts, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return "-"
    days = (datetime.now(timezone.utc) - created).days
    if days <= 0:
        return "오늘"
    if days < 30:
        return f"{days}일 전"
    months = days // 30
    return f"{months}개월 전"


def _model_dataframe(entries, selected_ids):
    has_selection = bool(selected_ids)
    selected_set = set(selected_ids or [])
    rows = [
        {
            "선택": (e["id"] in selected_set) if has_selection else True,
            "모델명": e["name"],
            "컨텍스트": e["context_length"],
            "출시일": _relative_release(e["created"]),
        }
        for e in entries
    ]
    return pd.DataFrame(rows, columns=["선택", "모델명", "컨텍스트", "출시일"])


def _selected_ids(entries, edited_df):
    name_to_id = {e["name"]: e["id"] for e in entries}
    return [
        name_to_id[row["모델명"]]
        for _, row in edited_df.iterrows()
        if row["선택"] and row["모델명"] in name_to_id
    ]


with st.spinner("사용 가능한 모델 목록을 불러오는 중입니다..."):
    vision_entries = list_free_vision_models_detailed()
    text_entries = list_free_text_models_detailed()
saved_vision_ids = db.parse_model_id_list(saved.get("selected_vision_models"))
saved_text_ids = db.parse_model_id_list(saved.get("selected_text_models"))

st.markdown("**식재료 인식(이미지) 모델**")
if not vision_entries:
    st.warning("현재 오픈라우터에서 무료 비전 모델 목록을 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.")
    edited_vision = None
else:
    edited_vision = st.data_editor(
        _model_dataframe(vision_entries, saved_vision_ids),
        hide_index=True,
        disabled=["모델명", "컨텍스트", "출시일"],
        key="vision_model_editor",
        use_container_width=True,
    )

st.markdown("**레시피 생성 모델**")
if not text_entries:
    st.warning("현재 오픈라우터에서 레시피 생성용 무료 모델 목록을 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.")
    edited_text = None
else:
    edited_text = st.data_editor(
        _model_dataframe(text_entries, saved_text_ids),
        hide_index=True,
        disabled=["모델명", "컨텍스트", "출시일"],
        key="text_model_editor",
        use_container_width=True,
    )

if st.button("모델 선택 저장"):
    new_vision_ids = _selected_ids(vision_entries, edited_vision) if edited_vision is not None else saved_vision_ids
    new_text_ids = _selected_ids(text_entries, edited_text) if edited_text is not None else saved_text_ids
    try:
        db.update_model_selection(user["id"], new_vision_ids or [], new_text_ids or [])
    except db.DatabaseError:
        st.error("모델 선택을 저장하지 못했습니다. " + DB_UNAVAILABLE_MESSAGE)
    else:
        st.success("모델 선택이 저장되었습니다.")
