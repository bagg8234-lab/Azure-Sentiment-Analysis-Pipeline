import os
import datetime
import psycopg2
from dotenv import load_dotenv
from curl_cffi import requests
import time

# 1. DB 설정 로드
load_dotenv()

def save_to_db(reviews, product_id):
    if not reviews: return
    conn = None
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST"),
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            port=os.getenv("DB_PORT", "5432")
        )
        cur = conn.cursor()
        insert_query = """
        INSERT INTO project.platform_reviews 
        (review_id, platform, product_id, rating, review_text, reviewer_name, purchase_yn, helpful_count, review_date, collected_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (review_id) DO NOTHING;
        """
        for r in reviews:
            cur.execute(insert_query, (
                f"oy_{r.get('reviewId')}", 
                "oliveyoung", 
                product_id, 
                float(r.get('reviewScore', 0)), 
                r.get('content', '').strip(), 
                r.get('mbrNm', '익명'), 
                True, 
                r.get('recommendCnt', 0), 
                r.get('sysRegDtm', datetime.datetime.now()),
                datetime.datetime.now()
            ))
        conn.commit()
        print(f"✅ DB에 {len(reviews)}건 저장 완료!")
        cur.close()
    except Exception as e:
        print(f"❌ DB 저장 에러: {e}")
    finally:
        if conn: conn.close()

def get_oliveyoung_reviews_success_version(goods_no, internal_id):
    # 1. 세션 생성
    # curl_cffi로 Chrome 브라우저 핑거프린트 우회 — requests 라이브러리로는 403 발생
    session = requests.Session(impersonate="chrome110")
    
    # 2. 보안 쿠키 발급 접속
    # 이 단계 없이 API 직접 호출하면 인증 실패 — 반드시 메인 페이지 먼저 접속 필요
    print(f"[{goods_no}] 접속 중...")
    main_url = f"https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo={goods_no}"
    session.get(main_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    time.sleep(2)

    # 3. API 호출 (20건)
    # 페이지네이션 확장 시 page/size 파라미터 조정 필요
    api_url = "https://m.oliveyoung.co.kr/review/api/v2/reviews"
    payload = {
        "goodsNumber": goods_no,
        "page": 0,
        "size": 20,
        "sortType": "USEFUL_SCORE_DESC",
        "reviewType": "ALL"
    }
    api_headers = {
        "Origin": "https://www.oliveyoung.co.kr",
        "Referer": "https://www.oliveyoung.co.kr/",
        "Content-Type": "application/json",
    }

    try:
        response = session.post(api_url, json=payload, headers=api_headers)
        if response.status_code == 200:
            data = response.json()
            # API 버전에 따라 응답 키가 'contents' 또는 'data'로 다름 — 둘 다 대비
            reviews = data.get('contents', data.get('data', []))

            if reviews:
                print(f"{len(reviews)}개의 리뷰를 가져왔습니다.")
                save_to_db(reviews, internal_id)
            return reviews
        else:
            print(f"❌ API 실패: {response.status_code}")
            return []
    except Exception as e:
        print(f"⚠️ 에러: {e}")
        return []

if __name__ == "__main__":
    GOODS_NO = "A000000231020"
    MY_PRODUCT_ID = 5 # products 테이블 FK — 변경 시 확인 필수
    get_oliveyoung_reviews_success_version(GOODS_NO, MY_PRODUCT_ID)