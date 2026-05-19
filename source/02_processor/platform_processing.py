import os
import re
import html
import datetime
import logging
import psycopg2
from dotenv import load_dotenv
from collections import Counter
from psycopg2.extras import execute_batch

from kiwipiepy import Kiwi
from langdetect import detect, LangDetectException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

load_dotenv(encoding="utf-8")

kiwi = Kiwi()


# 설정

SPAM_PATTERNS = [
    r"https?://\S+",
    r"www\.\S+",
    r"^\s*[ㄱ-ㅎㅏ-ㅣ]+\s*$",
]

STOPWORDS = {
    "이", "그", "저", "것", "수", "등", "및", "더", "제", "좀",
    # 플랫폼 단어
    "리뷰", "평점", "별점", "선택", "신고",
    # 의미 약한 단어
    "제품", "사용", "정도", "느낌", "기준", "보통", "타입",
    # 형태소 노이즈
    "이것", "이것저것",
    # 브랜드 분해 노이즈
    "이사"
}

BRAND_KEYWORDS = {
    "투슬래시포": ["투슬래시포", "2/4", "투포", "투슬포", "이사배"],
}


# 텍스트 전처리
def clean_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

# 정규화: 반복 문자 축소, 공백 정리
def normalize(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"(.)\1{2,}", r"\1\1", text)  
    text = re.sub(r"\s+", " ", text).strip()
    return text

# 스팸 점수 계산: URL, 반복 문자 등 패턴 기반 점수 + 레이블
def calculate_spam_score(text: str) -> dict:
    score = 0
    for pattern in SPAM_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            score += 50
    if re.fullmatch(r"(.)\1{4,}", text.strip()):
        score += 50

    label = "정상"
    if score >= 70:
        label = "스팸"
    elif score >= 40:
        label = "의심"

    return {"score": min(score, 100), "label": label}

# 언어 감지
def detect_language(text: str) -> str:
    if not text:
        return "unknown"

    if re.search(r"[가-힣]", text):
        return "ko"

    try:
        return detect(text)
    except LangDetectException:
        return "unknown"

# 브랜드 언급 추출
def extract_mentioned_brands(text: str) -> str | None:
    found = [
        brand for brand, keywords in BRAND_KEYWORDS.items()
        if any(kw in text for kw in keywords)
    ]
    return ",".join(found) if found else None

# 형태소 분석: 명사 추출 + 키워드 선정
def morpheme_analysis(text: str):
    if not text:
        logging.error("Kiwi 분석 실패: 텍스트가 비어 있습니다.")
        return None, None, None
    try:
        result = kiwi.analyze(text)[0][0]
        morphs = [token.form for token in result]
        tokens = " ".join(morphs)
        nouns = [
            token.form for token in result
            if token.tag in ("NNG", "NNP") and len(token.form) > 1
        ]
        filtered = [n for n in nouns if n not in STOPWORDS]
        counter = Counter(filtered)
        keywords = " ".join([w for w, _ in counter.most_common(5)])
        return tokens, " ".join(nouns), keywords
    except Exception as e:
        logging.error(f"Kiwi 분석 실패: {e}")
        return None, None, None

# 리뷰 하나 전처리: HTML 제거 → 정규화 → 스팸 점수 → 언어 감지 → 브랜드 추출 → 형태소 분석
def preprocess_review(review_id: str, text: str) -> dict:
    cleaned = clean_html(text)
    normalized = normalize(cleaned)
    spam = calculate_spam_score(cleaned)
    language = detect_language(normalized)
    is_empty = not bool(cleaned.strip())
    mentioned_brands = extract_mentioned_brands(normalized)

    if spam["label"] != "스팸" and not is_empty and language == "ko":
        tokens, nouns, keywords = morpheme_analysis(normalized)
    else:
        tokens, nouns, keywords = None, None, None

    return {
        "review_id":        review_id,
        "original_text":    text,
        "cleaned_text":     cleaned,
        "normalized_text":  normalized,
        "tokens":           tokens,
        "nouns":            nouns,
        "keywords":         keywords,
        "spam_score":       spam["score"],
        "spam_level":       spam["label"],
        "is_empty":         is_empty,
        "language":         language,
        "mentioned_brands": mentioned_brands,
        "cleansed_at":      datetime.datetime.now(),
    }

# DB 연결
def get_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        port=os.getenv("DB_PORT", 5432)
    )

# DB에서 전처리 대상 리뷰 로드
def fetch_unprocessed_reviews(conn, only_new: bool = True) -> list:
    """
    only_new=True  : 아직 전처리 안 된 것만 (기본값, 일반 실행 시)
    only_new=False : 전체 재처리 (KEYWORD_DICT 등 바꿨을 때)
    """
    if only_new:
        query = """
            SELECT pr.review_id, pr.review_text
            FROM project.platform_reviews pr
            LEFT JOIN project.platform_review_cleansed prc
                ON pr.review_id = prc.review_id
            WHERE prc.review_id IS NULL
        """
    else:
        query = """
            SELECT review_id, review_text
            FROM project.platform_reviews
        """
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
    logging.info(f"📥 전처리 대상: {len(rows)}건 ({'신규' if only_new else '전체 재처리'})")
    return rows


def upsert_cleansing(conn, results: list, only_new: bool = True):
    if only_new:
        # 신규만 처리할 땐 중복 무시
        conflict_clause = "ON CONFLICT (review_id) DO NOTHING;"
    else:
        # 전체 재처리할 땐 덮어쓰기
        conflict_clause = """
        ON CONFLICT (review_id) DO UPDATE SET
            cleaned_text     = EXCLUDED.cleaned_text,
            normalized_text  = EXCLUDED.normalized_text,
            tokens           = EXCLUDED.tokens,
            nouns            = EXCLUDED.nouns,
            keywords         = EXCLUDED.keywords,
            spam_score       = EXCLUDED.spam_score,
            spam_level       = EXCLUDED.spam_level,
            is_empty         = EXCLUDED.is_empty,
            language         = EXCLUDED.language,
            mentioned_brands = EXCLUDED.mentioned_brands,
            cleansed_at      = EXCLUDED.cleansed_at;
        """
    query = f"""
        INSERT INTO project.platform_review_cleansed
            (review_id, original_text, cleaned_text, normalized_text,
             tokens, nouns, keywords, spam_score, spam_level,
             is_empty, language, mentioned_brands, cleansed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        {conflict_clause}
    """
    with conn.cursor() as cur:
        execute_batch(cur, query, results, page_size=500)
    conn.commit()

# 전체 실행: 리뷰 로드 → 전처리 → platform_review_cleansed 저장 → 통계 로그
def run_preprocessing(only_new: bool = True):
    conn = None
    try:
        conn = get_connection()
        raw_reviews = fetch_unprocessed_reviews(conn, only_new=only_new)

        if not raw_reviews:
            logging.info("전처리 대상 없음")
            return

        results = []
        noun_counter = Counter()

        for review_id, review_text in raw_reviews:
            r = preprocess_review(review_id, review_text)
            results.append((
                r["review_id"],
                r["original_text"],
                r["cleaned_text"],
                r["normalized_text"],
                r["tokens"],
                r["nouns"],
                r["keywords"],
                r["spam_score"],
                r["spam_level"],
                r["is_empty"],
                r["language"],
                r["mentioned_brands"],
                r["cleansed_at"],
            ))

            if (
            r.get("keywords")
            and r["spam_level"] != "스팸"
            and r["language"] == "ko"
            ):
                noun_counter.update(r["keywords"].split())

        upsert_cleansing(conn, results, only_new=only_new)

        top_10 = noun_counter.most_common(10)

        spam_cnt  = sum(1 for r in results if r[8] == "스팸")
        empty_cnt = sum(1 for r in results if r[9])

        logging.info(
            f"총 {len(results)}건 처리 | 스팸 {spam_cnt}건 | 빈값 {empty_cnt}건 | "
            f"정상 {len(results) - spam_cnt - empty_cnt}건 | "
            f"Top10 키워드: {', '.join([f'{w}({c})' for w,c in top_10])}"
        )

    except Exception as e:
        if conn:
            conn.rollback()
        logging.error(f"에러 발생: {e}")
        raise

    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    # 평소 실행: 신규만 처리
    run_preprocessing(only_new=False)

    # KEYWORD_DICT 등 수정 후 전체 재처리할 때:
    # run_preprocessing(only_new=False)
