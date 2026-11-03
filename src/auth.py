import re

import bcrypt

import db

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

GENERIC_LOGIN_ERROR = "이메일 또는 비밀번호가 올바르지 않습니다."

PASSWORD_MIN_LENGTH = 8
# bcrypt는 72바이트까지만 처리한다(5.x는 넘으면 ValueError). 한글은 글자당 3바이트라 24자까지다.
PASSWORD_MAX_BYTES = 72
PASSWORD_RULE_HELP = "8자 이상, 영문·숫자 기준 72자 이하(한글은 24자 이하)로 입력해 주세요."


def hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password, password_hash):
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # 72바이트를 넘는 입력은 가입 단계에서 막히므로, 여기로 왔다면 틀린 비밀번호로 본다.
        return False


def signup(email, password, nickname):
    """성공 시 (True, user_dict), 실패 시 (False, 에러메시지)."""
    email = email.strip().lower()
    nickname = nickname.strip()

    if not EMAIL_PATTERN.match(email):
        return False, "올바른 이메일 형식이 아닙니다."
    if len(password) < PASSWORD_MIN_LENGTH:
        return False, f"비밀번호는 {PASSWORD_MIN_LENGTH}자 이상이어야 합니다."
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        return False, "비밀번호가 너무 깁니다. 영문·숫자 기준 72자 이하(한글은 24자 이하)로 입력해 주세요."
    if not nickname:
        return False, "닉네임을 입력해 주세요."
    if db.email_exists(email):
        return False, "이미 가입된 이메일입니다."

    user_id = db.create_user(email, hash_password(password), nickname)
    return True, {"id": user_id, "email": email, "nickname": nickname}


def login(email, password):
    """성공 시 (True, user_dict), 실패 시 (False, 에러메시지).

    보안: 이메일 존재 여부와 비밀번호 오류를 구분하지 않고 동일한 에러 메시지를 반환한다.
    """
    email = email.strip().lower()
    user = db.get_user_by_email(email)
    # user["password_hash"]가 없으면 Google 등 소셜 로그인 전용 계정이다.
    if not user or not user["password_hash"] or not verify_password(password, user["password_hash"]):
        return False, GENERIC_LOGIN_ERROR

    return True, {"id": user["id"], "email": user["email"], "nickname": user["nickname"]}
