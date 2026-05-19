# ═══════════════════════════════════════════════════════════════════
# 댓글 전처리 스크립트
# 기능: project.comments → project.comment_cleansed
# 전처리 항목:
#   1. HTML 태그·엔티티 제거 / 공백 정리      → cleaned_text
#   2. 맞춤법 정규화                        → normalized_text
#   3. 형태소 분석 (Kiwi)                   → tokens / nouns / keywords
#   4. 스팸 패턴 감지                         → spam_score / spam_level
#   5. 전처리 후 빈 텍스트 감지               → is_empty
#   6. 언어 감지                              → language
#   7. 브랜드 언급 추출                       → mentioned_brands
#
# ═══════════════════════════════════════════════════════════════════

import re
import html
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from datetime import datetime, timezone
from collections import Counter
import os
import logging
import time

from kiwipiepy import Kiwi
from langdetect import detect, LangDetectException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

kiwi = Kiwi()


# 스팸 카테고리 정의
SPAM_CATEGORIES = {
    "전화/연락처": {
        "patterns": [
            r"\d{2,3}[-\s]?\d{3,4}[-\s]?\d{4}",
            r"(카카오톡|카톡)\s*(으로)?\s*문의(주세요|부탁|바랍니다)?"
        ],
        "per_match": 50,
        "max_score": 80,
    },
    "URL": {
        "patterns": [
            r"https?://\S+",
            r"bit\.ly|tinyurl|goo\.gl|t\.co",
            r"링크\s*클릭|바로가기",
        ],
        "per_match": 40,
        "max_score": 70,
    },
    "광고/협찬": {
        "patterns": [
            r"광고\s*(문의|대행|홍보|제안)",
            r"협찬\s*(문의|제안|가능)",
            r"부업|재택\s*알바|월\s*\d+만",
        ],
        "per_match": 40,
        "max_score": 70,
    },
    "이벤트/유도": {
        "patterns": [
            r"구독.*이벤트|이벤트.*구독",
            r"따라오세요|클릭하세요",
            r"인스타\s*팔로우|유튜브\s*구독",
            r"리뷰\s*이벤트|댓글\s*이벤트|공유\s*이벤트",
        ],
        "per_match": 30,
        "max_score": 70,
    },
    "판매/할인": {
        "patterns": [
            r"공구|공동구매",
            r"할인\s*코드|쿠폰\s*코드",
            r"선착순\s*마감|한정\s*수량",
            r"무료\s*체험|무료\s*이벤트",
        ],
        "per_match": 15,
        "max_score": 35,
    },
    "피싱/사칭": {
        "patterns": [
            r"당첨\s*(되셨|축하|선정)",
            r"아이폰|갤럭시|에어팟|기프티콘\s*(증정|당첨|선물)",
            r"등록\s*(하세요|해주세요|하면)|여기서\s*등록",
            r"채널\s*(구독자|방문자)\s*(이벤트|추첨|선정)",
            r"축하합니다.{0,20}당첨|당첨.{0,20}축하",
            r"무료\s*(증정|제공|획득|받기)",
        ],
        "per_match": 45,
        "max_score": 70,
    },
}

QUESTION_WHITELIST = [
    r"공구\s*(언제|어디서|어떻게|알려|궁금|있나요|하나요|되나요)",
    r"할인\s*코드\s*(있나요|어디|알려|궁금|어떻게)",
    r"쿠폰\s*(있나요|어디|알려|궁금)",
    r"공동구매\s*(언제|어디|알려|궁금|있나요)",
]

FLAGS = re.IGNORECASE


def is_question_whitelist(text: str) -> bool:
    return any(re.search(p, text, FLAGS) for p in QUESTION_WHITELIST)

# 스팸 카테고리별 점수화
def calculate_spam_score(text: str) -> dict:
    total = 0
    matched_categories = []
    length = len(text)
    is_question = is_question_whitelist(text)

    for category, cfg in SPAM_CATEGORIES.items():
        cat_score = 0
        matched_info = []

        for pattern in cfg["patterns"]:
            if is_question and category == "판매/할인":
                continue
            matches = re.findall(pattern, text, FLAGS)
            if matches:
                score_delta = len(matches) * cfg["per_match"]
                cat_score += score_delta
                matched_info.append({
                    "pattern": pattern,
                    "count": len(matches),
                    "score": score_delta,
                })

        capped = min(cat_score, cfg["max_score"])
        if capped > 0:
            total += capped
            matched_categories.append({
                "category": category,
                "score": capped,
                "detail": matched_info,
            })

    final_score = min(total, 100)

    if len(matched_categories) >= 2:
        final_score = min(int(final_score * 1.3), 100)

    if final_score >= 40 and length < 30:
        final_score = min(int(final_score * 1.1), 100)

    return {
        "score": final_score,
        "label": _label(final_score),
        "length": length,
        "is_question": is_question,
        "matched": matched_categories,
    }


def _label(score: int) -> str:
    if score >= 70:
        return "스팸"
    elif score >= 40:
        return "의심"
    else:
        return "정상"


# 고정 불용어
STOPWORDS = {
    "이","그","저","것","수","등","및","더","제","좀",
    "잘","걸","거","듯","때","말","분","게","건",

    "진짜","정말","너무","되게","완전","아직","이제",

    "쿠션","제품","영상","댓글","언니","이번","피부",
    "베이스","화장","사용","사람","평소","생각","느낌","요즘",

    "고민","기대","투슬래시포","투포","투슬포","이사배","메이크업","엔젤",
    "유튜브", "채널",
    "로우","유목","타이밍","플래시","광고","동시","대학","테스터",

    "요새","처리","기회","발견","시간","안녕","시작","방법",
    "추천","구매","최고","감사","인생","소개","표현","중요","상태",

    "태닝","퍼스널","줄기","판매","중학","용돈","대신","데일리","패드",
    "이것","저것","이것저것","결국","처음","지금","오늘",
    "이번","마지막","조금","부분","정도","경우", "확인", "강조", "눈물",
    "순간", "포함", "상황", "레이", "조아", "아묻따", "연습", "이사베",
    "투슬래쉬포", "연습", "탈출", "특유", "모델", "출연", "물론", "확신",
    "아이템", "세계", "고객", "보완", "행사", "정리", "트렌드", "선택",
    "조명", "부자"
}

TOP_N_KEYWORDS = 8

# 브랜드 키워드 사전
BRAND_KEYWORDS = {
    "투슬래시포": ["투슬래시포", "2/4", "투포", "투슬포", "이사배"],
}


# 1. HTML 태그·엔티티 제거 + 공백 정리 → cleaned_text
def clean_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# 2. 반복 문자 정규화 → normalized_text
def normalize(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"(.)\1{2,}", r"\1\1", text)
    text = re.sub(
        r"[^\w\s\.\,\!\?\~\'\"\(\)\[\]\U0001F000-\U0001FFFF\U00002600-\U000027BF]",
        " ",
        text
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text


# 3. 형태소 분석 → tokens / nouns / keywords
def morpheme_analysis(text: str) -> tuple[str, str, str]:
    if not text:
        return "", "", ""

    try:
        result = kiwi.analyze(text)[0][0]
        morphs = [token.form for token in result]
        tokens = " ".join(morphs)

        # 명사 전체 (1글자 제거, STOPWORDS 미적용)
        noun_list = [
            token.form for token in result
            if token.tag in ("NNG", "NNP")
            and len(token.form) >= 2
        ]
        nouns = " ".join(noun_list)

        # keywords: STOPWORDS만 적용
        filtered_nouns = [n for n in noun_list if n not in STOPWORDS]
        counter  = Counter(filtered_nouns)
        keywords = ",".join([word for word, _ in counter.most_common(TOP_N_KEYWORDS)])

    except Exception as e:
        logger.warning(f"형태소 분석 실패: {text[:50]}: {e}")
        tokens, nouns, keywords = text, "", ""

    return tokens, nouns, keywords


# 4. 언어 감지 → language
def detect_language(text: str) -> str:
    if not text:
        return "unknown"
    if re.search(r"[가-힣]", text):
        return "ko"
    if not re.search(r"[a-zA-Z]", text):
        return "unknown"
    try:
        return detect(text)
    except LangDetectException:
        return "unknown"


# 5. 브랜드 언급 추출 → mentioned_brands
def extract_brands(noun_list: list[str]) -> str | None:
    found = []
    for brand, keywords in BRAND_KEYWORDS.items():
        for kw in keywords:
            if kw in noun_list:
                found.append(brand)
                break
    return ",".join(found) if found else None


# 통합 전처리 함수 
def preprocess(comment_id: str, original_text: str) -> dict:

    def empty_result():
        return {
            "comment_id"      : comment_id,
            "original_text"   : original_text or "",
            "cleaned_text"    : "",
            "normalized_text" : "",
            "tokens"          : None,
            "nouns"           : None,
            "keywords"        : None,
            "spam_score"      : 0,
            "spam_level"      : "정상",
            "is_empty"        : True,
            "language"        : "unknown",
            "mentioned_brands": None,
            "cleansed_at"     : datetime.now(timezone.utc),
        }

    if not original_text or len(original_text.strip()) == 0:
        return empty_result()

    # 1단계: HTML 정제
    cleaned = clean_html(original_text)
    if not cleaned:
        return empty_result()

    # 2단계: 정규화
    normalized = normalize(cleaned)

    # 3단계: 스팸 판별
    spam_result = calculate_spam_score(cleaned)
    spam_score  = spam_result["score"]
    spam_level  = spam_result["label"]

    empty_flag = len(normalized.strip()) == 0
    language   = detect_language(normalized) if not empty_flag else "unknown"

    # 4단계: 형태소 분석 (고정 STOPWORDS만 적용)
    if spam_level != "스팸" and not empty_flag:
        tokens, nouns, keywords = morpheme_analysis(normalized)
    else:
        tokens, nouns, keywords = "", "", ""

    # 5단계: 브랜드 추출
    noun_list = nouns.split() if nouns else []
    if spam_level != "스팸" and noun_list:
        mentioned_brands = extract_brands(noun_list)
    else:
        mentioned_brands = None

    return {
        "comment_id"      : comment_id,
        "original_text"   : original_text,
        "cleaned_text"    : cleaned,
        "normalized_text" : normalized,
        "tokens"          : tokens           or None,
        "nouns"           : nouns            or None,
        "keywords"        : keywords         or None,
        "spam_score"      : spam_score,
        "spam_level"      : spam_level,
        "is_empty"        : empty_flag,
        "language"        : language,
        "mentioned_brands": mentioned_brands,
        "cleansed_at"     : datetime.now(timezone.utc),
    }


# 메인: 댓글 읽기 → 전처리 → comment_cleansed 테이블에 저장
def run_processing(only_new: bool = True):
    """
    only_new=True  : 미처리 댓글만 처리 (기본값, 일반 실행 시)
    only_new=False : 전체 재처리 (STOPWORDS·스팸 패턴 수정 후)
    """
    conn = None
    cur  = None
 
    try:
        conn = psycopg2.connect(
            host    =os.getenv("DB_HOST",     "localhost"),
            database=os.getenv("DB_NAME",     "postgres"),
            user    =os.getenv("DB_USER",     "postgres"),
            password=os.getenv("DB_PASSWORD", ""),
        )
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
 
        if only_new:
            cur.execute("""
                SELECT c.comment_id, c.text
                FROM project.comments c
                LEFT JOIN project.comment_cleansed cc
                    ON c.comment_id = cc.comment_id
                WHERE cc.comment_id IS NULL
            """)
        else:
            cur.execute("SELECT comment_id, text FROM project.comments")
 
        rows = cur.fetchall()
        logger.info(f"원본 댓글 로드: {len(rows)}건 ({'신규' if only_new else '전체 재처리'})")
 
        results    = []
        start_time = time.time()
 
        for i, row in enumerate(rows, 1):
            result = preprocess(row["comment_id"], row["text"] or "")
            results.append(result)
            if i % 500 == 0:
                elapsed   = time.time() - start_time
                rate      = i / elapsed
                remaining = (len(rows) - i) / rate if rate > 0 else 0
                logger.info(f"처리 중: {i}/{len(rows)} ({remaining:.1f}초 남음)")
 
        total       = len(results)
        spam_cnt    = sum(1 for r in results if r["spam_level"] == "스팸")
        suspect_cnt = sum(1 for r in results if r["spam_level"] == "의심")
        empty_cnt   = sum(1 for r in results if r["is_empty"])
        ko_cnt      = sum(1 for r in results if r["language"] == "ko")
        brand_cnt   = sum(1 for r in results if r["mentioned_brands"])
 
        global_keyword_counter = Counter()
        for r in results:
            if r["keywords"] and r["spam_level"] != "스팸" and r["language"] == "ko":
                global_keyword_counter.update(r["keywords"].split(","))
        top_10 = [word for word, _ in global_keyword_counter.most_common(10)]
 
        logger.info(
            f"전처리 완료 | 전체: {total} | "
            f"스팸: {spam_cnt} | 의심: {suspect_cnt} | 빈텍스트: {empty_cnt} | "
            f"한국어: {ko_cnt} | 브랜드 언급: {brand_cnt} | "
            f"Top 10 키워드: {', '.join(top_10)}"
        )
 
        if only_new:
            conflict_clause = "ON CONFLICT (comment_id) DO NOTHING;"
        else:
            conflict_clause = """
            ON CONFLICT (comment_id) DO UPDATE SET
                original_text    = EXCLUDED.original_text,
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
 
        insert_sql = f"""
            INSERT INTO project.comment_cleansed (
                comment_id, original_text, cleaned_text, normalized_text,
                tokens, nouns, keywords,
                spam_score, spam_level, is_empty, language, mentioned_brands, cleansed_at
            ) VALUES (
                %(comment_id)s, %(original_text)s, %(cleaned_text)s, %(normalized_text)s,
                %(tokens)s, %(nouns)s, %(keywords)s,
                %(spam_score)s, %(spam_level)s, %(is_empty)s, %(language)s,
                %(mentioned_brands)s, %(cleansed_at)s
            )
            {conflict_clause}
        """
        psycopg2.extras.execute_batch(cur, insert_sql, results, page_size=500)
        conn.commit()
        logger.info(f"comment_cleansed 저장 완료: {len(results)}건")
 
    except Exception as e:
        logger.error(f"전처리 실패: {e}")
        if conn:
            conn.rollback()
        raise
 
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
 
 
if __name__ == "__main__":
    # 평소 실행: 신규만 처리
    run_processing(only_new=True)
 
    # STOPWORDS·스팸 패턴 수정 후 전체 재처리:
    # run_processing(only_new=False)