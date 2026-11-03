import streamlit as st

import db
from auth_ui import current_user, render_sidebar_auth
from ui_common import DB_UNAVAILABLE_MESSAGE, as_text_list, escape_markdown, format_cook_time

st.set_page_config(page_title="내 레시피", page_icon="📖")
render_sidebar_auth()

st.title("📖 내 레시피")

user = current_user()
if not user:
    st.info("로그인 후 저장한 레시피를 볼 수 있습니다. 왼쪽 사이드바에서 로그인/회원가입해 주세요.")
    st.stop()

try:
    recipes = db.list_recipes(user["id"])
except db.DatabaseError:
    st.error(DB_UNAVAILABLE_MESSAGE)
    st.stop()

if not recipes:
    st.info(
        "아직 저장한 레시피가 없습니다. 메인 화면의 '③ 레시피 추천'에서 레시피를 '자세히 보기'로 연 뒤 "
        "'💾 레시피 저장'을 눌러 보세요."
    )
else:
    st.caption(f"저장한 레시피 {len(recipes)}개")
    delete_target = None
    for recipe in recipes:
        recipe_name = escape_markdown(recipe.get("name") or "이름 없음")
        with st.container(border=True):
            col_main, col_delete = st.columns([5, 1])
            with col_main:
                st.markdown(f"### {recipe_name}")
                st.caption(f"저장 시각: {recipe['saved_at']}")
                st.write(format_cook_time(recipe.get("cook_time_minutes")))
                st.write("**사용 재료**: " + escape_markdown(", ".join(as_text_list(recipe["used_ingredients"]))))
                missing = as_text_list(recipe["missing_ingredients"])
                if missing:
                    st.write("**추가로 필요한 재료**: " + escape_markdown(", ".join(missing)))
                with st.expander("조리 순서 보기"):
                    for i, step in enumerate(as_text_list(recipe["steps"]), start=1):
                        st.write(f"{i}. {escape_markdown(step)}")
            with col_delete:
                # 삭제는 되돌릴 수 없으므로 한 번 더 확인을 거친 뒤 삭제한다.
                with st.popover("삭제"):
                    st.write(f"'{recipe_name}' 레시피를 삭제할까요? 삭제하면 되돌릴 수 없습니다.")
                    if st.button("삭제 확인", key=f"delete_saved_{recipe['id']}", type="primary"):
                        delete_target = recipe["id"]

    if delete_target is not None:
        try:
            db.delete_recipe(user["id"], delete_target)
        except db.DatabaseError:
            st.error("레시피를 삭제하지 못했습니다. " + DB_UNAVAILABLE_MESSAGE)
        else:
            st.rerun()
