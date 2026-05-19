# 테이블 정의서

> **스키마:** `project`  
> **작성일:** 2026.02.27  
> **최종 수정일:** 2026.03.03

---

## 📑 목차

1. [products - 상품 정보](#1-products---상품-정보)
2. [videos - 영상 정보](#2-videos---영상-정보)
3. [video_summary - 영상 댓글 요약](#3-video_summary---영상-댓글-요약)
4. [comments - 댓글](#4-comments---댓글)
5. [comment_cleansed - 댓글 전처리](#5-comment_cleansed---댓글-전처리)
6. [comments_analysis - 댓글 분석](#6-comments_analysis---댓글-분석)
7. [platform_reviews - 플랫폼 리뷰 (신규)](#7-platform_reviews---플랫폼-리뷰-신규)
8. [platform_review_cleansed - 플랫폼 리뷰 전처리 (신규)](#8-platform_review_cleansed---플랫폼-리뷰-전처리-신규)
9. [platform_reviews_analysis - 플랫폼 리뷰 분석 (신규)](#9-platform_reviews_analysis---플랫폼-리뷰-분석-신규)


---

## 1. `products` - 상품 정보
| 컬럼명 | 데이터 타입 | NULL 허용 | KEY | 설명 |
| :--- | :--- | :---: | :---: | :--- |
| `product_id` | SERIAL | NOT NULL | PK | 상품 고유 ID (자동 증가) |
| `product_name` | VARCHAR(200) | NOT NULL | | 상품명 |
| `brand_name` | VARCHAR(200) | NULL | | 브랜드명 |

---

## 2. `videos` - 영상 정보
| 컬럼명 | 데이터 타입 | NULL 허용 | KEY | 설명 |
| :--- | :--- | :---: | :---: | :--- |
| `video_id` | VARCHAR(50) | NOT NULL | PK | 영상 고유 ID |
| `product_id` | INT | NULL | FK | 상품 고유 ID (`products` 참조) |
| `title` | TEXT | NULL | | 영상 제목 |
| `view_count` | INT | NULL | | 조회수 |
| `is_sponsored` | BOOLEAN | NOT NULL | | 협찬 영상 여부 (기본값: FALSE) |
| `collected_at` | TIMESTAMP | NULL | | 데이터 수집 일시 |

---

## 3. `video_summary` - 영상 댓글 요약
| 컬럼명 | 데이터 타입 | NULL 허용 | KEY | 설명 |
| :--- | :--- | :---: | :---: | :--- |
| `video_id` | VARCHAR(50) | NOT NULL | PK, FK | 영상 고유 ID (`videos` 참조) |
| `total_comment_threads` | INT | NULL | | 최상위 댓글 수 |
| `total_replies` | INT | NULL | | 대댓글 수 |
| `total_all` | INT | NULL | | 전체 댓글 수 (댓글 + 대댓글) |

---

## 4. `comments` - 댓글
| 컬럼명 | 데이터 타입 | NULL 허용 | KEY | 설명 |
| :--- | :--- | :---: | :---: | :--- |
| `comment_id` | VARCHAR(50) | NOT NULL | PK | 댓글 고유 ID |
| `video_id` | VARCHAR(50) | NULL | FK | 영상 고유 ID (`videos` 참조) |
| `parent_id` | VARCHAR(50) | NULL | | 상위 댓글 ID (대댓글인 경우 존재) |
| `text` | TEXT | NULL | | 댓글 내용 |
| `likes` | INT | NULL | | 좋아요 수 |

---

## 5. `comment_cleansing` - 댓글 전처리
| 컬럼명 | 데이터 타입 | NULL 허용 | KEY | 설명 |
| :--- | :--- | :---: | :---: | :--- |
| `comment_id` | VARCHAR(50) | NOT NULL | PK, FK | 댓글 고유 ID (`comments` 참조) |
| `original_text` | TEXT | NULL | | 원본 텍스트 복사본 |
| `cleaned_text` | TEXT | NULL | | 정제된 텍스트 |
| `spam_score` | INT | NULL | | 스팸 점수화 |
| `spam_level` | VARCHAR(20) | NOT NULL | | 스팸 등급 |
| `mentioned_brands` | TEXT | NULL | | 언급된 브랜드 목록 |
| `cleansed_at` | TIMESTAMP | NULL | | 전처리 수행 일시 |

---

## 6. `comments_analysis` - 댓글 분석
| 컬럼명 | 데이터 타입 | NULL 허용 | KEY | 설명 |
| :--- | :--- | :---: | :---: | :--- |
| `comment_id` | VARCHAR(50) | NOT NULL | PK, FK | 댓글 고유 ID (`comments` 참조) |
| `sentiment_score` | FLOAT | NULL | | 감성 점수 (연속형) |
| `sentiment_label` | VARCHAR(20) | NULL | | **(추가)** 감성 라벨 (`POSITIVE`, `NEGATIVE`, `NEUTRAL`) |
| `weighted_sentiment` | FLOAT | NULL | | **(추가)** 좋아요 가중치 반영 감성 점수 |
| `is_brand_mention` | BOOLEAN | NULL | | **(추가)** 해당 영상 제품(브랜드) 일치 언급 여부 |
| `is_purchase_intent` | BOOLEAN | NULL | | **(추가)** 구매 의도 포함 여부 |
| `purchase_intent_level` | INT | NULL | | **(추가)** 구매 의도 단계 (`0`, `1`, `2`, `3`) |
| `purchase_intent_label` | VARCHAR(10) | NULL | | **(추가)** 구매 의도 라벨 (`NONE`, `L1`, `L2`, `L3`) |
| `intent_confidence` | FLOAT | NULL | | **(추가)** 구매 의도 예측 신뢰도 |
| `crisis_flag` | BOOLEAN | NULL | | 위기 징후 여부 |
| `sentiment_pos` | FLOAT | NULL | | **(추가)** 긍정 점수 세부값 |
| `sentiment_neg` | FLOAT | NULL | | **(추가)** 부정 점수 세부값 |
| `attribute_tags` | TEXT | NULL | | **(추가)** 속성 태그 (뷰티 특화 카테고리:키워드) |
| `audience_segment` | VARCHAR(20) | NULL | | **(추가)** 시청자 페르소나 (`Loyal`, `Newbie`, `None`) |
| `analyzed_at` | TIMESTAMP | NULL | | **(추가)** 분석 완료 및 DB 저장 일시 |
| `inflow_segment` | VARCHAR(20) | NULL | | **(추가)** 유입 구간 |
---

## 7. `platform_reviews` - 플랫폼 리뷰 (신규)
| 컬럼명 | 데이터 타입 | NULL 허용 | KEY | 설명 |
| :--- | :--- | :---: | :---: | :--- |
| `review_id` | VARCHAR(100) | NOT NULL | PK | 리뷰 고유 ID |
| `platform` | VARCHAR(30) | NOT NULL | | 플랫폼 (oliveyoung, naver_shopping) |
| `product_id` | INT | NOT NULL | FK | 상품 고유 ID (`products` 참조) |
| `rating` | FLOAT | NULL | | 별점 |
| `review_text` | TEXT | NULL | | 리뷰 본문 내용 |
| `purchase_yn` | BOOLEAN | NULL | | 구매 인증 여부 |
| `review_date` | TIMESTAMP | NULL | | 리뷰 작성 일시 |

---

## 8. `platform_review_cleansing` - 플랫폼 리뷰 전처리 (신규)
| 컬럼명 | 데이터 타입 | NULL 허용 | KEY | 설명 |
| :--- | :--- | :---: | :---: | :--- |
| `review_id` | VARCHAR(100) | NOT NULL | PK, FK | 리뷰 ID (`platform_reviews` 참조) |
| `cleaned_text` | TEXT | NULL | | 정제된 텍스트 |
| `keywords` | TEXT | NULL | | 핵심 키워드 |
| `is_spam` | BOOLEAN | NOT NULL | | 스팸 여부 |
| `cleansed_at` | TIMESTAMP | NULL | | 전처리 일시 |

---

## 9. `platform_reviews_analysis` - 플랫폼 리뷰 분석 (신규)
| 컬럼명 | 데이터 타입 | NULL 허용 | KEY | 설명 |
| :--- | :--- | :---: | :---: | :--- |
| `review_id` | VARCHAR(100) | NOT NULL | PK, FK | 리뷰 ID (`platform_reviews` 참조) |
| `sentiment_score` | FLOAT | NULL | | 감성 점수 |
| `sentiment_label` | VARCHAR(20) | NULL | | 감성 레이블 |
| `is_purchase_intent` | BOOLEAN | NULL | | 구매 의도 여부 |
| `sentiment_pos` | FLOAT | NULL | | **(추가)** 긍정 점수 세부값 |
| `sentiment_neg` | FLOAT | NULL | | **(추가)** 부정 점수 세부값 |
| `attribute_tags` | TEXT | NULL | | **(추가)** 속성 태그 (뷰티 특화 카테고리:키워드) |
| `crisis_flag` | BOOLEAN | NULL | | 위기 징후 여부 |

---

## 10. `dashboard_total_score` - 종합 점수 대시보드 뷰 (신규)
| 컬럼명 | 데이터 타입 | NULL 허용 | KEY | 설명 |
| :--- | :--- | :---: | :---: | :--- |
| `video_id` | VARCHAR(50) | NOT NULL | PK | 비디오 고유 ID (논리적) |
| `title` | VARCHAR(255) | NOT NULL | | 비디오 제목 |
| `view_count` | INT | NULL | | 영상의 총 조회수 |
| `total_comments` | INT | NULL | | 영상의 총 댓글 수 |
| `quality_score` | NUMERIC | NULL | | 질적 밀도 점수 (최대 100점 환산) |
| `scale_score` | NUMERIC | NULL | | 양적 파급력 점수 (최대 100점 환산) |
| `total_score` | NUMERIC | NULL | | 최종 종합 점수 (질적 60% + 양적 40%) |

## 🔗 ERD 관계 요약

- **products**: `videos`, `product_metrics`, `reports`, `platform_reviews`와 연관
- **videos**: `video_summary`, `comments`, `video_metrics`와 연관
- **comments**: `comment_cleansing`, `comments_analysis`와 연관
- **platform_reviews**: `platform_review_cleansing`, `platform_reviews_analysis`와 연관
