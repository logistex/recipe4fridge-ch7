import os
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st
from dotenv import load_dotenv

import db
from auth_ui import current_user, render_sidebar_auth
from openrouter import error_info, is_daily_rate_limit
from recipe import (
    CUISINE_OPTIONS,
    DIFFICULTY_OPTIONS,
    SERVINGS_OPTIONS,
    TIME_OPTIONS,
    generate_recipes,
    parse_recipes,
)
from vision import (
    MAX_IMAGE_DIMENSION,
    UNIT_OPTIONS,
    merge_duplicate_ingredients,
    parse_ingredients,
    recognize_ingredients,
    to_display_bytes,
    to_resized_data_uri,
)

load_dotenv()

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
RECIPE_REQUEST_COOLDOWN_SECONDS = 5
WIZARD_STEP_LABELS = ["① 사진 업로드", "② 식재료 확인", "③ 레시피 추천"]

# "처음부터" 버튼/로그아웃 시 초기화할 진행 상태 키. 새 session_state 키를 추가하면 여기도 갱신해야 한다.
RESET_STATE_KEYS = [
    "wizard_step",
    "uploaded_photo_bytes",
    "uploaded_photo_name",
    "ingredients",
    "recognition_error",
    "recognition_error_detail",
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


def rate_limit_message(response, action):
    """429 응답을 계정 일일 한도와 모델 순간 혼잡으로 구분해 안내 문구를 만든다.

    둘은 사용자가 할 일이 다르다. 일일 한도는 초기화될 때까지 다시 눌러도 소용없고,
    순간 혼잡은 잠시 뒤 다시 시도하면 대체로 풀린다.
    """
    error = error_info(response)
    metadata = error.get("metadata") or {}
    if not is_daily_rate_limit(error):
        return (
            f"❌ {action} 실패: 무료 모델 3곳이 모두 혼잡합니다 (429). "
            "1~2분 뒤에 다시 시도해 주세요."
        )

    headers = metadata.get("headers") or {}
    limit = headers.get("X-RateLimit-Limit")
    quota = f"오늘 쓸 수 있는 무료 호출 {limit}회를" if limit else "오늘 쓸 수 있는 무료 호출을"
    when = _format_reset_time(headers.get("X-RateLimit-Reset"))
    return (
        f"❌ {action} 실패: {quota} 모두 사용했습니다. "
        f"{when} 초기화되며, 그때까지는 다시 시도해도 같은 오류가 납니다."
    )


def _format_reset_time(reset_ms):
    """한도 초기화 시각(에포크 밀리초)을 한국 시각 문구로 바꾼다.

    배포 환경(Streamlit Cloud)의 시간대가 UTC이므로 한국 시각으로 명시해 변환한다.
    """
    try:
        moment = datetime.fromtimestamp(int(reset_ms) / 1000, ZoneInfo("Asia/Seoul"))
    except (TypeError, ValueError, OSError, OverflowError):
        return "잠시 후"
    half = "오전" if moment.hour < 12 else "오후"
    hour = moment.hour % 12 or 12
    return f"{moment.month}월 {moment.day}일 {half} {hour}시에"


def get_api_key():
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    try:
        return st.secrets["OPENROUTER_API_KEY"]
    except (FileNotFoundError, KeyError):
        st.error("OPENROUTER_API_KEY가 설정되지 않았습니다. .env 또는 Streamlit secrets를 확인해주세요.")
        st.stop()


def get_allowed_models(field_name):
    """로그인한 사용자가 프로필에서 선택해둔 모델 목록을 읽어온다. 없으면 None(전체 사용)."""
    user = current_user()
    if not user:
        return None
    saved = db.get_user_by_id(user["id"])
    if not saved:
        return None
    return db.parse_model_id_list(saved.get(field_name))


def render_ingredient_editor():
    """② 식재료 확인 섹션. 2단계와 3단계 화면에서 공통으로 사용한다.

    재료 한 개당 한 줄(이름/수량/단위/삭제)로 배치한다 (추가 폼과 동일한 형태).
    """
    st.subheader("② 식재료 확인")
    st.caption("⚠️ AI 인식 결과는 부정확할 수 있습니다. 이름/수량/단위를 확인하고 필요하면 직접 수정해주세요.")

    ingredients = st.session_state.get("ingredients", [])
    if not ingredients:
        st.info("아직 목록이 비어 있습니다. 아래에서 직접 추가해주세요.")

    delete_index = None
    for i, item in enumerate(ingredients):
        item.setdefault("unit", "개")
        # 위젯 key는 배열 인덱스가 아니라 재료별 고유 id를 써야 한다. 인덱스를 쓰면
        # 항목 삭제로 뒤쪽 재료들의 인덱스가 당겨질 때 Streamlit이 그 위젯들의
        # 이전(다른 재료) 값을 그대로 들고 있어서, 중간 삭제 시 엉뚱한(마지막) 항목이
        # 사라진 것처럼 보이는 문제가 있었다.
        item_id = item.setdefault("_id", uuid.uuid4().hex)
        col_name, col_qty, col_unit, col_delete = st.columns([2.5, 2, 1.5, 1.2])
        item["name"] = col_name.text_input(
            "이름", value=item["name"], key=f"ingredient_name_{item_id}", label_visibility="collapsed"
        )
        try:
            qty_value = int(item["quantity"])
        except (TypeError, ValueError):
            qty_value = 1
        item["quantity"] = str(
            col_qty.number_input(
                "수량",
                min_value=0,
                step=1,
                value=qty_value,
                key=f"ingredient_qty_{item_id}",
                label_visibility="collapsed",
            )
        )
        unit_index = UNIT_OPTIONS.index(item["unit"]) if item["unit"] in UNIT_OPTIONS else 0
        item["unit"] = col_unit.selectbox(
            "단위", UNIT_OPTIONS, index=unit_index, key=f"ingredient_unit_{item_id}", label_visibility="collapsed"
        )
        if col_delete.button("삭제", key=f"delete_{item_id}"):
            delete_index = i
    if delete_index is not None:
        ingredients.pop(delete_index)
        st.rerun()

    with st.form("add_ingredient_form", clear_on_submit=True):
        col_name, col_qty, col_unit, col_add = st.columns([2.5, 2, 1.5, 1.2])
        new_name = col_name.text_input("재료 이름", label_visibility="collapsed", placeholder="재료 이름")
        new_qty = col_qty.number_input("수량", min_value=0, step=1, value=1, label_visibility="collapsed")
        new_unit = col_unit.selectbox("단위", UNIT_OPTIONS, label_visibility="collapsed")
        submitted = col_add.form_submit_button("추가")
        if submitted and new_name.strip():
            new_name_clean = new_name.strip()
            existing = next(
                (it for it in ingredients if it["name"].strip().lower() == new_name_clean.lower()), None
            )
            if existing:
                try:
                    existing["quantity"] = str(int(existing["quantity"]) + int(new_qty))
                except (TypeError, ValueError):
                    existing["quantity"] = str(new_qty)
            else:
                ingredients.append(
                    {"_id": uuid.uuid4().hex, "name": new_name_clean, "quantity": str(new_qty), "unit": new_unit}
                )
            st.rerun()

    st.session_state.ingredients = merge_duplicate_ingredients(ingredients)


st.set_page_config(page_title="냉장고 식재료 인식", page_icon="🥬", layout="wide")
render_sidebar_auth()

# '자세히 보기' 버튼이 카드마다 다른 높이에 걸리지 않도록, 카드 안 마지막 요소(버튼)를 바닥에 붙인다.
st.markdown(
    """
    <style>
    div[data-testid="column"] > div[data-testid="stVerticalBlockBorderWrapper"] > div {
        display: flex;
        flex-direction: column;
        height: 100%;
    }
    div[data-testid="column"] > div[data-testid="stVerticalBlockBorderWrapper"] > div > div:last-child {
        margin-top: auto;
    }
    .ingredient-chip {
        display: inline-block;
        background: #e8e8e8;
        color: #333;
        border-radius: 14px;
        padding: 4px 12px;
        margin: 2px 4px 2px 0;
        font-size: 0.9em;
        white-space: nowrap;
    }
    /* layout="wide"는 본문 폭이 브라우저 폭(=확대/축소 배율)에 비례해서 늘어난다.
       업로드 사진은 use_container_width=True라 그 폭을 그대로 따라가므로,
       화면을 축소(더 넓은 영역이 보임)하면 사진이 오히려 커지는 문제가 있었다.
       최대 폭을 제한해 과도하게 커지지 않게 한다 (좁은 화면에서는 계속 축소됨). */
    [data-testid="stImage"] img {
        max-width: 700px !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.session_state.setdefault("uploader_key", 0)
st.session_state.setdefault("wizard_step", 1)

def _do_restart():
    for key in RESET_STATE_KEYS:
        st.session_state.pop(key, None)
    st.session_state.uploader_key += 1
    st.session_state.wizard_step = 1
    st.rerun()


header_col, restart_col = st.columns([5, 1])
header_col.title("🥬 냉장고 레시피 추천")
with restart_col:
    has_progress = st.session_state.wizard_step > 1 or bool(st.session_state.get("ingredients"))
    if has_progress:
        # 진행 중인 작업(인식된 재료/레시피)이 있을 때만 확인을 한 번 거치게 해서 실수로 잃지 않게 한다.
        with st.popover("🔄 처음부터"):
            st.write("지금까지 인식하거나 수정한 내용이 모두 사라집니다.")
            if st.button("네, 처음부터 시작할게요", type="primary"):
                _do_restart()
    else:
        if st.button("🔄 처음부터"):
            _do_restart()

api_key = get_api_key()
wizard_step = st.session_state.wizard_step

step_cols = st.columns(3)
for i, col in enumerate(step_cols, start=1):
    label = WIZARD_STEP_LABELS[i - 1]
    # 마지막 단계(③)는 다음 단계가 없어 i < wizard_step 조건으로는 절대 완료 표시가 되지 않는다.
    # 레시피가 실제로 생성되었는지(st.session_state.recipes)를 완료 기준으로 따로 확인한다.
    is_last_step_done = i == 3 and i == wizard_step and bool(st.session_state.get("recipes"))
    if is_last_step_done:
        col.markdown(f"✅ **:red[{label}]**")
    elif i == wizard_step:
        col.markdown(f"**:red[{label}]**")
    elif i < wizard_step:
        col.markdown(f"✅ {label}")
    else:
        col.markdown(label)
st.divider()

# ==================== 1단계 화면: 사진 업로드(좌) + 식재료 인식(우) ====================
if wizard_step == 1:
    st.subheader("① 사진 업로드")
    uploaded_file = st.file_uploader(
        "냉장고 사진을 업로드하거나, 이 영역으로 파일을 끌어다 놓으세요",
        type=["jpg", "jpeg", "png"],
        key=f"fridge_photo_{st.session_state.uploader_key}",
    )

    if uploaded_file is not None:
        if uploaded_file.size > MAX_UPLOAD_BYTES:
            st.error("이미지 용량이 10MB를 초과합니다. 더 작은 이미지를 업로드해주세요.")
            st.stop()

        photo_col, recognize_col = st.columns(2)

        with photo_col:
            st.image(uploaded_file, caption="업로드한 사진", use_container_width=True)

        with recognize_col:
            if st.button("식재료 인식하기", type="primary"):
                st.session_state.pop("recognition_error", None)
                st.session_state.pop("recognition_error_detail", None)
                st.session_state.pop("ingredients", None)
                st.session_state.pop("recipes", None)
                st.session_state.pop("selected_recipe", None)
                # 2단계에서 리런마다 원본을 다시 줄이지 않도록 표시용 크기로 한 번만 줄여 보관한다.
                st.session_state.uploaded_photo_bytes = to_display_bytes(uploaded_file.getvalue())
                st.session_state.uploaded_photo_name = uploaded_file.name

                with st.status("사진을 분석하는 중입니다...", expanded=True) as status:
                    data_uri, resized_bytes = to_resized_data_uri(uploaded_file)
                    status.write(
                        f"이미지 축소: {uploaded_file.size // 1024}KB → {resized_bytes // 1024}KB "
                        f"(긴 변 {MAX_IMAGE_DIMENSION}px 이하)"
                    )
                    try:
                        response, used_model = recognize_ingredients(
                            data_uri,
                            api_key,
                            on_attempt=lambda label, model: status.write(f"{label}: `{model}` 호출 중..."),
                            allowed_models=get_allowed_models("selected_vision_models"),
                        )
                    except requests.exceptions.Timeout:
                        status.update(label="분석 실패", state="error")
                        st.session_state.recognition_error = "❌ 식재료 인식 실패: 모델이 제한 시간 안에 응답하지 않았습니다. 잠시 후 다시 시도해주세요."
                        st.rerun()
                    except requests.exceptions.RequestException as e:
                        status.update(label="분석 실패", state="error")
                        st.session_state.recognition_error = f"❌ 식재료 인식 실패: 네트워크 오류가 발생했습니다: {e}"
                        st.rerun()
                    status.update(label="분석 완료", state="complete")

                st.session_state.used_vision_model = used_model

                if response.status_code == 429:
                    st.session_state.recognition_error = rate_limit_message(response, "식재료 인식")
                    st.session_state.recognition_error_detail = response.text
                elif response.status_code >= 500:
                    st.session_state.recognition_error = "❌ 식재료 인식 실패: 모델 제공자 서버 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
                elif response.status_code != 200:
                    st.session_state.recognition_error = f"❌ 식재료 인식 실패: 요청이 실패했습니다 (status {response.status_code})."
                    st.session_state.recognition_error_detail = response.text
                else:
                    body = response.json()
                    choices = body.get("choices")
                    if not choices:
                        st.session_state.recognition_error = "❌ 식재료 인식 실패: 모델 응답 형식이 올바르지 않습니다 (choices 없음). 잠시 후 다시 시도해주세요."
                        st.session_state.recognition_error_detail = response.text
                    else:
                        content = choices[0]["message"]["content"]
                        ingredients = parse_ingredients(content)
                        if ingredients is None:
                            st.session_state.recognition_error = "❌ 식재료 인식 실패: 서로 다른 모델 3차 시도 모두 응답을 재료 목록으로 변환하지 못했습니다."
                            st.session_state.recognition_error_detail = content
                        else:
                            st.session_state.ingredients = ingredients
                            st.session_state.wizard_step = 2
                st.rerun()

            if st.session_state.get("used_vision_model"):
                st.caption(f"실제 응답 모델: `{st.session_state.used_vision_model}`")

            if st.session_state.get("recognition_error"):
                st.error(st.session_state.recognition_error)
                if st.session_state.get("recognition_error_detail"):
                    with st.expander("자세히 보기 (기술 정보)"):
                        st.code(st.session_state.recognition_error_detail)
                if st.button("재료를 직접 입력할게요"):
                    st.session_state.ingredients = []
                    st.session_state.pop("recognition_error", None)
                    st.session_state.pop("recognition_error_detail", None)
                    st.session_state.wizard_step = 2
                    st.rerun()

# ==================== 2단계 화면: 업로드한 사진(좌) + 식재료 확인(우) ====================
elif wizard_step == 2:
    photo_col, ingredient_col = st.columns(2)

    with photo_col:
        st.subheader("① 사진 업로드")
        if st.session_state.get("uploaded_photo_bytes"):
            st.image(
                st.session_state.uploaded_photo_bytes,
                caption=st.session_state.get("uploaded_photo_name", "업로드한 사진"),
                use_container_width=True,
            )
        if st.session_state.get("used_vision_model"):
            st.caption(f"실제 응답 모델: `{st.session_state.used_vision_model}`")

    with ingredient_col:
        render_ingredient_editor()

    st.divider()
    nav_cols = st.columns(2)
    if nav_cols[0].button("← 이전 (사진 다시 업로드)"):
        st.session_state.wizard_step = 1
        st.rerun()
    ingredients = st.session_state.get("ingredients", [])
    if nav_cols[1].button(
        "다음: 레시피 추천 →",
        type="primary",
        disabled=not ingredients,
        help=None if ingredients else "재료를 1개 이상 추가해야 다음 단계로 진행할 수 있습니다.",
    ):
        st.session_state.wizard_step = 3
        st.rerun()

# ==================== 3단계 화면: 식재료 확인(좌) + 레시피 추천(우) ====================
elif wizard_step == 3:
    ingredient_col, recipe_col = st.columns(2)

    with ingredient_col:
        render_ingredient_editor()
        if st.button("← 이전 (식재료 다시 확인)"):
            st.session_state.wizard_step = 2
            st.rerun()

    with recipe_col:
        st.subheader("③ 레시피 추천")

        ingredient_names = [
            item["name"] for item in st.session_state.ingredients if item.get("name", "").strip()
        ]
        chips_html = "".join(f'<span class="ingredient-chip">{name}</span>' for name in ingredient_names)
        st.markdown(chips_html or "_재료를 먼저 추가해주세요._", unsafe_allow_html=True)

        # 이번 세션에서 처음 진입한 경우, 프로필에 저장된 기본 조건을 초깃값으로 채운다.
        # 이미 값이 있으면(사용자가 수정했거나 이전에 채워졌으면) 건드리지 않는다.
        if "recipe_cuisine" not in st.session_state:
            user = current_user()
            saved = db.get_user_by_id(user["id"]) if user else None

            def _default(saved_value, options):
                return saved_value if saved_value in options else options[0]

            st.session_state.recipe_cuisine = _default(saved and saved.get("default_cuisine"), CUISINE_OPTIONS)
            st.session_state.recipe_difficulty = _default(
                saved and saved.get("default_difficulty"), DIFFICULTY_OPTIONS
            )
            st.session_state.recipe_time = _default(saved and saved.get("default_time"), TIME_OPTIONS)
            st.session_state.recipe_servings = _default(saved and saved.get("default_servings"), SERVINGS_OPTIONS)

        opt_cols = st.columns(4)
        cuisine = opt_cols[0].selectbox("요리 종류", CUISINE_OPTIONS, key="recipe_cuisine")
        difficulty = opt_cols[1].selectbox("난이도", DIFFICULTY_OPTIONS, key="recipe_difficulty")
        time_pref = opt_cols[2].selectbox("조리 시간", TIME_OPTIONS, key="recipe_time")
        servings = opt_cols[3].selectbox("인원", SERVINGS_OPTIONS, key="recipe_servings")
        recipe_options = {
            "cuisine": cuisine,
            "difficulty": difficulty,
            "time_pref": time_pref,
            "servings": servings,
        }

        now = time.time()
        last_request = st.session_state.get("last_recipe_request_time", 0)
        elapsed = now - last_request
        can_request = elapsed >= RECIPE_REQUEST_COOLDOWN_SECONDS

        button_label = "다시 추천받기" if "recipes" in st.session_state else "레시피 생성하기"
        if not can_request:
            st.caption(f"{RECIPE_REQUEST_COOLDOWN_SECONDS - elapsed:.0f}초 후 다시 시도할 수 있습니다.")

        if not ingredient_names:
            button_help = "재료를 1개 이상 추가해야 레시피를 생성할 수 있습니다."
        elif not can_request:
            button_help = f"{RECIPE_REQUEST_COOLDOWN_SECONDS - elapsed:.0f}초 후 다시 시도할 수 있습니다."
        else:
            button_help = None
        manual_click = st.button(
            button_label,
            type="primary",
            disabled=not can_request or not ingredient_names,
            help=button_help,
        )

        if manual_click:
            st.session_state.last_recipe_request_time = time.time()
            with st.status("레시피를 생성하는 중입니다...", expanded=True) as status:
                try:
                    response, used_recipe_model = generate_recipes(
                        ingredient_names,
                        api_key,
                        on_attempt=lambda label, model: status.write(f"{label}: `{model}` 호출 중..."),
                        allowed_models=get_allowed_models("selected_text_models"),
                        **recipe_options,
                    )
                except requests.exceptions.Timeout:
                    status.update(label="생성 실패", state="error")
                    st.session_state.pop("recipes", None)
                    st.session_state.pop("selected_recipe", None)
                    st.error("❌ 레시피 생성 실패: 모델이 제한 시간 안에 응답하지 않았습니다. 잠시 후 다시 시도해주세요.")
                    st.stop()
                except requests.exceptions.RequestException as e:
                    status.update(label="생성 실패", state="error")
                    st.session_state.pop("recipes", None)
                    st.session_state.pop("selected_recipe", None)
                    st.error(f"❌ 레시피 생성 실패: 네트워크 오류가 발생했습니다: {e}")
                    st.stop()
                status.update(label="생성 완료", state="complete")

            st.session_state.used_recipe_model = used_recipe_model

            if response.status_code == 429:
                st.session_state.pop("recipes", None)
                st.session_state.pop("selected_recipe", None)
                st.error(rate_limit_message(response, "레시피 생성"))
                with st.expander("자세히 보기 (기술 정보)"):
                    st.code(response.text)
            elif response.status_code >= 500:
                st.session_state.pop("recipes", None)
                st.session_state.pop("selected_recipe", None)
                st.error("❌ 레시피 생성 실패: 모델 제공자 서버 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
            elif response.status_code != 200:
                st.session_state.pop("recipes", None)
                st.session_state.pop("selected_recipe", None)
                st.error(f"❌ 레시피 생성 실패: 요청이 실패했습니다 (status {response.status_code}).")
                with st.expander("자세히 보기 (기술 정보)"):
                    st.code(response.text)
            else:
                body = response.json()
                choices = body.get("choices")
                if not choices:
                    st.session_state.pop("recipes", None)
                    st.session_state.pop("selected_recipe", None)
                    st.error("❌ 레시피 생성 실패: 모델 응답 형식이 올바르지 않습니다 (choices 없음). 잠시 후 다시 시도해주세요.")
                    with st.expander("자세히 보기 (기술 정보)"):
                        st.code(response.text)
                else:
                    content = choices[0]["message"]["content"]
                    recipes = parse_recipes(content)
                    if not recipes:
                        st.session_state.pop("recipes", None)
                        st.session_state.pop("selected_recipe", None)
                        st.error("❌ 레시피 생성 실패: 서로 다른 모델 3차 시도 모두 응답을 레시피로 변환하지 못했습니다.")
                        with st.expander("자세히 보기 (기술 정보)"):
                            st.code(content)
                    else:
                        st.session_state.recipes = recipes
                        st.session_state.selected_recipe = 0
                        # 인디케이터(③ 레시피 추천)는 화면 상단, 이 성공 처리보다 먼저 그려진다.
                        # 여기서 즉시 rerun해야 인디케이터가 방금 세팅된 recipes를 반영해 체크 표시로 바뀐다.
                        st.rerun()

        if st.session_state.get("used_recipe_model"):
            st.caption(f"실제 응답 모델: `{st.session_state.used_recipe_model}`")

        if st.session_state.get("recipes"):
            st.caption("💡 매 요청마다 다른 레시피가 나올 수 있어요. 마음에 안 들면 다시 추천받아보세요.")
            cols = st.columns(len(st.session_state.recipes))
            for idx, (col, r) in enumerate(zip(cols, st.session_state.recipes)):
                with col:
                    with st.container(border=True, height=260):
                        st.markdown(f"**{r.get('name', '이름 없음')}**")
                        st.write(f"⏱ 약 {r.get('cook_time_minutes', '?')}분")
                        missing = r.get("missing_ingredients") or []
                        if missing:
                            st.warning("부족: " + ", ".join(missing))
                        else:
                            st.success("모든 재료 보유!")
                        if st.button("자세히 보기", key=f"select_recipe_{idx}"):
                            st.session_state.selected_recipe = idx

            selected_idx = st.session_state.get("selected_recipe")
            if selected_idx is not None and 0 <= selected_idx < len(st.session_state.recipes):
                recipe = st.session_state.recipes[selected_idx]
                st.divider()
                detail_header_col, detail_back_col = st.columns([4, 1])
                detail_header_col.markdown(f"### 📖 {recipe.get('name', '이름 없음')}")
                if detail_back_col.button("← 목록으로"):
                    st.session_state.pop("selected_recipe", None)
                    st.rerun()

                st.write("**사용 재료**: " + ", ".join(recipe.get("used_ingredients") or []))
                missing = recipe.get("missing_ingredients") or []
                if missing:
                    st.warning("**부족한 재료**: " + ", ".join(missing))
                else:
                    st.success("모든 재료를 보유하고 있습니다!")
                st.write("**조리 순서**")
                for step_idx, step in enumerate(recipe.get("steps") or [], start=1):
                    st.write(f"{step_idx}. {step}")

                if st.button("레시피 저장", key="save_recipe"):
                    user = current_user()
                    if not user:
                        st.warning("로그인 후 저장할 수 있습니다. 왼쪽 사이드바에서 로그인/회원가입해주세요.")
                    else:
                        db.save_recipe(user["id"], recipe)
                        st.success(f"'{recipe.get('name')}' 레시피를 저장했습니다. 사이드바의 '내 레시피'에서 확인하세요.")
