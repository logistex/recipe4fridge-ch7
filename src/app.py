import html
import os
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st
from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError

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
from ui_common import (
    as_text_list,
    clear_app_flow_state,
    cook_time_to_int,
    escape_markdown,
    format_cook_time,
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
# 레시피 카드를 한 줄에 몇 개까지 둘지. 모델이 더 많이 주면 다음 줄로 넘겨 카드가 좁아지지 않게 한다.
RECIPE_CARDS_PER_ROW = 3
# 기술 정보 expander에 보여줄 응답 본문 최대 길이.
TECH_DETAIL_MAX_CHARS = 2000

# 손상되었거나 이미지가 아닌 파일을 열 때 Pillow가 내는 예외들.
IMAGE_ERRORS = (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError)
IMAGE_OPEN_ERROR_MESSAGE = (
    "❌ 이 사진 파일을 열 수 없습니다. 파일이 손상되었거나 JPG·PNG가 아닌 형식일 수 있습니다. "
    "사진을 다시 저장하거나 다른 사진을 올려 주세요."
)


def rate_limit_message(response, action, attempt_count):
    """429 응답을 계정 일일 한도와 모델 순간 혼잡으로 구분해 안내 문구를 만든다.

    둘은 사용자가 할 일이 다르다. 일일 한도는 초기화될 때까지 다시 눌러도 소용없고,
    순간 혼잡은 잠시 뒤 다시 시도하면 대체로 풀린다.
    """
    error = error_info(response)
    metadata = error.get("metadata") or {}
    if not is_daily_rate_limit(error):
        return (
            f"❌ {action} 실패: 무료 AI 모델이 혼잡해 응답을 받지 못했습니다 ({attempt_count}번 시도). "
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


def describe_failure(action, response, error, attempted_models, content=None, parse_target="", parse_next_step=""):
    """실패한 AI 호출을 사용자 안내(무슨 일 + 어떻게 하면 되는지)와 기술 정보로 나눠 정리한다.

    response/error는 폴백 루프가 돌려준 마지막 시도의 결과다 (앞선 시도의 결과는 따로 남지 않는다).
    반환값: {"message": 화면 안내, "detail": expander용 기술 정보, "retryable": 다시 시도할 의미가 있는지}
    """
    count = len(attempted_models)
    retryable = True
    if isinstance(error, requests.exceptions.Timeout):
        message = (
            f"❌ {action} 실패: AI 모델이 제한 시간 안에 응답하지 않았습니다 ({count}번 시도). "
            "무료 모델이 붐비는 시간일 수 있으니 1~2분 뒤 다시 시도해 주세요."
        )
        last_result = f"시간 초과 ({type(error).__name__})"
    elif error is not None:
        message = (
            f"❌ {action} 실패: AI 서버에 연결하지 못했습니다. "
            "인터넷 연결을 확인한 뒤 다시 시도해 주세요."
        )
        last_result = f"연결 오류 ({type(error).__name__})"
    else:
        status = response.status_code
        last_result = f"HTTP {status}"
        if status == 401:
            retryable = False
            message = (
                f"❌ {action} 실패: AI 서비스 인증에 실패했습니다 (API 키 문제). "
                "다시 시도해도 해결되지 않으니 앱 관리자에게 알려 주세요."
            )
        elif status == 402:
            retryable = False
            message = (
                f"❌ {action} 실패: AI 서비스 이용 크레딧이 부족합니다. "
                "다시 시도해도 해결되지 않으니 앱 관리자에게 알려 주세요."
            )
        elif status == 403:
            message = (
                f"❌ {action} 실패: AI 모델이 이 요청을 거부했습니다. "
                "다시 시도해 보고, 같은 일이 반복되면 다른 사진이나 재료로 시도해 주세요."
            )
        elif status == 404:
            message = (
                f"❌ {action} 실패: 사용하려던 AI 모델을 지금은 쓸 수 없습니다. "
                "프로필의 'AI 모델 선택'에서 다른 모델을 고르거나 선택을 모두 해제한 뒤 다시 시도해 주세요."
            )
        elif status == 429:
            retryable = not is_daily_rate_limit(error_info(response))
            message = rate_limit_message(response, action, count)
        elif status >= 500:
            message = (
                f"❌ {action} 실패: AI 모델 서버에 일시적인 문제가 생겼습니다 ({count}번 시도). "
                "잠시 후 다시 시도해 주세요."
            )
        elif status != 200:
            message = f"❌ {action} 실패: 요청을 처리하지 못했습니다 (오류 코드 {status}). 잠시 후 다시 시도해 주세요."
        else:
            last_result = "HTTP 200 (응답 형식 오류)"
            message = f"❌ {action} 실패: AI 응답을 {parse_target} 정리하지 못했습니다 ({count}번 시도). {parse_next_step}"

    lines = [f"시도한 모델 ({count}번):"]
    lines += [f"  {i}차: {model}" for i, model in enumerate(attempted_models, start=1)]
    lines.append(f"마지막 시도 결과: {last_result}")
    if error is not None:
        lines.append(f"오류 내용: {str(error)[:300]}")
    else:
        body = content if content is not None else response.text
        lines += ["", "마지막 응답 본문:", (body or "(비어 있음)")[:TECH_DETAIL_MAX_CHARS]]
    return {"message": message, "detail": "\n".join(lines), "retryable": retryable}


def show_failure(failure):
    st.error(failure["message"])
    if failure.get("detail"):
        with st.expander("자세히 보기 (기술 정보)"):
            st.code(failure["detail"], language=None)


def _response_content(response):
    """성공(200) 응답에서 모델이 쓴 본문을 꺼낸다. 형식이 다르거나 JSON이 아니면 None."""
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) else None


def _safe_parse(parser, content):
    if content is None:
        return None
    try:
        return parser(content)
    except (TypeError, ValueError, AttributeError):
        return None


def _attempt_logger(status, attempted_models):
    """폴백 시도마다 진행 상황을 상태 상자에 쓰고, 시도한 모델을 기록하는 콜백을 만든다."""
    def on_attempt(label, model):
        attempted_models.append(model)
        status.write(f"{label}: `{model}` 모델에 요청하는 중...")
    return on_attempt


def _is_readable_image(uploaded_file):
    try:
        with Image.open(uploaded_file) as image:
            image.verify()
        return True
    except IMAGE_ERRORS:
        return False
    finally:
        uploaded_file.seek(0)


def _file_id(uploaded_file):
    return getattr(uploaded_file, "file_id", None) or f"{uploaded_file.name}:{uploaded_file.size}"


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
        st.error("OPENROUTER_API_KEY가 설정되지 않았습니다. .env 또는 Streamlit secrets를 확인해 주세요.")
        st.stop()


def _saved_user_profile():
    """로그인한 사용자의 DB 저장 정보(기본 조건/모델 선택). 로그인 전이거나 DB에 연결할 수 없으면 None.

    DB가 잠시 내려가 있어도 인식·추천은 기본 설정으로 계속 쓸 수 있게 오류를 삼킨다.
    """
    user = current_user()
    if not user:
        return None
    try:
        return db.get_user_by_id(user["id"])
    except db.DatabaseError:
        return None


def get_allowed_models(field_name):
    """로그인한 사용자가 프로필에서 선택해둔 모델 목록을 읽어온다. 없으면 None(전체 사용)."""
    saved = _saved_user_profile()
    if not saved:
        return None
    return db.parse_model_id_list(saved.get(field_name))


def run_recognition(uploaded_file, api_key):
    """사진에서 식재료를 인식한다. 성공하면 2단계로 넘어가고, 실패하면 원인 안내를 남긴 뒤 리런한다."""
    for key in ("recognition_error", "used_vision_model", "ingredients", "recipes", "selected_recipe"):
        st.session_state.pop(key, None)
    file_id = _file_id(uploaded_file)
    attempted_models = []
    response = error = content = ingredients = None

    with st.status("사진 속 식재료를 찾는 중입니다... (보통 20초~1분 걸립니다)", expanded=True) as status:
        try:
            # 2단계에서 리런마다 원본을 다시 줄이지 않도록 표시용 크기로 한 번만 줄여 보관한다.
            st.session_state.uploaded_photo_bytes = to_display_bytes(uploaded_file.getvalue())
            st.session_state.uploaded_photo_name = uploaded_file.name
            data_uri, resized_bytes = to_resized_data_uri(uploaded_file)
        except IMAGE_ERRORS:
            status.update(label="사진을 열지 못했습니다", state="error")
            st.session_state.recognition_error = {
                "message": IMAGE_OPEN_ERROR_MESSAGE,
                "detail": None,
                "retryable": False,
                "file_id": file_id,
            }
            st.rerun()
        status.write(
            f"사진 크기 줄이기: {uploaded_file.size // 1024}KB → {resized_bytes // 1024}KB "
            f"(긴 변 {MAX_IMAGE_DIMENSION}px 이하)"
        )
        try:
            response, used_model = recognize_ingredients(
                data_uri,
                api_key,
                on_attempt=_attempt_logger(status, attempted_models),
                allowed_models=get_allowed_models("selected_vision_models"),
            )
        except requests.exceptions.RequestException as e:
            error = e
        if error is None and response.status_code == 200:
            content = _response_content(response)
            ingredients = _safe_parse(parse_ingredients, content)
        if ingredients is not None:
            status.update(label=f"식재료 {len(ingredients)}개를 찾았습니다", state="complete")
        else:
            status.update(label="식재료를 인식하지 못했습니다", state="error")

    if ingredients is not None:
        st.session_state.used_vision_model = used_model
        st.session_state.ingredients = ingredients
        st.session_state.wizard_step = 2
    else:
        failure = describe_failure(
            "식재료 인식",
            response,
            error,
            attempted_models,
            content,
            parse_target="재료 목록으로",
            parse_next_step="식재료가 잘 보이도록 밝게 찍은 사진으로 다시 시도하거나, 재료를 직접 입력해 주세요.",
        )
        failure["file_id"] = file_id
        st.session_state.recognition_error = failure
    st.rerun()


def render_ingredient_editor():
    """② 식재료 확인 섹션. 2단계와 3단계 화면에서 공통으로 사용한다.

    재료 한 개당 한 줄(이름/수량/단위/삭제)로 배치한다 (추가 폼과 동일한 형태).
    """
    st.subheader("② 식재료 확인")
    st.caption("⚠️ AI 인식 결과는 부정확할 수 있습니다. 이름/수량/단위를 확인하고 필요하면 직접 수정해 주세요.")

    ingredients = st.session_state.get("ingredients", [])
    if not ingredients:
        st.info("아직 목록이 비어 있습니다. 아래에서 직접 추가해 주세요.")

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
    clear_app_flow_state()
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


def _go_to_manual_entry():
    """인식 없이 재료를 직접 입력하는 2단계로 넘어간다."""
    st.session_state.ingredients = []
    st.session_state.pop("recognition_error", None)
    st.session_state.pop("used_vision_model", None)
    st.session_state.wizard_step = 2
    st.rerun()


# ==================== 1단계 화면: 사진 업로드(좌) + 식재료 인식(우) ====================
if wizard_step == 1:
    st.subheader("① 사진 업로드")
    if st.session_state.get("ingredients"):
        # 2단계에서 "이전"으로 돌아온 경우, 다시 인식하지 않고 기존 목록으로 계속할 길을 남긴다.
        if st.button("지금 재료 목록으로 계속하기 →"):
            st.session_state.wizard_step = 2
            st.rerun()
    uploaded_file = st.file_uploader(
        "냉장고 사진을 업로드하거나, 이 영역으로 파일을 끌어다 놓으세요",
        type=["jpg", "jpeg", "png"],
        key=f"fridge_photo_{st.session_state.uploader_key}",
        help="JPG·PNG 사진을 10MB 이하로 올려 주세요. 냉장고 안이 밝고 선명하게 보일수록 잘 인식됩니다.",
    )

    if uploaded_file is None:
        st.caption("사진이 없으면 재료를 직접 입력해서 레시피를 추천받을 수도 있습니다.")
        if st.button("사진 없이 재료 직접 입력하기"):
            st.session_state.pop("uploaded_photo_bytes", None)
            st.session_state.pop("uploaded_photo_name", None)
            _go_to_manual_entry()
    else:
        if uploaded_file.size > MAX_UPLOAD_BYTES:
            st.error("이미지 용량이 10MB를 초과합니다. 더 작은 이미지를 업로드해 주세요.")
            st.stop()
        # 손상된 파일은 st.image에서도 예외가 나므로, 그리기 전에 먼저 열어본다.
        if not _is_readable_image(uploaded_file):
            st.error(IMAGE_OPEN_ERROR_MESSAGE)
            st.stop()

        photo_col, recognize_col = st.columns(2)

        with photo_col:
            st.image(uploaded_file, caption="업로드한 사진", use_container_width=True)

        with recognize_col:
            failure = st.session_state.get("recognition_error")
            # 다른 사진을 새로 올렸으면 이전 사진의 실패 안내는 보여주지 않는다.
            if failure and failure.get("file_id") != _file_id(uploaded_file):
                failure = None

            if failure is None:
                if st.button("식재료 인식하기", type="primary"):
                    run_recognition(uploaded_file, api_key)
                if st.session_state.get("ingredients"):
                    st.caption("다시 인식하면 지금 재료 목록이 새 인식 결과로 바뀝니다.")
                else:
                    st.caption("인식 결과는 다음 단계에서 직접 고칠 수 있습니다.")
            else:
                show_failure(failure)
                if failure.get("retryable"):
                    if st.button("다시 시도하기", type="primary"):
                        run_recognition(uploaded_file, api_key)
                if st.button("재료를 직접 입력할게요"):
                    _go_to_manual_entry()

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
        else:
            st.caption("업로드한 사진 없이 진행 중입니다. 재료를 직접 입력해 주세요.")
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
        # 재료 이름은 사용자 입력이거나 AI 출력이므로 HTML로 해석되지 않게 이스케이프한다.
        chips_html = "".join(
            f'<span class="ingredient-chip">{html.escape(name)}</span>' for name in ingredient_names
        )
        st.markdown(chips_html or "_재료를 먼저 추가해 주세요._", unsafe_allow_html=True)

        # 이번 세션에서 처음 진입한 경우, 프로필에 저장된 기본 조건을 초깃값으로 채운다.
        # 이미 값이 있으면(사용자가 수정했거나 이전에 채워졌으면) 건드리지 않는다.
        if "recipe_cuisine" not in st.session_state:
            saved = _saved_user_profile()

            def _default(saved_value, options):
                return saved_value if saved_value in options else options[0]

            st.session_state.recipe_cuisine = _default(saved and saved.get("default_cuisine"), CUISINE_OPTIONS)
            st.session_state.recipe_difficulty = _default(
                saved and saved.get("default_difficulty"), DIFFICULTY_OPTIONS
            )
            st.session_state.recipe_time = _default(saved and saved.get("default_time"), TIME_OPTIONS)
            st.session_state.recipe_servings = _default(saved and saved.get("default_servings"), SERVINGS_OPTIONS)

        # 화면 절반 폭에 4칸을 나란히 두면 칸이 너무 좁아져서 2칸씩 두 줄로 배치한다.
        opt_row1 = st.columns(2)
        cuisine = opt_row1[0].selectbox("요리 종류", CUISINE_OPTIONS, key="recipe_cuisine")
        difficulty = opt_row1[1].selectbox("난이도", DIFFICULTY_OPTIONS, key="recipe_difficulty")
        opt_row2 = st.columns(2)
        time_pref = opt_row2[0].selectbox("조리 시간", TIME_OPTIONS, key="recipe_time")
        servings = opt_row2[1].selectbox("인원", SERVINGS_OPTIONS, key="recipe_servings")
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
            attempted_models = []
            response = error = content = recipes = None
            with st.status("레시피를 만드는 중입니다... (보통 10초~1분 걸립니다)", expanded=True) as status:
                try:
                    response, used_recipe_model = generate_recipes(
                        ingredient_names,
                        api_key,
                        on_attempt=_attempt_logger(status, attempted_models),
                        allowed_models=get_allowed_models("selected_text_models"),
                        **recipe_options,
                    )
                except requests.exceptions.RequestException as e:
                    error = e
                if error is None and response.status_code == 200:
                    content = _response_content(response)
                    parsed = _safe_parse(parse_recipes, content)
                    recipes = [r for r in parsed or [] if isinstance(r, dict)]
                if recipes:
                    status.update(label=f"레시피 {len(recipes)}개를 만들었습니다", state="complete")
                else:
                    status.update(label="레시피를 만들지 못했습니다", state="error")

            if recipes:
                st.session_state.recipes = recipes
                st.session_state.selected_recipe = 0
                st.session_state.used_recipe_model = used_recipe_model
                # 인디케이터(③ 레시피 추천)는 화면 상단, 이 성공 처리보다 먼저 그려진다.
                # 여기서 즉시 rerun해야 인디케이터가 방금 세팅된 recipes를 반영해 체크 표시로 바뀐다.
                st.rerun()
            # 실패해도 이전에 받은 추천은 지우지 않는다 ("다시 추천받기"가 실패했다고 기존 결과를 잃지 않도록).
            show_failure(
                describe_failure(
                    "레시피 생성",
                    response,
                    error,
                    attempted_models,
                    content,
                    parse_target="레시피로",
                    parse_next_step=f"{RECIPE_REQUEST_COOLDOWN_SECONDS}초 뒤 위의 버튼으로 다시 시도해 주세요.",
                )
            )
            if st.session_state.get("recipes"):
                st.caption("아래는 이전에 받은 추천입니다.")

        if st.session_state.get("used_recipe_model"):
            st.caption(f"실제 응답 모델: `{st.session_state.used_recipe_model}`")

        if st.session_state.get("recipes"):
            st.caption("💡 매 요청마다 다른 레시피가 나올 수 있어요. 마음에 안 들면 다시 추천받아보세요.")
            all_recipes = st.session_state.recipes
            # 모델이 레시피를 3개보다 많이 주면 다음 줄로 넘겨, 카드 폭이 지나치게 좁아지지 않게 한다.
            for row_start in range(0, len(all_recipes), RECIPE_CARDS_PER_ROW):
                row = all_recipes[row_start:row_start + RECIPE_CARDS_PER_ROW]
                cols = st.columns(RECIPE_CARDS_PER_ROW)
                for offset, (col, r) in enumerate(zip(cols, row)):
                    idx = row_start + offset
                    with col:
                        with st.container(border=True, height=260):
                            st.markdown(f"**{escape_markdown(r.get('name') or '이름 없음')}**")
                            st.write(format_cook_time(r.get("cook_time_minutes")))
                            missing = as_text_list(r.get("missing_ingredients"))
                            if missing:
                                st.warning("부족: " + escape_markdown(", ".join(missing)))
                            else:
                                st.success("모든 재료 보유!")
                            if st.button("자세히 보기", key=f"select_recipe_{idx}"):
                                st.session_state.selected_recipe = idx

            selected_idx = st.session_state.get("selected_recipe")
            if selected_idx is not None and 0 <= selected_idx < len(all_recipes):
                recipe = all_recipes[selected_idx]
                recipe_name = recipe.get("name") or "이름 없음"
                st.divider()
                detail_header_col, detail_back_col = st.columns([4, 1])
                detail_header_col.markdown(f"### 📖 {escape_markdown(recipe_name)}")
                if detail_back_col.button("← 목록으로"):
                    st.session_state.pop("selected_recipe", None)
                    st.rerun()

                used = as_text_list(recipe.get("used_ingredients"))
                st.write("**사용 재료**: " + escape_markdown(", ".join(used)))
                missing = as_text_list(recipe.get("missing_ingredients"))
                if missing:
                    st.warning("**부족한 재료**: " + escape_markdown(", ".join(missing)))
                else:
                    st.success("모든 재료를 보유하고 있습니다!")
                st.write("**조리 순서**")
                for step_idx, step in enumerate(as_text_list(recipe.get("steps")), start=1):
                    st.write(f"{step_idx}. {escape_markdown(step)}")

                # 저장 여부는 세션의 레시피 dict에 표시해, 같은 레시피를 여러 번 눌러도 한 번만 저장되게 한다.
                # (db.save_recipe는 정해진 필드만 읽으므로 이 표시는 DB에 들어가지 않는다.)
                if recipe.get("_saved"):
                    st.button("✅ 저장한 레시피", key="save_recipe_done", disabled=True)
                    st.caption("사이드바의 '내 레시피'에서 확인할 수 있습니다.")
                elif st.button("💾 레시피 저장", key="save_recipe"):
                    user = current_user()
                    if not user:
                        st.warning(
                            "로그인 후 저장할 수 있습니다. 왼쪽 사이드바에서 로그인/회원가입해 주세요 "
                            "(모바일에서는 화면 왼쪽 위 › 버튼으로 사이드바를 열 수 있습니다)."
                        )
                    else:
                        try:
                            db.save_recipe(
                                user["id"],
                                # INTEGER 컬럼이라 "15~20" 같은 문자열이 오면 저장이 실패하므로 정수로 맞춘다.
                                {**recipe, "cook_time_minutes": cook_time_to_int(recipe.get("cook_time_minutes"))},
                            )
                        except db.DatabaseError:
                            st.error(
                                "레시피를 저장하지 못했습니다. 저장 서버에 잠시 연결할 수 없는 상태일 수 있으니 "
                                "잠시 후 다시 눌러 주세요."
                            )
                        else:
                            recipe["_saved"] = True
                            st.toast(f"'{recipe_name}' 레시피를 저장했습니다.", icon="✅")
                            st.caption("사이드바의 '내 레시피'에서 확인할 수 있습니다.")
