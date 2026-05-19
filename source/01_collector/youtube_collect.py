# YouTube 댓글 수집 및 DB 저장 스크립트

from googleapiclient.discovery import build
import psycopg2
from datetime import datetime, timezone
from dotenv import load_dotenv
import os
import logging
import time
import hashlib
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
api_key = os.getenv("YOUTUBE_API_KEY")

# (video_id, product_id, is_sponsored)
# 협찬 영상 구분은 is_sponsored 플래그로 관리 — products 테이블 FK 매핑 필수
# 새 영상 추가 시 products 테이블에 먼저 등록 후 product_id 확인 필요
VIDEO_LIST = [
    ("yLMSqbUD7FE", 5, True),
    ("3v368YxlKZY", 5, True),
    ("l0lop8WpXi4", 5, True)
]

youtube = build("youtube", "v3", developerKey=api_key)

# YouTube Data API v3 일일 쿼터 10,000 unit
# commentThreads.list 1회 = 1 unit, 대댓글 추가 수집 시 영상당 쿼터 소모 급증 주의
DAILY_QUOTA_LIMIT = 10000
quota_used = 0

def check_quota(units: int):
    global quota_used
    quota_used += units
    logger.info(f"쿼터 사용량: {quota_used}/{DAILY_QUOTA_LIMIT} (+{units})")
    if quota_used >= DAILY_QUOTA_LIMIT:
        raise Exception(f"일일 쿼터 초과: {quota_used} 유닛 사용")


# 작성자명 비식별화 — 개인정보 보호 목적
# salt + SHA-256: 동일 유저 추적은 가능하되 원본 복구 불가 설계
# MASK_SALT는 반드시 .env에서 로드 (코드 하드코딩 금지)
# salt 없으면 dictionary attack으로 원본 복구 가능해져서 필수
SALT = os.getenv("MASK_SALT")
if not SALT:
    raise ValueError("MASK_SALT 환경변수 미설정")

def mask_name(name: str) -> str:
    if not name:
        return name
    # @ 멘션 형식 처리 — @ 제거 후 해싱
    target = name[1:] if name.startswith("@") else name
    if not target:
        return None
    digest = hashlib.sha256(f"{SALT}:{target}".encode("utf-8")).hexdigest()[:16]
    return f"user_{digest}"

"""
영상 1개 수집 + DB 저장 (트랜잭션 단위)
skip_video_upsert=True: 댓글만 재수집할 때 사용 (views/likes 업데이트 불필요한 경우)
예외 발생 시 자동 rollback — 호출부에서 다음 영상으로 continue 처리
"""
def process_video(video_id: str, product_id: int, is_sponsored: bool, conn,
                  skip_video_upsert: bool = False):

    try:
        with conn:
            with conn.cursor() as cur:

                # 1. 영상 메타데이터 수집 (videos.list = 1 unit)
                check_quota(1)
                video_response = youtube.videos().list(
                    part="snippet,statistics",
                    id=video_id
                ).execute()

                if not video_response.get("items"):
                    raise ValueError(f"존재하지 않는 video_id: {video_id}")

                video         = video_response["items"][0]
                title         = video["snippet"]["title"]
                channel_name  = video["snippet"]["channelTitle"]
                channel_id    = video["snippet"]["channelId"]
                published_at  = video["snippet"]["publishedAt"]
                description   = video["snippet"]["description"]
                view_count    = int(video["statistics"].get("viewCount", 0))
                like_count    = int(video["statistics"].get("likeCount", 0))
                comment_count = int(video["statistics"].get("commentCount", 0))
                url           = f"https://www.youtube.com/watch?v={video_id}"
                collected_at  = datetime.now(timezone.utc)

                # 2. 영상 정보 UPSERT
                # 이미 있으면 최신 데이터로 업데이트
                if not skip_video_upsert:
                    cur.execute("""
                        INSERT INTO project.videos (
                            video_id, title, channel_name, channel_id,
                            published_at, description, view_count,
                            like_count, comment_count, url, collected_at,
                            product_id, is_sponsored
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (video_id) DO UPDATE SET
                            channel_name  = EXCLUDED.channel_name,
                            view_count    = EXCLUDED.view_count,
                            like_count    = EXCLUDED.like_count,
                            comment_count = EXCLUDED.comment_count,
                            collected_at  = EXCLUDED.collected_at,
                            is_sponsored  = EXCLUDED.is_sponsored;
                    """, (
                        video_id, title, channel_name, channel_id,
                        published_at, description, view_count,
                        like_count, comment_count, url, collected_at,
                        product_id, is_sponsored
                    ))
                    logger.info(f"✓ [{video_id}] 비디오 정보 저장 완료")
                else:
                    logger.info(f"⏭ [{video_id}] 비디오 정보 저장 건너뜀")

                # 3. 최상위 댓글 전체 수집 (페이지네이션)
                # maxResults=100 고정 — API 최대값, 줄이면 쿼터 낭비
                all_comment_items = []
                next_page_token   = None
                page_count        = 0

                while True:
                    kwargs = dict(part="snippet,replies", videoId=video_id, maxResults=100)
                    if next_page_token:
                        kwargs["pageToken"] = next_page_token

                    check_quota(1)  # commentThreads.list = 1 unit
                    comment_response = youtube.commentThreads().list(**kwargs).execute()

                    page_count += 1
                    items = comment_response.get("items", [])
                    all_comment_items.extend(items)
                    logger.info(f"📄 [{video_id}] 페이지 {page_count}: {len(items)}개 수신")

                    next_page_token = comment_response.get("nextPageToken")
                    if not next_page_token:
                        break
                    time.sleep(1)

                logger.info(f"✓ [{video_id}] 댓글 수집 완료: 총 {len(all_comment_items)}개")

                # 4. 대댓글 추가 수집
                # commentThreads.list replies 필드는 최대 5개만 포함
                # totalReplyCount > fetched_replies 일 때만 추가 API 호출 (쿼터 절약)
                for item in all_comment_items:
                    total_reply_count = item["snippet"].get("totalReplyCount", 0)
                    fetched_replies   = len(item.get("replies", {}).get("comments", []))

                    if total_reply_count > fetched_replies:
                        comment_id = item["snippet"]["topLevelComment"]["id"]
                        item["replies"] = {"comments": []}
                        reply_next_token = None

                        while True:
                            r_kwargs = dict(part="snippet", parentId=comment_id, maxResults=100)
                            if reply_next_token:
                                r_kwargs["pageToken"] = reply_next_token

                            check_quota(1)  # comments.list = 1 unit
                            reply_response = youtube.comments().list(**r_kwargs).execute()
                            item["replies"]["comments"].extend(reply_response.get("items", []))

                            reply_next_token = reply_response.get("nextPageToken")
                            if not reply_next_token:
                                break
                            time.sleep(1)

                # 5. 저장 row 구성
                # 업로더(채널 주인) 본인 댓글 제외 — channel_id 비교로 필터링
                # 업로더 댓글 포함 시 감성 분석 왜곡 가능성 있어서 제거
                rows = []

                for item in all_comment_items:
                    top = item["snippet"]["topLevelComment"]
                    ts  = top["snippet"]

                    author_chid = ts.get("authorChannelId", {}).get("value")
                    if author_chid == channel_id:
                        continue

                    comment_id = top["id"]
                    rows.append((
                        comment_id, video_id, None,
                        mask_name(ts["authorDisplayName"]), author_chid,
                        ts["textDisplay"], ts.get("likeCount", 0),
                        ts["publishedAt"], ts["updatedAt"],
                    ))

                    if "replies" in item:
                        for reply in item["replies"]["comments"]:
                            r = reply["snippet"]
                            reply_chid = r.get("authorChannelId", {}).get("value")
                            if reply_chid == channel_id:
                                continue
                            rows.append((
                                reply["id"], video_id, comment_id,
                                mask_name(r["authorDisplayName"]), reply_chid,
                                r["textDisplay"], r.get("likeCount", 0),
                                r["publishedAt"], r["updatedAt"],
                            ))

                # 6. video_summary UPSERT — 재수집 시에도 최신 카운트로 덮어쓰기
                total_replies_saved = sum(1 for row in rows if row[2] is not None)
                total_threads_saved = sum(1 for row in rows if row[2] is None)

                cur.execute("""
                    INSERT INTO project.video_summary (
                        video_id, total_comment_threads,
                        total_replies, total_all
                    ) VALUES (%s,%s,%s,%s)
                    ON CONFLICT (video_id) DO UPDATE SET
                        total_comment_threads = EXCLUDED.total_comment_threads,
                        total_replies         = EXCLUDED.total_replies,
                        total_all             = EXCLUDED.total_all;
                """, (video_id, total_threads_saved, total_replies_saved, len(rows)))

                logger.info(f"✓ [{video_id}] video_summary 저장 완료")

                # 7. 댓글 bulk insert
                # ON CONFLICT DO NOTHING — 재수집 시 기존 댓글 덮어쓰지 않음 (중복 방어)
                # page_size=1000 — 너무 크게 잡으면 메모리 문제 가능, 1000으로 유지
                if rows:
                    execute_values(
                        cur,
                        """
                        INSERT INTO project.comments (
                            comment_id, video_id, parent_id,
                            author_name, author_channel_id,
                            text, likes, published_at, updated_at
                        ) VALUES %s
                        ON CONFLICT (comment_id) DO NOTHING;
                        """,
                        rows, page_size=1000
                    )

                logger.info(f"✓ [{video_id}] 댓글 저장 완료: {len(rows)}행")

        logger.info(f"🎉 [{video_id}] 전체 처리 완료 (COMMIT)")

    except Exception as e:
        # with conn 블록 벗어나면 자동 rollback
        logger.error(f"❌ [{video_id}] 처리 실패 (ROLLBACK): {e}")
        raise


def main():
    # 댓글만 재수집할 때 True로 변경 (views/likes 업데이트 불필요한 경우)
    SKIP_VIDEO_UPSERT = False

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        database=os.getenv("DB_NAME", "postgres"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )

    try:
        for video_id, product_id, is_sponsored in VIDEO_LIST:
            logger.info(f"\n{'='*60}")
            logger.info(f"▶ 처리 시작: {video_id} (is_sponsored={is_sponsored})")
            logger.info(f"{'='*60}")

            try:
                process_video(video_id, product_id, is_sponsored, conn,
                              skip_video_upsert=SKIP_VIDEO_UPSERT)
                logger.info(f"✅ [{video_id}] 처리 성공")

            except Exception as e:
                # 한 영상 실패해도 다음 영상으로 계속 진행
                logger.error(f"❌ [{video_id}] 실패 — 다음 영상으로 넘어갑니다: {e}")
                continue

        logger.info("\n🎉 전체 영상 처리 완료")

    finally:
        conn.close()
        logger.info("✓ 데이터베이스 연결 종료")


def run_collect():
    main()

if __name__ == "__main__":
    main()