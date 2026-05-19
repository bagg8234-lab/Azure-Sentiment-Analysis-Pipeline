import os
import datetime
import psycopg2
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import time
import hashlib

load_dotenv()


def save_to_db(reviews):
    if not reviews:
        return
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
        # ON CONFLICT DO NOTHING — 동일 review_id 중복 수집 방어
        insert_query = """
        INSERT INTO project.platform_reviews 
        (review_id, platform, product_id, rating, review_text, reviewer_name, purchase_yn, helpful_count, review_date, collected_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (review_id) DO NOTHING;
        """
        for r in reviews:
            cur.execute(insert_query, (
                r['review_id'], r['platform'], r['product_id'], r['rating'],
                r['review_text'], r['reviewer_name'], r['purchase_yn'],
                r['helpful_count'], r['review_date'], r['collected_at']
            ))
        conn.commit()
        print(f"\n✅ [DB 저장 완료] 총 {len(reviews)}건의 데이터가 처리되었습니다.")
        cur.close()
    except Exception as e:
        print(f"\n❌ [DB 저장 에러] {e}")
    finally:
        if conn:
            conn.close()


chrome_options = Options()
# headless 모드 비활성화 — 네이버는 headless 감지 후 리뷰 탭 렌더링을 차단함
# chrome_options.add_argument("--headless")
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
wait = WebDriverWait(driver, 10)

url = "https://brand.naver.com/twoslashfour/products/12422855463"
PRODUCT_INTERNAL_ID = 5  # products 테이블 FK — 변경 시 확인 필수

try:
    driver.get(url)

    # JS로 클릭 처리 — 일반 .click()은 네이버 SPA 구조에서 탭 전환이 안 되는 경우 있음
    review_tab = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'a[data-name="REVIEW"]')))
    driver.execute_script("arguments[0].click();", review_tab)
    print("--- [리뷰 탭 진입] ---")
    time.sleep(3)

    review_area = driver.find_element(By.ID, "REVIEW")
    reviews = review_area.find_elements(By.TAG_NAME, "li")
    print(f"--- [데이터 추출 시작: {len(reviews)}건] ---\n")

    parsed_reviews = []

    for i, review in enumerate(reviews, 1):
        try:
            # 텍스트 길이 5자 이상인 div만 본문으로 판단 — 별점/날짜 등 짧은 div 걸러내기용
            divs = review.find_elements(By.TAG_NAME, "div")
            content = ""
            for d in divs:
                if len(d.text) > 5:
                    content = d.text
                    break

            rating_element = review.find_element(By.TAG_NAME, "em")
            rating = rating_element.text if rating_element else "0"

            if content:
                print(f"[{i}] 별점: {rating}")
                print(f"내용: {content[:50]}...")

                # content 기반 SHA-256 해시 ID 
                r_id = "naver_" + hashlib.sha256(content.encode()).hexdigest()

                parsed_reviews.append({
                    "review_id": r_id,
                    "platform": "naver_shopping",
                    "product_id": PRODUCT_INTERNAL_ID,
                    "rating": float(rating),
                    "review_text": content,
                    "reviewer_name": "naver_user",
                    "purchase_yn": True,
                    "helpful_count": 0,
                    "review_date": datetime.datetime.now(),  # 실제 날짜 파싱 미구현 
                    "collected_at": datetime.datetime.now()
                })
                print("-" * 40)
        except:
            continue

    # 루프 완료 후 한 번에 저장 — 건별 저장 시 커넥션 과부하 방지
    save_to_db(parsed_reviews)

except Exception as e:
    print(f"❌ 에러: {e}")

finally:
    driver.quit()