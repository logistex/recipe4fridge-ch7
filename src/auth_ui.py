import streamlit as st

import auth
import db
from ui_common import DB_UNAVAILABLE_MESSAGE, clear_app_flow_state, escape_markdown

# 스키마 준비(init_db)가 끝났는지. 실패하면 다음 리런에서 다시 시도한다 (DB가 잠깐 내려간 경우).
_db_initialized = False


def _ensure_db_ready():
    """DB 스키마를 준비한다. 연결에 실패하면 예외 대신 False를 돌려 화면이 멈추지 않게 한다."""
    global _db_initialized
    if _db_initialized:
        return True
    try:
        db.init_db()
    except db.DatabaseError:
        return False
    _db_initialized = True
    return True


def current_user():
    return st.session_state.get("current_user")


def _sync_google_login():
    """Google 로그인 리다이렉트 후 돌아왔을 때, st.user 정보를 우리 DB/세션과 연결한다."""
    if current_user() is not None:
        return
    if not getattr(st.user, "is_logged_in", False):
        return

    info = st.user.to_dict()
    email = info.get("email")
    if not email:
        return
    nickname = info.get("name") or email.split("@")[0]
    google_user = db.get_or_create_google_user(email, nickname)
    st.session_state.current_user = {
        "id": google_user["id"],
        "email": google_user["email"],
        "nickname": google_user["nickname"],
        "via_google": True,
    }


def render_sidebar_auth():
    """사이드바에 로그인 상태/로그인·회원가입 폼을 그린다. 모든 페이지에서 공통으로 호출한다.

    반환값: DB에 연결할 수 있으면 True. False면 로그인·저장 기능을 쓸 수 없는 상태다.
    """
    db_ready = _ensure_db_ready()
    if db_ready:
        try:
            _sync_google_login()
        except db.DatabaseError:
            db_ready = False
    # st.rerun() 직전에 띄운 알림은 리런과 함께 사라지므로, 다음 실행에서 한 번 보여준다.
    notice = st.session_state.pop("auth_notice", None)
    if notice:
        st.toast(notice, icon="✅")
    # 모바일 터치 타겟 권장 크기(44px) 확보. 모든 페이지에서 공통으로 적용되도록 여기서 주입한다.
    st.markdown(
        """
        <style>
        [data-testid="stBaseButton-secondary"],
        [data-testid="stBaseButton-secondaryFormSubmit"],
        [data-testid="stBaseButton-primary"],
        [data-testid="stBaseButton-primaryFormSubmit"] {
            min-height: 44px;
        }
        [data-testid="stNumberInputStepUp"],
        [data-testid="stNumberInputStepDown"] {
            min-width: 44px;
            min-height: 44px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    with st.sidebar:
        user = current_user()
        if user:
            # 닉네임·이메일은 사용자가 입력한 값이라 마크다운(밑줄 → 기울임 등)으로 해석되지 않게 한다.
            st.markdown(f"👋 **{escape_markdown(user['nickname'])}**님")
            st.caption(escape_markdown(user["email"]))
            if st.button("로그아웃", key="logout_button"):
                if user.get("via_google"):
                    # st.logout()이 자체적으로 세션을 새로 시작시키므로 별도 rerun/정리가 불필요하다.
                    st.logout()
                else:
                    st.session_state.pop("current_user", None)
                    clear_app_flow_state()
                    st.rerun()
        elif not db_ready:
            st.subheader("로그인 / 회원가입")
            st.warning(
                DB_UNAVAILABLE_MESSAGE
                + " 사진으로 식재료를 인식하고 레시피를 추천받는 기능은 로그인 없이 계속 쓸 수 있습니다."
            )
        else:
            st.subheader("로그인 / 회원가입")
            st.caption("로그인하면 마음에 드는 레시피를 저장하고 '내 레시피'에서 다시 볼 수 있습니다.")

            if st.button("Google로 로그인", key="google_login_button"):
                try:
                    st.login()
                except Exception as e:
                    st.error("Google 로그인을 사용할 수 없습니다. 잠시 후 다시 시도하거나 이메일로 로그인해 주세요.")
                    with st.expander("자세히 보기 (기술 정보)"):
                        st.code(type(e).__name__)

            st.divider()

            tab_login, tab_signup = st.tabs(["로그인", "회원가입"])

            with tab_login:
                with st.form("login_form"):
                    login_email = st.text_input("이메일", key="login_email", placeholder="name@example.com")
                    login_password = st.text_input("비밀번호", type="password", key="login_password")
                    submitted = st.form_submit_button("로그인", type="primary")
                    if submitted:
                        try:
                            ok, result = auth.login(login_email, login_password)
                        except db.DatabaseError:
                            ok, result = False, DB_UNAVAILABLE_MESSAGE
                        if ok:
                            st.session_state.current_user = result
                            st.session_state.auth_notice = f"{result['nickname']}님, 환영합니다."
                            st.rerun()
                        else:
                            st.error(result)

            with tab_signup:
                with st.form("signup_form"):
                    signup_email = st.text_input("이메일", key="signup_email", placeholder="name@example.com")
                    signup_nickname = st.text_input("닉네임", key="signup_nickname")
                    signup_password = st.text_input(
                        "비밀번호 (8자 이상)", type="password", key="signup_password", help=auth.PASSWORD_RULE_HELP
                    )
                    submitted = st.form_submit_button("회원가입", type="primary")
                    if submitted:
                        try:
                            ok, result = auth.signup(signup_email, signup_password, signup_nickname)
                        except db.DatabaseError:
                            ok, result = False, DB_UNAVAILABLE_MESSAGE
                        if ok:
                            st.session_state.current_user = result
                            st.session_state.auth_notice = "회원가입이 완료되었습니다."
                            st.rerun()
                        else:
                            st.error(result)
