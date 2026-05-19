import os
import logging
from contextlib import contextmanager
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify, abort, request, redirect, url_for, session
from datetime import datetime
from functools import wraps
from psycopg2 import pool, OperationalError
import psycopg2.extras
import bcrypt

from config import EMOJI_MAP

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

try:
    _pool = pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=10,
        host=os.environ.get("DB_HOST"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME"),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASSWORD"),
    )
except OperationalError as e:
    logger.error("DB 연결 풀 생성 실패: %s", e)
    _pool = None


@contextmanager
def get_conn():
    if _pool is None:
        raise RuntimeError("DB 연결 풀이 초기화되지 않았습니다.")
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        _pool.putconn(conn)


def fmt_views(n):
    if n is None:
        return "0"
    n = int(n)
    if n >= 100_000_000:
        return f"{n/100_000_000:.1f}억"
    if n >= 10_000:
        return f"{n/10_000:.0f}만"
    return f"{n:,}"


# 인증 데코레이터
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth"))
        return f(*args, **kwargs)
    return decorated


# 인증 라우트
@app.route("/auth")
def auth():
    if "user_id" in session:
        return redirect(url_for("index"))
    lang = request.args.get("lang", "ko")   # ?lang=ko | en | ja
    tab  = request.args.get("tab",  "login")
    return render_template("auth.html", lang=lang, tab=tab)

@app.route("/login", methods=["POST"])
def login():
    # JavaScript에서 넘겨준 email, password 받기
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    if not email or not password:
        return redirect(url_for("auth", error="missing_fields"))

    user = None
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 데이터에 따라 is_active 또는 is_verified 컬럼 확인
                # 둘 중 하나가 없다면 WHERE email = %s 로만 테스트해보세요.
                cur.execute(
                    "SELECT * FROM project.users WHERE email = %s AND is_active = TRUE",
                    (email,)
                )
                user = cur.fetchone()
    except Exception as e:
        logger.error(f"DB Error during login: {e}")
        return redirect(url_for("auth", error="db_error"))

    if user:
        # bcrypt 비교 로직 (타입 변환 포함)
        db_hash = user["password_hash"]
        
        # DB에서 가져온 값이 문자열(str)이면 바이트(bytes)로 변환
        if isinstance(db_hash, str):
            db_hash = db_hash.encode('utf-8')
            
        # 입력받은 비밀번호 비교
        if bcrypt.checkpw(password.encode('utf-8'), db_hash):
            # 로그인 성공: 세션 저장
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["user_role"] = user.get("role", "viewer")
            
            logger.info(f"Login Success: {email}")
            return redirect(url_for("index"))
        else:
            logger.warning(f"Login Failed: Password mismatch for {email}")
    else:
        logger.warning(f"Login Failed: User not found or inactive: {email}")

    # 실패 시 리다이렉트
    return redirect(url_for("auth", login_fail="true"))


@app.route("/signup", methods=["POST"])
def signup():
    name     = request.form.get("name",     "").strip()
    company  = request.form.get("company",  "").strip()
    email    = request.form.get("email",    "").strip()
    role     = request.form.get("role",     "viewer")
    password = request.form.get("password", "")

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO project.users (email, password_hash, name, company, role)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (email, hashed, name, company, role)
                )
            conn.commit()
    except Exception as e:
        logger.error("signup 쿼리 실패: %s", e)
        # 에러 발생 시 에러 메시지를 들고 돌아가면 더 좋습니다.
        return redirect(url_for("auth", tab="signup", error="db"))

    # 가입 성공 시 success 파라미터를 추가하여 리다이렉트
    return redirect(url_for("auth", tab="signup", success="true"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth"))


# fetch_products — 제품 목록 (index.html)
def fetch_products():
    sql = """
        SELECT
            p.product_id,
            p.product_name,
            p.brand_name,
            COUNT(DISTINCT v.video_id)                 AS video_count,
            COALESCE(SUM(v.view_count), 0)             AS total_views,
            COALESCE(SUM(vs.total_all), 0)             AS total_comments,
            COALESCE(SUM(vs.total_comment_threads), 0) AS total_threads,
            COALESCE(SUM(vs.total_replies), 0)         AS total_replies,
            MAX(v.published_at)                        AS updated
        FROM project.products p
        LEFT JOIN project.videos v         ON p.product_id = v.product_id
        LEFT JOIN project.video_summary vs ON v.video_id   = vs.video_id
        GROUP BY p.product_id, p.product_name, p.brand_name
        ORDER BY p.product_id
    """
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                rows = cur.fetchall()
    except Exception as e:
        logger.error("fetch_products 실패: %s", e)
        return []

    products = []
    for r in rows:
        total   = int(r["total_comments"] or 0)
        updated = r["updated"].strftime("%Y-%m-%d %H:%M") if r["updated"] else "-"
        pid     = r["product_id"]
        products.append({
            "id":            pid,
            "name":          r["product_name"],
            "brand":         r["brand_name"],
            "emoji":         EMOJI_MAP.get(pid, "📦"),
            "video_count":   int(r["video_count"] or 0),
            "total_views":   fmt_views(r["total_views"]),
            "total_views_n": int(r["total_views"] or 0),
            "comments":      total,
            "threads":       int(r["total_threads"] or 0),
            "replies":       int(r["total_replies"] or 0),
            "updated":       updated,
        })
    return products


def fetch_dashboard(product_id):
    sql_product = """
        SELECT product_id, product_name, brand_name
        FROM project.products
        WHERE product_id = %s
    """

    # ── dashboard_total_score 뷰 JOIN 추가 ──
    sql_videos = """
        SELECT
            v.video_id,
            v.title,
            v.channel_name,
            v.view_count,
            v.like_count,
            v.comment_count,
            v.url,
            v.published_at,
            COALESCE(vs.total_all, 0)             AS total_all,
            COALESCE(vs.total_comment_threads, 0) AS total_threads,
            COALESCE(vs.total_replies, 0)         AS total_replies,
            v.published_at                        AS updated_at,
            COALESCE(ts.total_score,   0)         AS total_score,
            COALESCE(ts.quality_score, 0)         AS quality_score,
            COALESCE(ts.scale_score,   0)         AS scale_score
        FROM project.videos v
        LEFT JOIN project.video_summary vs
               ON v.video_id = vs.video_id
        LEFT JOIN project.dashboard_total_score ts
               ON v.video_id = ts.video_id
        WHERE v.product_id = %s
        ORDER BY v.view_count DESC
    """

    # ── [통합] analysis + crisis_count + intent_count → 쿼리 1개 ──
    sql_analysis_unified = """
        SELECT
            c.video_id,
            COUNT(*)                                                        AS total_analyzed,
            ROUND(AVG(ca.sentiment_score)::numeric, 3)                     AS avg_sentiment,
            COUNT(*) FILTER (WHERE ca.sentiment_label = 'POSITIVE')        AS positive_count,
            COUNT(*) FILTER (WHERE ca.sentiment_label = 'NEGATIVE')        AS negative_count,
            COUNT(*) FILTER (WHERE ca.sentiment_label = 'NEUTRAL')         AS neutral_count,
            COUNT(*) FILTER (WHERE ca.is_brand_mention  = true)            AS brand_mention,
            COUNT(*) FILTER (WHERE ca.is_purchase_intent = true)           AS purchase_intent,
            COUNT(*) FILTER (WHERE ca.purchase_intent_level = 2)           AS intent_high,
            COUNT(*) FILTER (WHERE ca.purchase_intent_level = 1)           AS intent_medium,
            COUNT(*) FILTER (WHERE ca.purchase_intent_level = 0)           AS intent_low,
            COUNT(*) FILTER (WHERE ca.crisis_flag = true)                  AS crisis_count
        FROM project.comments c
        JOIN project.videos    v  ON c.video_id    = v.video_id
        JOIN project.comments_analysis ca ON c.comment_id = ca.comment_id
        WHERE v.product_id = %s
        GROUP BY c.video_id
    """

    sql_crisis = """
        SELECT c.video_id, c.text, c.author_name, c.likes, ca.sentiment_score
        FROM project.comments c
        JOIN project.videos v ON c.video_id = v.video_id
        JOIN project.comments_analysis ca ON c.comment_id = ca.comment_id
        WHERE v.product_id = %s
          AND ca.crisis_flag = true
        ORDER BY c.likes DESC
        LIMIT 9
    """

    sql_intent = """
        SELECT c.video_id, c.text, c.author_name, c.likes, ca.purchase_intent_level
        FROM project.comments c
        JOIN project.videos v ON c.video_id = v.video_id
        JOIN project.comments_analysis ca ON c.comment_id = ca.comment_id
        WHERE v.product_id = %s
          AND ca.is_purchase_intent = true
          AND ca.purchase_intent_level = 2
        ORDER BY c.likes DESC
        LIMIT 9
    """

    sql_keywords = """
        SELECT keyword, COUNT(*) AS cnt
        FROM (
            SELECT regexp_split_to_table(cc.keywords, ',') AS keyword
            FROM project.comments c
            JOIN project.videos v ON c.video_id = v.video_id
            JOIN project.comment_cleansed cc ON c.comment_id = cc.comment_id
            WHERE v.product_id = %s
              AND cc.spam_level != '스팸'
              AND cc.keywords IS NOT NULL
            UNION ALL
            SELECT regexp_split_to_table(prc.keywords, E'\\s+') AS keyword
            FROM project.platform_reviews pr
            JOIN project.platform_review_cleansed prc ON pr.review_id = prc.review_id
            WHERE pr.product_id = %s
              AND prc.spam_level != '스팸'
              AND prc.keywords IS NOT NULL
        ) t
        WHERE keyword != ''
        GROUP BY keyword
        ORDER BY cnt DESC
        LIMIT 5
    """

    sql_voc = """
        WITH base AS (
            SELECT
                c.video_id,
                ca.attribute_tags,
                ca.sentiment_label
            FROM project.comments c
            JOIN project.videos v ON c.video_id = v.video_id
            JOIN project.comments_analysis ca ON c.comment_id = ca.comment_id
            WHERE v.product_id = %s
              AND ca.attribute_tags IS NOT NULL
              AND ca.attribute_tags != ''
        ),
        raw_tags AS (
            SELECT
                video_id,
                TRIM(tag_raw)        AS raw_tag,
                UPPER(sentiment_label) AS sentiment_label
            FROM base
            CROSS JOIN LATERAL regexp_split_to_table(attribute_tags, '[,|]+') AS tag_raw
            WHERE TRIM(tag_raw) != ''
        ),
        cleaned AS (
            SELECT
                video_id,
                sentiment_label,
                CASE
                    WHEN raw_tag LIKE '%%:%%' THEN TRIM(SPLIT_PART(raw_tag, ':', 2))
                    ELSE raw_tag
                END AS name
            FROM raw_tags
        )
        SELECT
            video_id,
            name,
            COUNT(*) FILTER (WHERE sentiment_label = 'POSITIVE') AS pos,
            COUNT(*) FILTER (WHERE sentiment_label = 'NEUTRAL')  AS neu,
            COUNT(*) FILTER (WHERE sentiment_label = 'NEGATIVE') AS neg,
            COUNT(*)                                              AS total
        FROM cleaned
        WHERE name != ''
        GROUP BY video_id, name
        ORDER BY video_id, total DESC
    """

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

                cur.execute(sql_product, (product_id,))
                product = cur.fetchone()
                if not product:
                    return None, None

                cur.execute(sql_videos, (product_id,))
                video_rows = cur.fetchall()
                if not video_rows:
                    return None, None

                cur.execute(sql_analysis_unified, (product_id,))
                analysis_map = {r["video_id"]: r for r in cur.fetchall()}

                cur.execute(sql_crisis, (product_id,))
                crisis_map = {}
                for r in cur.fetchall():
                    crisis_map.setdefault(r["video_id"], []).append(r)

                cur.execute(sql_intent, (product_id,))
                intent_map = {}
                for r in cur.fetchall():
                    intent_map.setdefault(r["video_id"], []).append(r)

                # ⑥ 키워드
                cur.execute(sql_keywords, (product_id, product_id))
                top_keywords = [
                    {"word": r["keyword"], "cnt": int(r["cnt"])}
                    for r in cur.fetchall()
                ]

                cur.execute(sql_voc, (product_id,))
                voc_map: dict[str, list] = {}
                for r in cur.fetchall():
                    voc_map.setdefault(r["video_id"], []).append({
                        "name":  r["name"],
                        "pos":   int(r["pos"]   or 0),
                        "neu":   int(r["neu"]   or 0),
                        "neg":   int(r["neg"]   or 0),
                        "total": int(r["total"] or 0),
                    })

    except Exception as e:
        logger.error("fetch_dashboard(product_id=%s) 실패: %s", product_id, e)
        return None, None

    videos = []
    for i, r in enumerate(video_rows):
        vid  = r["video_id"]
        an   = analysis_map.get(vid, {})
        title = r["title"] or ""

        view_count = int(r["view_count"] or 0)
        like_count = int(r["like_count"] or 0)
        like_rate  = round(like_count / view_count * 100, 2) if view_count else 0

        total_an = int(an.get("total_analyzed") or 0)
        pos      = int(an.get("positive_count")  or 0)
        neg      = int(an.get("negative_count")  or 0)
        neu      = int(an.get("neutral_count")   or 0)
        pos_rate = round(pos / total_an * 100, 1) if total_an else 0
        neg_rate = round(neg / total_an * 100, 1) if total_an else 0
        neu_rate = round(neu / total_an * 100, 1) if total_an else 0

        videos.append({
            "rank":            i + 1,
            "video_id":        vid,
            "channel_name":    r["channel_name"],
            "name":            r["channel_name"],
            "title":           title[:50] + "..." if len(title) > 50 else title,
            "views":           fmt_views(view_count),
            "likes":           fmt_views(like_count),
            "like_rate":       like_rate,
            "comments":        int(r["comment_count"] or 0),
            "total_all":       int(r["total_all"]),
            "threads":         int(r["total_threads"]),
            "replies":         int(r["total_replies"]),
            "url":             r["url"],
            "updated":         r["updated_at"].strftime("%Y-%m-%d %H:%M") if r["updated_at"] else "-",
            "total_analyzed":  total_an,
            "avg_sentiment":   float(an.get("avg_sentiment") or 0),
            "positive":        pos,
            "negative":        neg,
            "neutral":         neu,
            "pos_rate":        pos_rate,
            "neg_rate":        neg_rate,
            "neu_rate":        neu_rate,
            "brand_mention":   int(an.get("brand_mention")   or 0),
            "purchase_intent": int(an.get("purchase_intent") or 0),
            "intent_high":     int(an.get("intent_high")     or 0),
            "intent_medium":   int(an.get("intent_medium")   or 0),
            "intent_low":      int(an.get("intent_low")      or 0),
            "crisis_count":    int(an.get("crisis_count")    or 0),
            # ── 협업 성공 지수 (dashboard_total_score 뷰) ──
            "total_score":     float(r["total_score"]   or 0),
            "quality_score":   float(r["quality_score"] or 0),
            "scale_score":     float(r["scale_score"]   or 0),
            "crisis_samples":  [
                {"text": c["text"], "author": c["author_name"], "likes": c["likes"]}
                for c in crisis_map.get(vid, [])
            ],
            "intent_samples":  [
                {"text": c["text"], "author": c["author_name"], "likes": c["likes"]}
                for c in intent_map.get(vid, [])
            ],
            "voc_attributes":  voc_map.get(vid, []),
        })

    total_views    = sum(int(r["view_count"] or 0) for r in video_rows)
    total_comments = sum(int(r["total_all"])        for r in video_rows)
    total_crisis   = sum(v["crisis_count"]          for v in videos)
    total_purchase = sum(v["purchase_intent"]       for v in videos)
    total_brand    = sum(v["brand_mention"]         for v in videos)

    alert = {"active": False, "message": "", "time": ""}
    if total_crisis > 0:
        crisis_vids = [v["name"] for v in videos if v["crisis_count"] > 0]
        names = ", ".join(f'"{n}"' for n in crisis_vids[:2])
        alert = {
            "active":  True,
            "message": f"{names} — 위기 댓글 {total_crisis}건 감지.",
            "time":    datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    updated = max(
        (r["updated_at"] for r in video_rows if r["updated_at"]),
        default=None,
    )

    product_info = {
        "id":      product["product_id"],
        "name":    product["product_name"],
        "brand":   product["brand_name"],
        "emoji":   EMOJI_MAP.get(product["product_id"], "📦"),
        "updated": updated.strftime("%Y-%m-%d %H:%M") if updated else "-",
    }

    data = {
        "kpis": {
            "views":           fmt_views(total_views),
            "comments":        total_comments,
            "video_count":     len(videos),
            "crisis":          total_crisis,
            "purchase_intent": total_purchase,
            "brand_mention":   total_brand,
        },
        "alert":        alert,
        "videos":       videos,
        "top_keywords": top_keywords,
    }

    return product_info, data


# fetch_realtime — 실시간 스트림 통계 (1분 폴링, GET /api/realtime/<product_id>)
def fetch_realtime(product_id):

    sql_hot_keywords = """
        SELECT keyword, COUNT(*) AS cnt
        FROM (
            SELECT regexp_split_to_table(cc.keywords, ',') AS keyword
            FROM project.comments c
            JOIN project.videos v ON c.video_id = v.video_id
            JOIN project.comment_cleansed cc ON c.comment_id = cc.comment_id
            WHERE v.product_id = %s
              AND cc.spam_level != '스팸'
              AND cc.keywords IS NOT NULL
            UNION ALL
            SELECT regexp_split_to_table(prc.keywords, E'\\s+') AS keyword
            FROM project.platform_reviews pr
            JOIN project.platform_review_cleansed prc ON pr.review_id = prc.review_id
            WHERE pr.product_id = %s
              AND prc.spam_level != '스팸'
              AND prc.keywords IS NOT NULL
        ) t
        WHERE keyword != ''
        GROUP BY keyword
        ORDER BY cnt DESC
        LIMIT 5
    """

    sql_sentiment_1h = """
        SELECT
            COUNT(*)                                                        AS total,
            COUNT(*) FILTER (WHERE ca.sentiment_label = 'POSITIVE')        AS positive,
            COUNT(*) FILTER (WHERE ca.sentiment_label = 'NEGATIVE')        AS negative,
            COUNT(*) FILTER (WHERE ca.sentiment_label = 'NEUTRAL')         AS neutral,
            ROUND(AVG(ca.sentiment_score)::numeric, 3)                     AS avg_score
        FROM project.comments c
        JOIN project.videos v ON c.video_id = v.video_id
        JOIN project.comments_analysis ca ON c.comment_id = ca.comment_id
        WHERE v.product_id = %s
          AND ca.analyzed_at >= NOW() - INTERVAL '1 hour'
    """

    sql_velocity = """
        SELECT
            COUNT(*)                                                                  AS total_1h,
            EXTRACT(EPOCH FROM (MAX(c.published_at) - MIN(c.published_at)))           AS span_sec
        FROM project.comments c
        JOIN project.videos v ON c.video_id = v.video_id
        WHERE v.product_id = %s
          AND c.published_at >= NOW() - INTERVAL '1 hour'
    """

    sql_spam = """
        SELECT
            COUNT(*) FILTER (WHERE cc.spam_level = '스팸') AS spam_total,
            COUNT(*)                                        AS all_total
        FROM project.comments c
        JOIN project.videos v ON c.video_id = v.video_id
        JOIN project.comment_cleansed cc ON c.comment_id = cc.comment_id
        WHERE v.product_id = %s
    """

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

                cur.execute(sql_hot_keywords, (product_id, product_id))
                hot_keywords = [
                    {"word": r["keyword"], "cnt": int(r["cnt"])}
                    for r in cur.fetchall()
                ]

                cur.execute(sql_sentiment_1h, (product_id,))
                s        = cur.fetchone() or {}
                total_s  = int(s.get("total")    or 0)
                pos_1h   = int(s.get("positive") or 0)
                neg_1h   = int(s.get("negative") or 0)
                neu_1h   = int(s.get("neutral")  or 0)
                avg_score = float(s.get("avg_score") or 0)
                sentiment_1h = {
                    "total":     total_s,
                    "positive":  pos_1h,
                    "negative":  neg_1h,
                    "neutral":   neu_1h,
                    "pos_rate":  round(pos_1h / total_s * 100, 1) if total_s else 0,
                    "neg_rate":  round(neg_1h / total_s * 100, 1) if total_s else 0,
                    "neu_rate":  round(neu_1h / total_s * 100, 1) if total_s else 0,
                    "avg_score": avg_score,
                    "temp":      round((avg_score + 1) / 2 * 100, 1),
                }

                cur.execute(sql_velocity, (product_id,))
                v        = cur.fetchone() or {}
                total_1h = int(v.get("total_1h") or 0)
                span_sec = float(v.get("span_sec") or 0)
                velocity = round(total_1h / (span_sec / 60), 1) if span_sec > 0 else 0

                cur.execute(sql_spam, (product_id,))
                sp         = cur.fetchone() or {}
                spam_total = int(sp.get("spam_total") or 0)
                all_total  = int(sp.get("all_total")  or 0)
                spam_rate  = round(spam_total / all_total * 100, 1) if all_total else 0

    except Exception as e:
        logger.error("fetch_realtime(product_id=%s) 실패: %s", product_id, e)
        return None

    return {
        "updated_at":   datetime.now().strftime("%H:%M:%S"),
        "hot_keywords": hot_keywords,
        "sentiment_1h": sentiment_1h,
        "velocity":     {"per_min": velocity, "total_1h": total_1h},
        "spam":         {"total": spam_total, "rate": spam_rate, "all": all_total},
    }


@app.route("/")
@login_required
def index():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    products = fetch_products()
    return render_template("index.html", products=products, now=now)


@app.route("/dashboard/<int:product_id>")
@login_required
def dashboard(product_id):
    product, data = fetch_dashboard(product_id)
    if not product:
        abort(404)
    return render_template("dashboard.html", product=product, data=data)


@app.route("/api/products")
@login_required
def api_products():
    return jsonify(fetch_products())


@app.route("/api/dashboard/<int:product_id>")
@login_required
def api_dashboard(product_id):
    _, data = fetch_dashboard(product_id)
    if not data:
        abort(404)
    return jsonify(data)


@app.route("/api/realtime/<int:product_id>")
@login_required
def api_realtime(product_id):
    data = fetch_realtime(product_id)
    if data is None:
        abort(404)
    return jsonify(data)


def fetch_timebin(video_id: str):
    sql = """
        WITH base AS (
            SELECT
                ca.sentiment_score,
                ca.sentiment_label,
                EXTRACT(EPOCH FROM (c.published_at - v.published_at)) / 3600.0 AS hours_after
            FROM project.comments c
            JOIN project.comments_analysis ca ON c.comment_id = ca.comment_id
            JOIN project.videos v             ON c.video_id   = v.video_id
            WHERE c.video_id = %s
              AND c.published_at IS NOT NULL
              AND v.published_at IS NOT NULL
        ),
        binned AS (
            SELECT
                CASE
                    WHEN hours_after <  24  THEN 0
                    WHEN hours_after <  72  THEN 1
                    WHEN hours_after < 168  THEN 2
                    ELSE                         3
                END AS bin,
                sentiment_score,
                UPPER(sentiment_label) AS sentiment_label
            FROM base
            WHERE hours_after >= 0
        )
        SELECT
            bin,
            COUNT(*)                                                          AS cnt,
            ROUND(AVG(sentiment_score)::numeric, 4)                          AS avg_score,
            ROUND(STDDEV(sentiment_score)::numeric, 4)                       AS std_dev,
            COUNT(*) FILTER (WHERE sentiment_label = 'POSITIVE')             AS pos,
            COUNT(*) FILTER (WHERE sentiment_label = 'NEUTRAL')              AS neu,
            COUNT(*) FILTER (WHERE sentiment_label = 'NEGATIVE')             AS neg
        FROM binned
        GROUP BY bin
        ORDER BY bin
    """
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (video_id,))
                rows = {r["bin"]: r for r in cur.fetchall()}
    except Exception as e:
        logger.error("fetch_timebin(%s) 실패: %s", video_id, e)
        return None

    bins = []
    for i in range(4):
        r = rows.get(i)
        if r:
            cnt = int(r["cnt"])
            pos = int(r["pos"])
            bins.append({
                "bin":       i,
                "count":     cnt,
                "avg_score": float(r["avg_score"]) if r["avg_score"] is not None else None,
                "std_dev":   float(r["std_dev"])   if r["std_dev"]   is not None else None,
                "pos":       pos,
                "neu":       int(r["neu"]),
                "neg":       int(r["neg"]),
                "pos_rate":  round(pos / cnt * 100, 1) if cnt else None,
            })
        else:
            bins.append({"bin": i, "count": 0, "avg_score": None, "std_dev": None,
                         "pos": 0, "neu": 0, "neg": 0, "pos_rate": None})

    return {"video_id": video_id, "bins": bins}




@app.route("/api/comments/<string:video_id>")
@login_required
def api_comments(video_id):
    """전체 댓글 목록 — dashboard.html Total Comments 탭용"""
    limit     = min(int(request.args.get("limit", 50)), 200)
    sentiment = request.args.get("sentiment", "ALL").upper()

    sent_filter = ""
    if sentiment in ("POSITIVE", "NEGATIVE", "NEUTRAL"):
        sent_filter = f"AND UPPER(ca.sentiment_label) = '{sentiment}'"

    sql = f"""
        SELECT
            COALESCE(cc.normalized_text, c.text) AS text,
            c.author_name,
            c.likes,
            UPPER(ca.sentiment_label)  AS sentiment,
            ca.sentiment_score         AS score,
            ca.crisis_flag             AS crisis,
            CASE
                WHEN EXTRACT(EPOCH FROM (c.published_at - v.published_at)) / 3600.0 <  24  THEN 0
                WHEN EXTRACT(EPOCH FROM (c.published_at - v.published_at)) / 3600.0 <  72  THEN 1
                WHEN EXTRACT(EPOCH FROM (c.published_at - v.published_at)) / 3600.0 < 168  THEN 2
                ELSE 3
            END AS bin
        FROM project.comments c
        JOIN project.videos v              ON c.video_id   = v.video_id
        JOIN project.comments_analysis ca  ON c.comment_id = ca.comment_id
        LEFT JOIN project.comment_cleansed cc ON c.comment_id = cc.comment_id
        WHERE c.video_id = %s
          {sent_filter}
        ORDER BY c.likes DESC, ca.sentiment_score DESC
        LIMIT {limit}
    """

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (video_id,))
                rows = cur.fetchall()
    except Exception as e:
        logger.error("api_comments 실패: %s", e)
        return jsonify({"comments": [], "total": 0})

    comments = [
        {
            "text":      r["text"] or "",
            "author":    r["author_name"] or "",
            "likes":     int(r["likes"] or 0),
            "sentiment": r["sentiment"] or "NEUTRAL",
            "score":     float(r["score"] or 0),
            "crisis":    bool(r["crisis"]),
            "bin":       int(r["bin"]) if r["bin"] is not None else None,
        }
        for r in rows
    ]
    return jsonify({"video_id": video_id, "total": len(comments), "comments": comments})

@app.route("/api/timebin/<string:video_id>")
@login_required
def api_timebin(video_id):
    data = fetch_timebin(video_id)
    if not data:
        abort(404)
    return jsonify(data)


@app.route("/api/voc-comments/<string:video_id>")
@login_required
def api_voc_comments(video_id):
    attr      = request.args.get("attr", "").strip()
    sentiment = request.args.get("sentiment", "all").upper()
    limit     = min(int(request.args.get("limit", 30)), 100)

    if not attr:
        return jsonify({"comments": [], "total": 0})

    sent_filter = ""
    if sentiment in ("POSITIVE", "NEGATIVE", "NEUTRAL"):
        sent_filter = f"AND UPPER(ca.sentiment_label) = '{sentiment}'"

    sql = f"""
        SELECT
            COALESCE(cc.normalized_text, c.text) AS text,
            c.author_name,
            c.likes,
            ca.sentiment_label,
            ca.sentiment_score
        FROM project.comments c
        JOIN project.comments_analysis  ca ON c.comment_id = ca.comment_id
        LEFT JOIN project.comment_cleansed cc ON c.comment_id = cc.comment_id
        WHERE c.video_id = %s
          AND ca.attribute_tags IS NOT NULL
          AND (
              ca.attribute_tags ILIKE %s
              OR ca.attribute_tags ILIKE %s
          )
          {sent_filter}
        ORDER BY c.likes DESC, ca.sentiment_score DESC
        LIMIT {limit}
    """
    like_exact  = f"%{attr}%"
    like_colon  = f"%:{attr}%"

    sql_count = f"""
        SELECT COUNT(*) AS cnt
        FROM project.comments c
        JOIN project.comments_analysis ca ON c.comment_id = ca.comment_id
        WHERE c.video_id = %s
          AND ca.attribute_tags IS NOT NULL
          AND (
              ca.attribute_tags ILIKE %s
              OR ca.attribute_tags ILIKE %s
          )
          {sent_filter}
    """

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql,       (video_id, like_exact, like_colon))
                rows = cur.fetchall()
                cur.execute(sql_count, (video_id, like_exact, like_colon))
                total = int(cur.fetchone()["cnt"] or 0)
    except Exception as e:
        logger.error("api_voc_comments 실패: %s", e)
        return jsonify({"comments": [], "total": 0})

    comments = [
        {
            "text":      r["text"],
            "author":    r["author_name"],
            "likes":     int(r["likes"] or 0),
            "sentiment": (r["sentiment_label"] or "").upper(),
            "score":     float(r["sentiment_score"] or 0),
        }
        for r in rows
    ]
    return jsonify({"attr": attr, "sentiment": sentiment, "total": total, "comments": comments})


# api_platform_reviews — 영상 업로드 기준 전/후 time bin + 플랫폼별 평점/감성 (GET /api/platform-reviews/<product_id>)
@app.route("/api/platform-reviews/<int:product_id>")
@login_required
def api_platform_reviews(product_id):
    # 해당 product의 영상 업로드일 중 가장 이른 날짜를 기준일로 사용
    sql_upload_date = """
        SELECT MIN(v.published_at) AS upload_date
        FROM project.videos v
        WHERE v.product_id = %s
    """

    sql_reviews = """
        SELECT
            pr.platform,
            pr.rating,
            pr.review_date,
            pra.sentiment_label,
            pra.sentiment_score,
            pra.crisis_flag,
            -- 영상 업로드일 기준 time bin
            CASE
                WHEN pr.review_date < %(upload_date)s
                    THEN -1   -- 영상 업로드 전
                WHEN pr.review_date < %(upload_date)s + INTERVAL '1 day'
                    THEN 0    -- 24시간 이내
                WHEN pr.review_date < %(upload_date)s + INTERVAL '3 days'
                    THEN 1    -- 1~3일
                WHEN pr.review_date < %(upload_date)s + INTERVAL '7 days'
                    THEN 2    -- 3~7일
                ELSE 3        -- 7일 이후
            END AS time_bin
        FROM project.platform_reviews pr
        JOIN project.platform_reviews_analysis pra ON pr.review_id = pra.review_id
        JOIN project.platform_review_cleansed prc  ON pr.review_id = prc.review_id
        WHERE pr.product_id = %(product_id)s
          AND (prc.spam_level IS NULL OR prc.spam_level != '스팸')
          AND pr.review_date IS NOT NULL
        ORDER BY pr.review_date
    """

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 영상 업로드 기준일 조회
                cur.execute(sql_upload_date, (product_id,))
                row = cur.fetchone()
                upload_date = row["upload_date"] if row and row["upload_date"] else None

                if not upload_date:
                    return jsonify({"bins": [], "platforms": [], "summary": {}})

                # 리뷰 데이터 조회
                cur.execute(sql_reviews, {
                    "product_id": product_id,
                    "upload_date": upload_date
                })
                rows = cur.fetchall()

    except Exception as e:
        logger.error("api_platform_reviews 실패: %s", e)
        return jsonify({"bins": [], "platforms": [], "summary": {}, "error": str(e)})

    if not rows:
        return jsonify({"bins": [], "platforms": [], "upload_date": None, "summary": {}})

    # ── 집계 ──
    BIN_LABELS = {-1: "영상 전", 0: "24h 이내", 1: "1~3일", 2: "3~7일", 3: "7일~"}
    platforms  = sorted(set(r["platform"] for r in rows))

    # bin × platform 집계
    from collections import defaultdict
    agg = defaultdict(lambda: {"count": 0, "rating_sum": 0.0, "pos": 0, "neg": 0, "neu": 0})

    for r in rows:
        b  = r["time_bin"]
        pl = r["platform"]
        key = (b, pl)
        agg[key]["count"]      += 1
        agg[key]["rating_sum"] += float(r["rating"] or 0)
        sl = (r["sentiment_label"] or "").upper()
        if sl == "POSITIVE":   agg[key]["pos"] += 1
        elif sl == "NEGATIVE": agg[key]["neg"] += 1
        else:                  agg[key]["neu"] += 1

    # bin 목록 (-1 포함, 정렬)
    all_bins = sorted(set(b for (b, _) in agg.keys()))

    bins_out = []
    for b in all_bins:
        entry = {
            "bin":   b,
            "label": BIN_LABELS.get(b, str(b)),
            "platforms": {}
        }
        for pl in platforms:
            key = (b, pl)
            if key in agg:
                d = agg[key]
                entry["platforms"][pl] = {
                    "count":      d["count"],
                    "avg_rating": round(d["rating_sum"] / d["count"], 2) if d["count"] else None,
                    "pos":        d["pos"],
                    "neg":        d["neg"],
                    "neu":        d["neu"],
                    "pos_rate":   round(d["pos"] / d["count"] * 100, 1) if d["count"] else 0,
                }
            else:
                entry["platforms"][pl] = None
        bins_out.append(entry)

    # ── 전/후 평균 평점 요약 ──
    def avg_rating_for(bin_filter):
        total, cnt = 0.0, 0
        for r in rows:
            if bin_filter(r["time_bin"]) and r["rating"]:
                total += float(r["rating"])
                cnt   += 1
        return round(total / cnt, 2) if cnt else None

    rating_before = avg_rating_for(lambda b: b == -1)
    rating_after  = avg_rating_for(lambda b: b >= 0)
    diff = round(rating_after - rating_before, 2) if (rating_before and rating_after) else None

    summary = {
        "total":          len(rows),
        "upload_date":    upload_date.strftime("%Y-%m-%d") if upload_date else None,
        "rating_before":  rating_before,
        "rating_after":   rating_after,
        "rating_diff":    diff,
        "platforms":      platforms,
    }

    return jsonify({
        "upload_date": upload_date.strftime("%Y-%m-%d %H:%M") if upload_date else None,
        "platforms":   platforms,
        "bins":        bins_out,
        "summary":     summary,
    })


# api_audience_segment — audience_segment 집계 (GET /api/audience-segment/<video_id>)
@app.route("/api/audience-segment/<string:video_id>")
@login_required
def api_audience_segment(video_id):
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                        COALESCE(ca.audience_segment, 'None') AS segment,
                        COUNT(*) AS count
                    FROM project.comments_analysis ca
                    JOIN project.comments c ON ca.comment_id = c.comment_id
                    WHERE c.video_id = %s
                    GROUP BY COALESCE(ca.audience_segment, 'None')
                """, (video_id,))
                rows = cur.fetchall()

        segments = {"Loyal": 0, "Newbie": 0, "None": 0}
        for row in rows:
            seg = row["segment"]
            if seg in segments:
                segments[seg] = int(row["count"])
            else:
                segments["None"] += int(row["count"])

        total = sum(segments.values())
        return jsonify({"segments": segments, "total": total})

    except Exception as e:
        logger.error("api_audience_segment 실패: %s", e)
        return jsonify({"error": str(e)}), 500

# api_chat — AI 챗봇 RAG (POST /api/chat)
from rag_function import generate_response

@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    try:
        body = request.get_json()
        question = body.get("question", "").strip()

        if not question:
            return jsonify({"error": "질문이 없습니다."}), 400

        answer = generate_response(question)
        return jsonify({"status": "success", "answer": answer})

    except Exception as e:
        logger.error("api_chat 실패: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    # 로컬 테스트용
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)