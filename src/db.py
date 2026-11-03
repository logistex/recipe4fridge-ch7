import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import psycopg2.pool
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# 첫 연결(TCP+TLS)이 응답 없이 무한정 매달리지 않도록 하는 상한. libpq 기본값은 무제한이다.
DB_CONNECT_TIMEOUT_SECONDS = 10
# 풀에 반납된 지 이 시간이 안 된 커넥션은 살아있다고 보고 SELECT 1 점검(왕복 1회)을 건너뛴다.
DB_PING_IF_IDLE_SECONDS = 30


def _database_url():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    try:
        return st.secrets["DATABASE_URL"]
    except (FileNotFoundError, KeyError):
        st.error("DATABASE_URL이 설정되지 않았습니다. .env 또는 Streamlit secrets를 확인해주세요.")
        st.stop()


class _TrackedConnection(psycopg2.extensions.connection):
    """마지막으로 풀에 반납된 시각(last_used)을 기록할 수 있는 커넥션."""

    last_used = 0.0


@st.cache_resource
def _connection_pool():
    """프로세스당 커넥션 풀 하나. 쿼리마다 새로 연결(TCP+TLS 핸드셰이크)하는 비용을 없앤다.

    Streamlit은 세션마다 별도 스레드에서 스크립트를 실행하므로 스레드 안전한 풀을 쓴다.
    keepalives는 풀에 오래 놀고 있던 커넥션이 서버 쪽에서 끊기는 것을 줄이기 위한 설정.

    tcp_user_timeout이 없으면, 죽은 소켓에 쿼리를 보냈을 때 TCP 기본값 때문에 10분 넘게
    매달린 뒤에야 "could not send data to server: Connection timed out"으로 실패한다.
    Streamlit Community Cloud는 트래픽이 없으면 앱 컨테이너를 재우는데, 자는 동안에는
    keepalive 패킷도 나가지 않아 풀 안의 커넥션이 통째로 죽은 채 남는다.
    """
    return psycopg2.pool.ThreadedConnectionPool(
        1,
        5,
        _database_url(),
        connection_factory=_TrackedConnection,
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=DB_CONNECT_TIMEOUT_SECONDS,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
        tcp_user_timeout=8000,
    )


def _checkout_live_connection():
    """풀에서 살아있는 커넥션을 꺼낸다. 죽어있으면 풀을 통째로 버리고 새로 만든다.

    앱이 잠들었다 깨어나면 풀 안의 커넥션이 하나가 아니라 전부 같은 이유로 죽어 있다.
    그래서 커넥션 하나만 교체하지 않고 풀 전체를 폐기한다.

    점검(SELECT 1)은 쿼리마다 DB 왕복을 한 번 더 늘리므로, 반납된 지
    DB_PING_IF_IDLE_SECONDS가 안 된 커넥션은 점검 없이 바로 쓴다. 잠들었다 깨어난 경우는
    유휴 시간이 길어 항상 점검 대상이 된다.
    """
    for last_attempt in (False, True):
        pool = _connection_pool()
        conn = pool.getconn()
        idle_seconds = time.time() - conn.last_used
        if not conn.closed and 0 <= idle_seconds < DB_PING_IF_IDLE_SECONDS:
            return pool, conn
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            return pool, conn
        except psycopg2.Error:
            try:
                pool.closeall()
            except psycopg2.Error:
                pass
            _connection_pool.clear()
            if last_attempt:
                raise


@contextmanager
def get_connection():
    pool, conn = _checkout_live_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.last_used = time.time()
        # putconn은 트랜잭션이 남아있으면 롤백하고, 끊어진 커넥션은 닫아 풀에서 제거한다.
        pool.putconn(conn)


def init_db():
    # 앱 시작 시 DB 왕복을 한 번으로 줄이기 위해 스키마 문장을 하나로 묶어 보낸다.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT,
                    nickname TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                -- Google 로그인 사용자는 비밀번호가 없으므로, 기존에 NOT NULL로 만들어진
                -- 테이블이 있다면 nullable로 완화한다. 이미 nullable이면 테이블 락이 걸리는
                -- ALTER TABLE 자체를 실행하지 않는다.
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'users'
                          AND column_name = 'password_hash'
                          AND is_nullable = 'NO'
                    ) THEN
                        ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;
                    END IF;
                END $$;

                -- 레시피 추천 기본 조건(프로필에서 설정, 레시피 화면 기본값으로 사용)과
                -- 사용자가 프로필에서 고른 AI 모델 목록(JSON 배열 문자열. NULL/빈 값이면 전체 무료 모델 사용).
                ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS default_cuisine TEXT,
                    ADD COLUMN IF NOT EXISTS default_difficulty TEXT,
                    ADD COLUMN IF NOT EXISTS default_time TEXT,
                    ADD COLUMN IF NOT EXISTS default_servings TEXT,
                    ADD COLUMN IF NOT EXISTS selected_vision_models TEXT,
                    ADD COLUMN IF NOT EXISTS selected_text_models TEXT;

                CREATE TABLE IF NOT EXISTS saved_recipes (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    name TEXT,
                    used_ingredients TEXT,
                    missing_ingredients TEXT,
                    steps TEXT,
                    cook_time_minutes INTEGER,
                    saved_at TEXT NOT NULL
                );
                """
            )


def _now():
    return datetime.now(timezone.utc).isoformat()


def email_exists(email):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE email = %s", (email,))
            return cur.fetchone() is not None


def create_user(email, password_hash, nickname):
    user_id = str(uuid.uuid4())
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, password_hash, nickname, created_at) VALUES (%s, %s, %s, %s, %s)",
                (user_id, email, password_hash, nickname, _now()),
            )
    return user_id


def get_user_by_email(email):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_user_by_id(user_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_or_create_google_user(email, nickname):
    """Google 로그인 사용자를 이메일로 조회하고, 처음 로그인이면 비밀번호 없이 새로 만든다."""
    user = get_user_by_email(email)
    if user:
        return user
    user_id = create_user(email, None, nickname)
    return get_user_by_id(user_id)


def update_nickname(user_id, nickname):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET nickname = %s WHERE id = %s", (nickname, user_id))


def update_recipe_preferences(user_id, cuisine, difficulty, time_pref, servings):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET default_cuisine = %s, default_difficulty = %s, default_time = %s, default_servings = %s
                WHERE id = %s
                """,
                (cuisine, difficulty, time_pref, servings, user_id),
            )


def update_model_selection(user_id, vision_model_ids, text_model_ids):
    """모델 선택을 저장한다. 빈 리스트를 넘기면 NULL로 저장되어 '전체 사용'으로 취급된다."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET selected_vision_models = %s, selected_text_models = %s WHERE id = %s",
                (
                    json.dumps(vision_model_ids, ensure_ascii=False) if vision_model_ids else None,
                    json.dumps(text_model_ids, ensure_ascii=False) if text_model_ids else None,
                    user_id,
                ),
            )


def parse_model_id_list(raw):
    """users.selected_vision_models/selected_text_models(JSON 문자열)를 리스트로 파싱한다.

    비어있거나 잘못된 값이면 None을 반환한다 (호출부에서 '전체 사용'으로 해석).
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, list) and parsed else None


def save_recipe(user_id, recipe):
    recipe_id = str(uuid.uuid4())
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO saved_recipes
                    (id, user_id, name, used_ingredients, missing_ingredients, steps, cook_time_minutes, saved_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    recipe_id,
                    user_id,
                    recipe.get("name"),
                    json.dumps(recipe.get("used_ingredients") or [], ensure_ascii=False),
                    json.dumps(recipe.get("missing_ingredients") or [], ensure_ascii=False),
                    json.dumps(recipe.get("steps") or [], ensure_ascii=False),
                    recipe.get("cook_time_minutes"),
                    _now(),
                ),
            )
    return recipe_id


def list_recipes(user_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM saved_recipes WHERE user_id = %s ORDER BY saved_at DESC", (user_id,)
            )
            rows = cur.fetchall()
    recipes = []
    for row in rows:
        recipe = dict(row)
        recipe["used_ingredients"] = json.loads(recipe["used_ingredients"] or "[]")
        recipe["missing_ingredients"] = json.loads(recipe["missing_ingredients"] or "[]")
        recipe["steps"] = json.loads(recipe["steps"] or "[]")
        recipes.append(recipe)
    return recipes


def delete_recipe(user_id, recipe_id):
    """user_id가 일치하는 경우에만 삭제한다 (권한 검사)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM saved_recipes WHERE id = %s AND user_id = %s", (recipe_id, user_id)
            )
            return cur.rowcount > 0
