import os
import re
import sys
import math
import logging
import argparse
import pickle
import importlib
import psycopg2
import psycopg2.extras
from pathlib import Path
from dotenv import load_dotenv
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import json
import datetime

import __main__
import purchase_intent_model_v4

# purchase_intent_model_v4의 클래스/함수를 __main__ 네임스페이스에 등록
# (pickle로 저장된 모델 로드 시 클래스 참조 오류 방지)
for name in dir(purchase_intent_model_v4):
    if not name.startswith('_'):
        setattr(__main__, name, getattr(purchase_intent_model_v4, name))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)
load_dotenv()


# 1. SentimentAnalyzer
#    KcELECTRA 파인튜닝 모델 기반 감성 분석기.
#    모델 로드 실패 시 키워드 기반 폴백으로 자동 전환.

class SentimentAnalyzer:
    MODEL_PATH = Path(__file__).parent / "kcelectra_sentiment_final"
    MAX_LENGTH = 512

    def __init__(self):
        self._model = None

    def _get_model(self):
        """모델 지연 로드 (최초 호출 시 1회만 로드)"""
        if self._model is None:
            try:
                from transformers import pipeline
                self._model = pipeline(
                    task="text-classification",
                    model=str(self.MODEL_PATH),
                    tokenizer=str(self.MODEL_PATH),
                    top_k=None, truncation=True, max_length=self.MAX_LENGTH
                )
                logger.info(f"파인튜닝 모델 로드 완료: {self.MODEL_PATH}")
            except Exception as e:
                logger.warning(f"모델 로드 실패, 키워드 폴백 사용: {e}")
                self._model = "fallback"
        return self._model

    def analyze(self, text: str) -> dict:
        """단일 텍스트 감성 분석. POSITIVE / NEGATIVE / NEUTRAL 반환."""
        if not text or not text.strip():
            return {"label": "NEUTRAL", "score": 0.5, "detail": {"pos": 0.0, "neg": 0.0}}

        model = self._get_model()
        if model == "fallback":
            pos_score = 0.6 if any(k in text for k in ["좋아", "추천", "최고"]) else 0.1
            neg_score = 0.6 if any(k in text for k in ["별로", "실망", "트러블"]) else 0.1
            label = "POSITIVE" if pos_score > neg_score else ("NEGATIVE" if neg_score > pos_score else "NEUTRAL")
            return {"label": label, "score": max(pos_score, neg_score), "detail": {"pos": pos_score, "neg": neg_score}}

        raw = model(text[:self.MAX_LENGTH])[0]
        score_map = {r["label"].upper(): r["score"] for r in raw}
        pos, neg = score_map.get("POSITIVE", 0.0), score_map.get("NEGATIVE", 0.0)
        label = "POSITIVE" if pos > neg else "NEGATIVE"
        return {"label": label, "score": round(max(pos, neg), 4), "detail": {"pos": pos, "neg": neg}}

    def analyze_batch(self, texts: List[str], batch_size: int = 64) -> List[dict]:
        """배치 감성 분석. batch_size개씩 묶어 모델에 전달해 속도를 높임."""
        model = self._get_model()
        results = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            if model == "fallback":
                for t in batch:
                    pos_score = 0.6 if any(k in t for k in ["좋아", "추천", "최고"]) else 0.1
                    neg_score = 0.6 if any(k in t for k in ["별로", "실망", "트러블"]) else 0.1
                    lbl = "POSITIVE" if pos_score > neg_score else ("NEGATIVE" if neg_score > pos_score else "NEUTRAL")
                    results.append({"label": lbl, "score": max(pos_score, neg_score), "detail": {"pos": pos_score, "neg": neg_score}})
                continue
            valid = [t[:self.MAX_LENGTH] if t and t.strip() else " " for t in batch]
            raws = model(valid)
            for t, raw in zip(batch, raws):
                if not t or not t.strip():
                    results.append({"label": "NEUTRAL", "score": 0.5, "detail": {"pos": 0.0, "neg": 0.0}})
                    continue
                score_map = {r["label"].upper(): r["score"] for r in raw}
                pos, neg = score_map.get("POSITIVE", 0.0), score_map.get("NEGATIVE", 0.0)
                lbl = "POSITIVE" if pos > neg else "NEGATIVE"
                results.append({"label": lbl, "score": round(max(pos, neg), 4), "detail": {"pos": pos, "neg": neg}})
            logger.info(f"감성 분석: {min(i + batch_size, len(texts))}/{len(texts)}건 완료")
        return results


# 2. BrandTagger
#    DB의 videos-products 조인으로 영상별 브랜드를 로드.
#    영상 단위로 1:1 매칭해 타 영상 브랜드 오염 방지.

class BrandTagger:

    def __init__(self):
        self.video_brand_map = self._load_video_brands_from_db()

    def _load_video_brands_from_db(self) -> Dict[str, List[str]]:
        """DB에서 영상-브랜드 매핑을 딕셔너리로 로드. {'video_id': ['브랜드명', ...]}"""
        mapping = {}
        try:
            conn = psycopg2.connect(
                host=os.getenv("DB_HOST", "localhost"),
                port=os.getenv("DB_PORT", "5432"),
                database=os.getenv("DB_NAME", "postgres"),
                user=os.getenv("DB_USER", "postgres"),
                password=os.getenv("DB_PASSWORD", "")
            )
            query = """
                SELECT v.video_id, p.brand_name
                FROM project.videos v
                JOIN project.products p ON v.product_id = p.product_id
                WHERE p.brand_name IS NOT NULL;
            """
            with conn.cursor() as cur:
                cur.execute(query)
                for vid, brand in cur.fetchall():
                    vid_str = str(vid).strip()
                    brand_str = str(brand).strip().lower()
                    if vid_str not in mapping:
                        mapping[vid_str] = []
                    mapping[vid_str].append(brand_str)
            conn.close()
            logger.info("DB에서 브랜드 매핑 로드 완료")
        except Exception as e:
            logger.error(f"영상-브랜드 매핑 로드 실패: {e}")
        return mapping

    def tag(self, text: str, video_id: str) -> bool:
        """해당 영상의 브랜드 키워드가 텍스트에 포함되면 True 반환."""
        target_brands = self.video_brand_map.get(str(video_id), [])
        if not target_brands:
            return False
        return any(b in text.lower() for b in target_brands)


# 3. AttributeTagger
#    뷰티 특화 키워드 사전으로 댓글에서 속성 추출.
#    "카테고리:키워드" 형태로 반환. (예: "제형_사용감:촉촉")

class AttributeTagger:

    BEAUTY_DICT = {
        "피부타입": ["수부지", "건성", "악건성", "지성", "복합성", "민감성", "건복합", "지복합", "민복합"],
        "피부고민": ["여드름", "트러블", "홍조", "붉은기", "각질", "모공", "요철", "기미", "잡티", "주름", "탄력저하", "속당김", "화이트헤드", "블랙헤드", "피지", "흉터", "흔적", "민감", "알레르기", "건조", "지성", "복합", "민복합"],
        "효과_기능": ["보습", "진정", "미백", "커버력", "지속력", "다크닝", "쿨링", "톤업", "탄력", "광채", "세정력", "결광", "윤광", "장벽", "리프팅", "피지조절", "각질제거", "수분공급", "영양공급", "피부결개선", "모공개선", "주름개선", "미세먼지차단", "자외선차단", "항산화"],
        "제형_사용감": ["발림성", "촉촉", "매트", "세미매트", "뽀송", "보송", "끈적", "산뜻", "흡수", "밀착", "가벼운", "무거운", "건조", "당김", "꾸덕", "묽은", "워터리", "쫀쫀", "벨벳", "글로우", "겉보속촉", "뭉침", "들뜸", "밀림", "모공끼임", "화잘먹", "회끼"],
        "색조_컬러": ["웜톤", "쿨톤", "봄웜", "여쿨", "가을웜", "겨쿨", "17호", "19호", "21호", "22호", "23호", "핑크베이스", "핑베", "옐로베이스", "옐베", "상아색", "잿빛", "착색", "발색", "다크닝", "화사"],
        "주요성분": ["시카", "병풀", "마데카", "판테놀", "레티놀", "히알루론산", "비타민", "세라마이드", "나이아신", "어성초", "콜라겐", "티트리", "아하", "바하", "파하", "aha", "bha", "pha", "펩타이드", "스쿠알란", "쑥", "프로폴리스", "달팽이", "PDRN", "센텔라", "알로에", "녹차", "카모마일", "감초", "아줄렌", "글리세린", "세테아릴알코올", "에탄올", "향료"]
    }

    def tag(self, text: str) -> List[str]:
        """텍스트에서 뷰티 속성 키워드를 추출. '카테고리:키워드' 리스트 반환."""
        extracted_keywords = set()
        text_lower = text.lower()
        for category, kws in self.BEAUTY_DICT.items():
            for kw in kws:
                if kw in text_lower:
                    extracted_keywords.add(f"{category}:{kw}")
        return list(extracted_keywords)


# 4. AudienceTagger
#    키워드 기반 시청자 분류: Loyal(재구매) / Newbie(신규 유입) / None

class AudienceTagger:

    def __init__(self):
        self.SEGMENTS = {
            "Loyal": [
                "저번 마켓", "저번에", "이미 쓰고", "재구매", "정착", "n통째", "쓰고 있는데",
                "또 샀", "믿고 쓰", "항상", "원래 쓰던", "인생템", "다 써가서", "쟁여"
            ],
            "Newbie": [
                "처음", "입문", "사배", "영상 보고", "알고리즘", "영업 당해",
                "이제야", "뉴비", "방금 구독", "처음 써", "유입", "처음 사"
            ]
        }

    def tag(self, text: str) -> str:
        """Loyal → Newbie → None 순으로 판별해 첫 번째 매칭 반환."""
        text_lower = text.lower()
        if any(kw in text_lower for kw in self.SEGMENTS["Loyal"]):
            return "Loyal"
        if any(kw in text_lower for kw in self.SEGMENTS["Newbie"]):
            return "Newbie"
        return "None"


# 5. AnalysisResult
#    댓글 1건의 분석 결과를 담는 데이터 클래스.

@dataclass
class AnalysisResult:
    video_id: str
    comment_id: str
    sentiment_score: float
    sentiment_label: str
    weighted_sentiment: float          # sentiment_score * log(1 + likes)
    is_brand_mention: bool
    is_purchase_intent: bool
    purchase_intent_level: int         # 0(NONE) / 1(L1) / 2(L2) / 3(L3)
    purchase_intent_label: str         # NONE / L1 / L2 / L3
    intent_confidence: float
    crisis_flag: bool                  # NEGATIVE이고 neg_score > 0.7
    sentiment_pos: float
    sentiment_neg: float
    attribute_tags: str                # DB TEXT 타입: "보습|진정"
    audience_segment: str              # Loyal / Newbie / None
    inflow_segment: str                # Early / Expansion / Steady
    video_average_likes: float
    raw_likes: int                     # Event Hub 전송용 (DB 미저장)
    raw_attrs_list: list               # Event Hub 전송용 (DB 미저장)
    comment_published_at: object = None
    video_published_at: object = None


# 6. AnalyzerEngine
#    모든 분석기를 하나로 묶어 댓글 배치 분석을 수행하는 엔진 클래스.
#    process_batch()로 배치 단위 분석을 수행하고 AnalysisResult 리스트를 반환.

class AnalyzerEngine:

    def __init__(self, model_path: str = "purchase_intent_v4.pkl"):
        script_dir = Path(__file__).parent
        self.model_path = script_dir / model_path

        self.sentiment      = SentimentAnalyzer()
        self.brand_tagger   = BrandTagger()
        self.attr_tagger    = AttributeTagger()
        self.audience_tagger = AudienceTagger()
        self._load_intent_model(self.model_path)

    def _load_intent_model(self, path):
        """구매의도 v4 모델 로드. 파일이 없으면 None으로 처리."""
        try:
            import purchase_intent_model_v4
            self.intent_model = purchase_intent_model_v4.PurchaseIntentModel.load(path)
            logger.info(f"구매의도 모델 로드 완료: {path}")
        except Exception as e:
            self.intent_model = None
            logger.error(f"구매의도 모델 로드 실패: {e}")
            logger.warning("구매의도 모델 없이 기본값으로 처리합니다.")

    def process_batch(self, rows: List[Dict], batch_size: int = 64) -> List[AnalysisResult]:
        """댓글 행 리스트를 받아 배치 감성 분석 후 AnalysisResult 리스트 반환."""
        texts        = [row["text"] for row in rows]
        sent_results = self.sentiment.analyze_batch(texts, batch_size=batch_size)

        results = []
        for row, sent in zip(rows, sent_results):
            text     = row["text"]
            likes    = row.get("likes", 0)
            video_id = row.get("video_id", "")

            is_brand = self.brand_tagger.tag(text, video_id)
            attrs    = self.attr_tagger.tag(text)
            audience = self.audience_tagger.tag(text)

            weighted  = round(sent["score"] * math.log1p(likes), 4)
            is_crisis = sent["label"] == "NEGATIVE" and sent["detail"]["neg"] > 0.7

            is_intent    = False
            intent_level = 0
            intent_label = "NONE"
            intent_conf  = 0.0
            if hasattr(self, "intent_model") and self.intent_model is not None:
                try:
                    pred         = self.intent_model.predict_one({"original_text": text})
                    is_intent    = bool(pred.get("is_purchase_intent", False))
                    intent_level = int(pred.get("purchase_intent_level", 0))
                    intent_label = str(pred.get("purchase_intent_label", "NONE"))
                    intent_conf  = float(pred.get("intent_confidence", 0.0))
                except Exception as e:
                    logger.error(f"predict_one 에러: {e} (텍스트: {text[:20]}...)")

            inflow_segment = "알 수 없음"
            v_pub = row.get("video_published_at")
            c_pub = row.get("comment_published_at")
            if v_pub and c_pub:
                diff_hours = (c_pub - v_pub).total_seconds() / 3600
                if diff_hours < 48:
                    inflow_segment = "Early"
                elif diff_hours < 168:
                    inflow_segment = "Expansion"
                else:
                    inflow_segment = "Steady"

            results.append(AnalysisResult(
                video_id=video_id,
                comment_id=row["comment_id"],
                sentiment_score=sent["score"],
                sentiment_label=sent["label"],
                weighted_sentiment=weighted,
                is_brand_mention=is_brand,
                is_purchase_intent=is_intent,
                purchase_intent_level=intent_level,
                purchase_intent_label=intent_label,
                intent_confidence=intent_conf,
                crisis_flag=is_crisis,
                sentiment_pos=sent["detail"]["pos"],
                sentiment_neg=sent["detail"]["neg"],
                attribute_tags="|".join(attrs) if attrs else None,
                audience_segment=audience,
                inflow_segment=inflow_segment,
                raw_likes=likes,
                video_average_likes=float(row.get("video_average_likes", 0.0)),
                raw_attrs_list=attrs,
                comment_published_at=row.get("comment_published_at"),
                video_published_at=row.get("video_published_at"),
            ))

        return results


# 7. run_pipeline
#    STEP 1. 미분석 댓글 조회 (comment_cleansed JOIN comments JOIN videos)
#    STEP 2. 64건 배치 분석 (AnalyzerEngine.process_batch)
#    STEP 3. 배치 즉시 DB 저장 (comments_analysis UPSERT)
#
#    limit=N → 테스트 모드 (N건만 처리), 미지정 → 전체 실행.

def run_pipeline(limit: int = None):
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        database=os.getenv("DB_NAME", "postgres"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "")
    )

    try:
        # STEP 1: 미분석 댓글 조회
        # comment_cleansed에 있고 스팸이 아니며 comments_analysis에 없는 것만 대상
        if limit:
            logger.info(f"테스트 모드: {limit}건 제한 실행")
            query_fetch = """
                SELECT
                    cc.comment_id,
                    COALESCE(cc.cleaned_text, cc.original_text) AS text,
                    c.likes,
                    c.video_id,
                    c.published_at AS comment_published_at,
                    v.published_at AS video_published_at,
                    AVG(c.likes) OVER(PARTITION BY c.video_id) AS video_average_likes
                FROM project.comment_cleansed cc
                JOIN project.comments c ON cc.comment_id = c.comment_id
                JOIN project.videos v ON c.video_id = v.video_id
                LEFT JOIN project.comments_analysis ca ON cc.comment_id = ca.comment_id
                WHERE ca.comment_id IS NULL AND cc.spam_level != '스팸'
                LIMIT %s
            """
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query_fetch, (limit,))
                rows = cur.fetchall()
        else:
            logger.info("라이브 모드: 미분석 신규 댓글 전체 실행")
            query_fetch = """
                SELECT
                    cc.comment_id,
                    COALESCE(cc.cleaned_text, cc.original_text) AS text,
                    c.likes,
                    c.video_id,
                    c.published_at AS comment_published_at,
                    v.published_at AS video_published_at,
                    AVG(c.likes) OVER(PARTITION BY c.video_id) AS video_average_likes
                FROM project.comment_cleansed cc
                JOIN project.comments c ON cc.comment_id = c.comment_id
                JOIN project.videos v ON c.video_id = v.video_id
                LEFT JOIN project.comments_analysis ca ON cc.comment_id = ca.comment_id
                WHERE ca.comment_id IS NULL AND cc.spam_level != '스팸'
            """
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query_fetch)
                rows = cur.fetchall()

        if not rows:
            logger.info("분석할 신규 댓글 없음")
            return

        import time
        engine     = AnalyzerEngine()
        BATCH_SIZE = 64

        query_insert = """
            INSERT INTO project.comments_analysis (
                comment_id, sentiment_score, sentiment_label, weighted_sentiment,
                is_brand_mention, is_purchase_intent,
                purchase_intent_level, purchase_intent_label, intent_confidence,
                crisis_flag, sentiment_pos, sentiment_neg, attribute_tags,
                audience_segment, inflow_segment
            ) VALUES %s
            ON CONFLICT (comment_id) DO UPDATE SET
                is_purchase_intent    = EXCLUDED.is_purchase_intent,
                purchase_intent_level = EXCLUDED.purchase_intent_level,
                purchase_intent_label = EXCLUDED.purchase_intent_label,
                intent_confidence     = EXCLUDED.intent_confidence,
                sentiment_score       = EXCLUDED.sentiment_score,
                sentiment_label       = EXCLUDED.sentiment_label,
                crisis_flag           = EXCLUDED.crisis_flag,
                analyzed_at           = CURRENT_TIMESTAMP,
                audience_segment      = EXCLUDED.audience_segment,
                inflow_segment        = EXCLUDED.inflow_segment;
        """

        total_saved = 0

        for batch_start in range(0, len(rows), BATCH_SIZE):
            batch_rows = rows[batch_start:batch_start + BATCH_SIZE]

            # STEP 2: 배치 분석 (엔진에 위임)
            t0      = time.time()
            results = engine.process_batch(batch_rows, batch_size=BATCH_SIZE)

            # STEP 3: 배치 즉시 DB 저장
            insert_data = [
                (r.comment_id, r.sentiment_score, r.sentiment_label, r.weighted_sentiment,
                 r.is_brand_mention, r.is_purchase_intent,
                 r.purchase_intent_level, r.purchase_intent_label, r.intent_confidence,
                 r.crisis_flag, r.sentiment_pos, r.sentiment_neg,
                 r.attribute_tags, r.audience_segment, r.inflow_segment)
                for r in results
            ]
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, query_insert, insert_data)
            conn.commit()
            total_saved += len(results)
            logger.info(f"{total_saved}/{len(rows)}건 DB 저장 완료 ({time.time() - t0:.1f}초)")

        logger.info(f"전체 완료: {total_saved}건 분석/저장")

    except Exception as e:
        logger.error(f"오류 발생: {e}")
        conn.rollback()
    finally:
        conn.close()


if __name__ == "__main__":
    run_pipeline()