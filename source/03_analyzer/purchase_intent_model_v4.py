"""
purchase_intent_model_v4.py
============================
LUMIQ 구매전환의도 분류 모델 v4

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v4 신규 기능
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1) 엔티티 마스킹 (타겟의존성 해결) 제거

  2) 시간성 분리 (L3 세분화)
     L3_즉시  : "방금 결제했어요", "바로 구매"
     L3_대기  : "마켓 또 언제 해요", "다시 열어주세요", "기다릴게요"

  3) 대기/재구매 수요 피처 추가
     "마켓 또", "언제 열어", "재입고", "현기증" 등

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
레벨 정의 (확정)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  NONE   (0) : 의미없음/스팸  "ㅋㅋ", "1빠", "토토"
  L1     (1) : 단순 긍정/칭찬 "피부 좋아보인다", "[타상품] 써봤어요"
  L2     (2) : 탐색/고민      "얼마예요?", "써도 되나요?", "고민돼요"
  L3     (3) : 구매/전환       "결제했어요", "마켓 또 열어줘", "기다릴게요"
  → DB 저장 시:
    is_purchase_intent = level >= 2
    purchase_intent_level: 0/1/2/3

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
실행 방법
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  # 로컬 — CSV 두 개로 바로 학습
  python purchase_intent_model_v4.py \
    --dummy-csv dummy_comments_200.csv \
    --real-csv  labeled_real_comments_200.csv

  # 로컬 — DB 실데이터 추가
  python purchase_intent_model_v4.py \
    --dummy-csv dummy_comments_200.csv \
    --real-csv  labeled_real_comments_200.csv \
    --use-db --env-path .env

  # 저장된 모델로 예측만
  python purchase_intent_model_v4.py --predict

  # 하이퍼파라미터 튜닝 포함
  python purchase_intent_model_v4.py \
    --dummy-csv dummy_comments_200.csv \
    --real-csv  labeled_real_comments_200.csv \
    --tune
"""

from __future__ import annotations

import os, re, json, pickle, logging, argparse, warnings
from pathlib import Path
from collections import Counter
from typing import Optional

import numpy as np
import pandas as pd
import scipy.sparse as sp

from sklearn.ensemble import GradientBoostingClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.model_selection import StratifiedKFold, cross_validate, GridSearchCV
from sklearn.metrics import classification_report, confusion_matrix, accuracy_score
from sklearn.base import BaseEstimator, TransformerMixin

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# 0. 레이블 상수
# ══════════════════════════════════════════════════════════════

STR_TO_INT = {"NONE": 0, "L1": 1, "L2": 2, "L3": 3}
INT_TO_STR = {v: k for k, v in STR_TO_INT.items()}
N_CLASSES  = 4
MODEL_PATH = Path("purchase_intent_v4.pkl")


def refine_l3(text: str, label: str) -> str:
    """L3_즉시 / L3_대기 / L3 를 모두 L3 로 통일"""
    if label in ("L3_즉시", "L3_대기", "L3"):
        return "L3"
    return label


# ══════════════════════════════════════════════════════════════
# 1. 엔티티 마스킹 (타겟의존성 해결)
# ══════════════════════════════════════════════════════════════

# 메인 상품 키워드 (영상별로 교체 가능 — 현재 선크림 마켓 기준)
# 엔티티 마스킹 제거 — 학습/예측 모두 원문 텍스트 그대로 사용


# ══════════════════════════════════════════════════════════════
# 2. 규칙 기반 피처 사전
# ══════════════════════════════════════════════════════════════

RULE_DICT = {
    # L3_즉시 시그널
    "l3_bought": [
        "구매했", "구매함", "샀어", "샀다", "샀습니다", "결제했",
        "결제 완료", "주문했", "주문 완료", "주문완료",
        "득템했", "장바구니 담았", "배송중", "받았어요",
        "바로 구매", "지금 사러", "당장 사러", "지름",
    ],
    "l3_will_buy": [
        "살게요", "살게", "살거야", "사야겠다", "사야지", "꼭 사야",
        "무조건 사", "지금 당장 사", "믿고 삽니다", "언니 믿고",
        "재구매", "정기구매", "또 살",
        "올영 가야겠다", "올리브영 가야겠다",
        "지르러", "질러", "지름",
    ],
    # L3 대기 수요 (L3_즉시와 합산 → 모두 L3)
    "l3_waiting": [
        "또 언제", "언제 또", "언제 열", "또 열어", "다시 열어",
        "재오픈", "재입고", "현기증", "기다릴게요", "기다리고 있",
        "다음에 꼭", "다음에 살", "나중에 살",
        "마켓 또", "또 마켓", "언제 마켓", "마켓 언제",
        "두 번째 마켓", "다음 마켓",
    ],
    # L2 탐색/고민
    "l2_inquiry": [
        "얼마예요", "얼마에요", "가격이", "가격은", "얼마나 해요",
        "어디서 사요", "어디서 팔아요", "링크", "구매 링크",
        "제품명이", "브랜드가", "뭐예요", "뭐에요", "뭔가요",
        "올리브영에 있나요", "쿠팡에도", "어디서 구해",
        "몇 호", "용량이", "성분이", "국내에 있나요",
    ],
    "l2_hesitate": [
        "고민", "살까말까", "살지말지", "사볼까", "망설",
        "어떨까요", "맞을까요", "효과 있을까", "맞을지",
        "뒤집어질까봐", "트러블 날까봐",
        "써도 되나요", "써도 될까요", "괜찮을까요",
        "지성한테도", "건성한테도", "민감성인데",
    ],
    "l2_compare": [
        "비교", " vs ", "어떤 게 더", "뭐가 더", "차이가",
        "둘 중", "뭐가 나아", "어떤 게 좋아요",
    ],
    "l2_condition": [
        "다 쓰면", "다음에 살", "월급 나오면", "세일하면",
        "쿠폰 있으면", "할인하면", "아직 남아서", "조금 남아서",
        "다음 달에",
    ],
    # L1 단순 긍정/칭찬
    "l1_product_praise": [
        "예쁘다", "예쁘네", "좋아 보여요", "좋아보여",
        "피부 좋아지", "효과 좋아 보여", "탐나요", "탐난다",
        "갖고싶다", "써보고 싶", "써봐야겠", "사고싶다",
    ],
    "l1_video_praise": [
        "잘 봤습니다", "잘 봤어요", "오늘도 잘", "재밌게 봤",
        "유익한 영상", "도움이 됐", "정보 감사", "알려줘서 감사",
        "편집 잘", "구독했", "알림 켰", "응원해요",
    ],
    # NONE
    "none_spam": [
        "토토", "카지노", "먹튀", "대출", "홍보합니다",
        "클릭하면", "http", "구독자 늘리",
    ],
    "none_noise": ["1빠", "2빠", "3빠"],
}

NEGATION_RE = [
    r'(사기|살|구매하기|쓰기)\s*(싫|싫어|싫다|않|안|못)',
    r'(살|구매할)\s*생각\s*(없|안)',
    r'절대\s*(안|못)\s*(사|구매)',
]

RULE_FEAT_NAMES = (
    [f"rule_{k}" for k in RULE_DICT.keys()]
    + [
        "negation", "has_question", "has_timestamp",
        "text_len_bin", "exclaim_count",
        "emo_positive", "emo_negative",
        "repeat_ㅠ", "repeat_ㅋ", "only_short",
        # 시간성 피처
        "market_reopen",
        "future_intent",
    ]
)


def _rule_features(text: str) -> list[float]:
    """원문 텍스트에서 규칙 피처 추출"""
    t = text.lower()
    feats = [float(any(kw in t for kw in kws)) for kws in RULE_DICT.values()]
    feats += [
        float(any(re.search(p, text) for p in NEGATION_RE)),
        float("?" in text),
        float(bool(re.search(r'\d{1,2}:\d{2}', text))),
        float(min(len(text) // 20, 6)),
        float(min(text.count("!"), 3)),
        float(bool(re.search(r'[😍🥰✨💕👍🙌]', text))),
        float(bool(re.search(r'[😢😞😤💸]', text))),
        float("ㅠ" in text or "ㅜ" in text),
        float("ㅋ" in text),
        float(len(re.sub(r'\s', '', text)) < 5),
        float(any(kw in t for kw in ["마켓 또", "또 마켓", "언제 마켓", "재오픈", "재입고", "다시 열어", "또 열어"])),
        float(any(kw in t for kw in ["나중에", "다음에", "다음번", "기다릴", "다음 마켓"])),
    ]
    return feats


# ══════════════════════════════════════════════════════════════
# 3. 데이터 로드 함수
# ══════════════════════════════════════════════════════════════

def load_dummy_csv(csv_path: str) -> pd.DataFrame:
    """
    dummy_comments_200.csv 형식 로드
    필수 컬럼: text, label  (label: NONE/L1/L2/L3)
    """
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df.columns = df.columns.str.strip()

    rows = []
    for _, r in df.iterrows():
        text  = str(r["text"])
        label = refine_l3(text, str(r.get("label", "NONE")).strip())
        if label not in STR_TO_INT:
            label = "NONE"
        row = _make_row(f"dummy_{_}", text)
        row["purchase_label"] = STR_TO_INT[label]
        rows.append(row)

    result = pd.DataFrame(rows)
    logger.info(f"더미 CSV 로드: {len(result)}건  ({csv_path})")
    _log_dist(result)
    return result


def load_real_csv(csv_path: str) -> pd.DataFrame:
    """
    labeled_real_comments_200.csv 형식 로드
    필수 컬럼: original_text (또는 text), label
    선택 컬럼: comment_id, normalized_text
    """
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df.columns = df.columns.str.strip()

    text_col = "original_text" if "original_text" in df.columns else "text"
    id_col   = "comment_id"    if "comment_id"    in df.columns else None

    rows = []
    for i, r in df.iterrows():
        text  = str(r[text_col])
        label = refine_l3(text, str(r.get("label", "NONE")).strip())
        if label not in STR_TO_INT:
            label = "NONE"
        cid = str(r[id_col]) if id_col and id_col in r else f"real_{i}"
        row = _make_row(cid, text)
        row["purchase_label"] = STR_TO_INT[label]
        rows.append(row)

    result = pd.DataFrame(rows)
    logger.info(f"실제 CSV 로드: {len(result)}건  ({csv_path})")
    _log_dist(result)
    return result


def _make_row(comment_id: str, text: str, is_spam: bool = False) -> dict:
    """comment_cleansing 포맷 생성 (마스킹 없음)"""
    cleaned    = re.sub(r'[^\w\s가-힣ㄱ-ㅎㅏ-ㅣ]', ' ', text).strip()
    normalized = re.sub(r'(.)\1{2,}', r'\1\1', text)
    tokens     = " ".join(re.findall(r'[가-힣a-zA-Z]+', cleaned))
    nouns      = " ".join(re.findall(r'[가-힣]{2,}', cleaned))
    keywords   = " ".join(sorted(set(re.findall(r'[가-힣]{3,}', cleaned))))
    return {
        "comment_id":      comment_id,
        "original_text":   text,
        "cleaned_text":    cleaned,
        "normalized_text": normalized,
        "tokens":          tokens,
        "nouns":           nouns,
        "keywords":        keywords,
        "is_spam":         is_spam,
        "is_empty":        len(cleaned.strip()) == 0,
        "language":        "ko",
    }


def _log_dist(df: pd.DataFrame):
    dist = {INT_TO_STR[k]: v for k, v in sorted(Counter(df["purchase_label"]).items())}
    logger.info(f"  레이블 분포: {dist}")


# ══════════════════════════════════════════════════════════════
# 4. .env DB 연동
# ══════════════════════════════════════════════════════════════

def _load_env(env_path: Optional[str] = None) -> dict:
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_path, override=True)
    except ImportError:
        logger.warning("python-dotenv 미설치 → os.environ 사용")
    return {
        "host":     os.getenv("DB_HOST",     "localhost"),
        "port":     int(os.getenv("DB_PORT", "5432")),
        "dbname":   os.getenv("DB_NAME",     "postgres"),
        "user":     os.getenv("DB_USER",     "postgres"),
        "password": os.getenv("DB_PASSWORD", ""),
        "schema":   os.getenv("DB_SCHEMA",   "project"),
    }


def load_from_db(env_path: Optional[str] = None,
                 limit: Optional[int] = None) -> pd.DataFrame:
    """comment_cleansing 테이블 로드 → 자동 레이블"""
    try:
        import psycopg2
    except ImportError:
        raise ImportError("pip install psycopg2-binary")

    cfg    = _load_env(env_path)
    schema = cfg.pop("schema")
    sql = f"""
        SELECT cc.comment_id, c.text AS original_text,
               cc.cleaned_text, cc.normalized_text, cc.tokens,
               cc.nouns, cc.keywords, cc.is_spam, cc.is_empty, cc.language
        FROM {schema}.comment_cleansing cc
        JOIN {schema}.comments c ON cc.comment_id = c.comment_id
        WHERE cc.is_spam=FALSE AND cc.is_empty=FALSE AND cc.language='ko'
        ORDER BY RANDOM() {'LIMIT '+str(limit) if limit else ''};
    """
    conn = psycopg2.connect(**cfg)
    try:
        df = pd.read_sql(sql, conn)
    finally:
        conn.close()

    rows = []
    for _, r in df.iterrows():
        text  = str(r["original_text"])
        label = _auto_label(text)
        row   = _make_row(str(r["comment_id"]), text)
        row["purchase_label"] = label
        rows.append(row)

    result = pd.DataFrame(rows)
    logger.info(f"DB 로드: {len(result)}건")
    _log_dist(result)
    return result


def _auto_label(text: str) -> int:
    """규칙 기반 자동 레이블링"""
    t = text.lower()
    if any(re.search(p, text) for p in NEGATION_RE):
        return STR_TO_INT["NONE"]
    if any(kw in t for kw in RULE_DICT["none_spam"] + RULE_DICT["none_noise"]):
        return STR_TO_INT["NONE"]
    if any(kw in t for kw in RULE_DICT["l3w_waiting"]):
        return STR_TO_INT["L3_대기"]
    if any(kw in t for kw in RULE_DICT["l3_bought"] + RULE_DICT["l3_will_buy"]):
        return STR_TO_INT["L3_즉시"]
    if any(kw in t for kw in (RULE_DICT["l2_inquiry"] + RULE_DICT["l2_hesitate"]
                               + RULE_DICT["l2_compare"] + RULE_DICT["l2_condition"])):
        return STR_TO_INT["L2"]
    if any(kw in t for kw in RULE_DICT["l1_product_praise"] + RULE_DICT["l1_video_praise"]):
        return STR_TO_INT["L1"]
    if len(re.sub(r'\s', '', text)) < 5:
        return STR_TO_INT["NONE"]
    return STR_TO_INT["NONE"]


def upsert_to_db(db_rows: list[dict], env_path: Optional[str] = None):
    try:
        import psycopg2, psycopg2.extras
    except ImportError:
        raise ImportError("pip install psycopg2-binary")

    cfg    = _load_env(env_path)
    schema = cfg.pop("schema")
    sql = f"""
        INSERT INTO {schema}.comments_analysis
            (comment_id, is_purchase_intent, purchase_intent_level,
             purchase_intent_label, intent_confidence)
        VALUES %s
        ON CONFLICT (comment_id) DO UPDATE SET
            is_purchase_intent    = EXCLUDED.is_purchase_intent,
            purchase_intent_level = EXCLUDED.purchase_intent_level,
            purchase_intent_label = EXCLUDED.purchase_intent_label,
            intent_confidence     = EXCLUDED.intent_confidence;
    """
    values = [(r["comment_id"], r["is_purchase_intent"],
               r["purchase_intent_level"], r["purchase_intent_label"],
               r["intent_confidence"]) for r in db_rows]
    conn = psycopg2.connect(**cfg)
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, values)
        conn.commit()
        logger.info(f"DB UPSERT 완료: {len(values)}건")
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# 5. 피처 추출기
# ══════════════════════════════════════════════════════════════

class RuleTransformer(BaseEstimator, TransformerMixin):
    def fit(self, X, y=None): return self
    def transform(self, X):   # X: list of text strings
        arr = np.array([_rule_features(t) for t in X], dtype=np.float32)
        return sp.csr_matrix(arr)


class CommentFeatureExtractor(BaseEstimator, TransformerMixin):
    """
    DataFrame → sparse feature matrix

    ① TF-IDF char n-gram (2~4)  cleaned_text
    ② TF-IDF word n-gram (1~2)  tokens
    ③ 규칙 피처 (시간성 포함)
    """
    def __init__(self, max_char=3000, max_word=3000):
        self.max_char = max_char
        self.max_word = max_word
        self.tfidf_char = TfidfVectorizer(
            analyzer="char_wb", ngram_range=(2, 4),
            max_features=max_char, sublinear_tf=True, min_df=1,
        )
        self.tfidf_word = TfidfVectorizer(
            analyzer="word", ngram_range=(1, 2),
            max_features=max_word, sublinear_tf=True, min_df=1,
        )
        self.rule = RuleTransformer()

    def _col(self, X, col):
        return X[col].fillna("").astype(str).tolist()

    def fit(self, X, y=None):
        self.tfidf_char.fit(self._col(X, "cleaned_text"))
        self.tfidf_word.fit(self._col(X, "tokens"))
        self.rule.fit(self._col(X, "original_text"))
        return self

    def transform(self, X):
        return sp.hstack([
            self.tfidf_char.transform(self._col(X, "cleaned_text")),
            self.tfidf_word.transform(self._col(X, "tokens")),
            self.rule.transform(self._col(X, "original_text")),
        ], format="csr")

    def feature_names(self):
        return (
            [f"char_{n}" for n in self.tfidf_char.get_feature_names_out()]
            + [f"word_{n}" for n in self.tfidf_word.get_feature_names_out()]
            + RULE_FEAT_NAMES
        )


# ══════════════════════════════════════════════════════════════
# 6. 메인 모델 클래스
# ══════════════════════════════════════════════════════════════

class PurchaseIntentModel:
    """구매전환의도 4단계 분류 (NONE/L1/L2/L3)"""

    def __init__(self):
        self.extractor  = CommentFeatureExtractor()
        self.classifier = GradientBoostingClassifier(
            n_estimators=300, max_depth=4,
            learning_rate=0.08, subsample=0.8,
            min_samples_leaf=2, random_state=42,
        )
        self._trained = False

    def train(self, df: pd.DataFrame,
              label_col: str = "purchase_label",
              cv_folds: int = 5) -> dict:

        df = df[~df["is_spam"].astype(bool)].copy()
        df = df[~df["is_empty"].astype(bool)].copy()
        X  = df.drop(columns=[label_col])
        y  = df[label_col].astype(int).values

        print(f"\n{'─'*55}")
        print(f"  학습 데이터: {len(df)}건")
        print("  레이블 분포:")
        for k, cnt in sorted(Counter(y).items()):
            pct = cnt / len(y) * 100
            print(f"    {INT_TO_STR[k]:<10} {cnt:>4}건  {pct:5.1f}%  {'█'*int(pct/2.5)}")
        print(f"{'─'*55}")

        self.extractor.fit(X)
        X_feat = self.extractor.transform(X)
        print(f"\n  피처 수: {X_feat.shape[1]}개")

        cv  = StratifiedKFold(n_splits=cv_folds, shuffle=True, random_state=42)
        res = cross_validate(self.classifier, X_feat, y,
                             cv=cv, scoring=["accuracy", "f1_macro"], n_jobs=-1)
        acc, f1 = res["test_accuracy"], res["test_f1_macro"]
        print(f"\n  [{cv_folds}-Fold CV]")
        print(f"  Accuracy  {acc.mean():.4f} ± {acc.std():.4f}  최고: {acc.max():.4f}")
        print(f"  F1-macro  {f1.mean():.4f} ± {f1.std():.4f}  최고: {f1.max():.4f}")

        self.classifier.fit(X_feat, y)
        self._trained = True

        y_pred = self.classifier.predict(X_feat)
        print(f"\n  Train Accuracy: {accuracy_score(y, y_pred):.4f}\n")
        print(classification_report(
            y, y_pred,
            target_names=[INT_TO_STR[i] for i in range(N_CLASSES)], digits=4,
        ))
        self._print_cm(y, y_pred)
        return {"cv_acc": round(acc.mean(), 4), "cv_f1": round(f1.mean(), 4)}

    def tune(self, df: pd.DataFrame, label_col="purchase_label") -> dict:
        df = df[~df["is_spam"].astype(bool)].copy()
        X  = df.drop(columns=[label_col])
        y  = df[label_col].astype(int).values
        self.extractor.fit(X)
        X_feat = self.extractor.transform(X)
        param_grid = {
            "n_estimators":  [200, 300, 400],
            "max_depth":     [3, 4, 5],
            "learning_rate": [0.05, 0.08, 0.1],
            "subsample":     [0.7, 0.8],
        }
        cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
        gs = GridSearchCV(GradientBoostingClassifier(random_state=42),
                          param_grid, cv=cv, scoring="f1_macro", n_jobs=-1, verbose=1)
        print("\n  [GridSearchCV 실행 중...]")
        gs.fit(X_feat, y)
        best = gs.best_params_
        print(f"  최적: {best}  F1: {gs.best_score_:.4f}")
        self.classifier = GradientBoostingClassifier(**best, random_state=42)
        return best

    def predict_df(self, df: pd.DataFrame) -> pd.DataFrame:
        assert self._trained
        X_feat = self.extractor.transform(df)
        preds  = self.classifier.predict(X_feat)
        probas = self.classifier.predict_proba(X_feat)

        out = pd.DataFrame()
        if "comment_id" in df.columns:
            out["comment_id"] = df["comment_id"].values

        out["purchase_intent_label"] = [INT_TO_STR[p] for p in preds]
        out["purchase_intent_level"] = preds.astype(int)
        out["is_purchase_intent"]    = (preds >= 2)
        out["intent_confidence"]     = probas.max(axis=1).round(4)
        for i in range(N_CLASSES):
            out[f"prob_{INT_TO_STR[i]}"] = probas[:, i].round(4)
        return out

    def predict_one(self, row: dict) -> dict:
        for col in ["cleaned_text", "original_text", "tokens", "nouns", "keywords"]:
            if col not in row:
                row[col] = ""
        row.setdefault("is_spam",  False)
        row.setdefault("is_empty", False)
        return self.predict_df(pd.DataFrame([row])).iloc[0].to_dict()

    def predict_for_db(self, df: pd.DataFrame) -> list[dict]:
        pred = self.predict_df(df)
        return [
            {
                "comment_id":            str(r.get("comment_id", "")),
                "is_purchase_intent":    bool(r["is_purchase_intent"]),
                "purchase_intent_level": int(r["purchase_intent_level"]),
                "purchase_intent_label": str(r["purchase_intent_label"]),
                "intent_confidence":     float(r["intent_confidence"]),
            }
            for _, r in pred.iterrows()
        ]

    def print_feature_importance(self, top_n=20):
        names = self.extractor.feature_names()
        imps  = self.classifier.feature_importances_
        idxs  = np.argsort(imps)[::-1][:top_n]
        print(f"\n  [피처 중요도 TOP {top_n}]")
        print(f"  {'순위':>4}  {'피처명':<40}  {'중요도':>8}")
        print("  " + "─" * 57)
        for rank, idx in enumerate(idxs, 1):
            name = names[idx] if idx < len(names) else f"feat_{idx}"
            print(f"  {rank:>4}. {name:<40}  {imps[idx]:.6f}  {'▮'*int(imps[idx]*500)}")

    def save(self, path: Path = MODEL_PATH):
        with open(path, "wb") as f:
            pickle.dump({"extractor": self.extractor,
                         "classifier": self.classifier,
                         "_trained": self._trained}, f)
        print(f"\n  [저장] {path}")

    @classmethod
    def load(cls, path: Path = MODEL_PATH) -> "PurchaseIntentModel":
        with open(path, "rb") as f:
            state = pickle.load(f)
        m = cls.__new__(cls)
        m.extractor  = state["extractor"]
        m.classifier = state["classifier"]
        m._trained   = state["_trained"]
        print(f"  [로드] {path}")
        return m

    def _print_cm(self, y_true, y_pred):
        cm  = confusion_matrix(y_true, y_pred)
        lbs = [INT_TO_STR[i] for i in range(N_CLASSES)]
        w   = 10
        print(f"  [혼동 행렬]")
        print("  " + " " * 10 + "".join(f"{l:>{w}}" for l in lbs))
        print("  " + "─" * (10 + w * N_CLASSES))
        for i, row in enumerate(cm):
            print("  " + f"{lbs[i]:>10}" + "".join(f"{v:>{w}}" for v in row))
        print()


# ══════════════════════════════════════════════════════════════
# 7. lumiq_analyzer.py 통합 인터페이스
# ══════════════════════════════════════════════════════════════

_GLOBAL_MODEL: Optional[PurchaseIntentModel] = None


def get_model(path: Path = MODEL_PATH) -> PurchaseIntentModel:
    """싱글턴 모델 로드 — Azure Function / lumiq_analyzer.py에서 사용"""
    global _GLOBAL_MODEL
    if _GLOBAL_MODEL is None:
        _GLOBAL_MODEL = PurchaseIntentModel.load(path)
    return _GLOBAL_MODEL


def classify_comment(model: PurchaseIntentModel,
                     text: str,
                     comment_id: str = "") -> dict:
    """
    단순 텍스트 입력으로 분류. lumiq_analyzer.py 연동용.

    사용 예:
        from purchase_intent_model_v4 import get_model, classify_comment
        model = get_model()
        result = classify_comment(model, "선크림 바로 결제했어요!", "yt_abc123")
    """
    row = _make_row(comment_id, text)
    return model.predict_one(row)


# ══════════════════════════════════════════════════════════════
# 8. 실행부
# ══════════════════════════════════════════════════════════════

def run_train(dummy_csv:  Optional[str] = None,
              real_csv:   Optional[str] = None,
              use_db:     bool = False,
              tune:       bool = False,
              env_path:   Optional[str] = None) -> PurchaseIntentModel:

    print("\n" + "═"*55)
    print("  LUMIQ 구매전환의도 분류 모델 v4  학습")
    print("  (엔티티 마스킹 + 시간성 분리 적용)")
    print("═"*55)

    frames = []

    if dummy_csv:
        frames.append(load_dummy_csv(dummy_csv))
    if real_csv:
        frames.append(load_real_csv(real_csv))
    if use_db:
        frames.append(load_from_db(env_path=env_path))

    if not frames:
        raise ValueError("학습 데이터가 없습니다. --dummy-csv 또는 --real-csv 를 지정하세요.")

    df = pd.concat(frames, ignore_index=True)
    df = df.sample(frac=1, random_state=42).reset_index(drop=True)
    print(f"\n  합계: {len(df)}건")

    model = PurchaseIntentModel()

    if tune:
        print("\n[하이퍼파라미터 튜닝]")
        best = model.tune(df)
        print(f"  최적 파라미터: {best}")

    metrics = model.train(df)
    model.print_feature_importance(top_n=20)
    model.save()

    print(f"\n{'═'*55}")
    print(f"  완료  CV Acc: {metrics['cv_acc']:.4f}  F1: {metrics['cv_f1']:.4f}")
    print(f"{'═'*55}\n")
    return model


def run_predict(model: Optional[PurchaseIntentModel] = None):
    if model is None:
        model = PurchaseIntentModel.load()

    # 레벨·엔티티·시간성을 모두 커버하는 테스트 케이스
    test_cases = [
        # (text, expected, 설명)
        ("선크림 영상 보자마자 결제했어요!",      "L3", "본상품 즉시 전환"),
        ("언니 믿고 바로 질러버림ㅋㅋ",          "L3", "즉시 전환"),
        ("지냐믿고질러본다.",                     "L3", "즉시 전환 짧은 표현"),
        ("이 마켓 또 언제 하시나요?",             "L3", "마켓 재진행 희망"),
        ("마켓 언제 또 열어요 기다릴게요",        "L3", "대기 수요"),
        ("세럼 써봤는데 너무 맘에 들어요",        "L1",      "타상품 칭찬 → L1"),
        ("선크림이랑 세럼 가격 어떻게 돼요?",     "L2",      "탐색/고민"),
        ("수부지가 써도 되나요?",                 "L2",      "적합성 문의"),
        ("피부 진짜 좋아보인다",                  "L1",      "단순 칭찬"),
        ("영상 잘 봤습니다",                      "L1",      "영상 칭찬"),
        ("ㅋㅋㅋㅋ",                             "NONE",    "노이즈"),
        ("1빠",                                   "NONE",    "스팸"),
        # 타겟의존성 핵심 케이스
        ("헉 세럼 배송 왔는데 너무 마음에 들어요", "L1",    "타상품 구매 → L1 (타겟의존성)"),
        ("세럼이랑 선크림 다 써서 사야됐는데 마켓이 열리다니", "L3", "본상품 포함 구매"),
    ]

    print("\n" + "═"*78)
    print("  예측 결과 (엔티티 마스킹 + 시간성 검증)")
    print("═"*78)
    print(f"  {'예측':<10} {'정답':<10} {'일치':>4}  {'신뢰도':>6}  댓글")
    print("  " + "─"*74)

    correct = 0
    for text, expected, desc in test_cases:
        row    = _make_row("t", text)
        result = model.predict_one(row)
        pred   = result["purchase_intent_label"]
        conf   = result["intent_confidence"]
        match  = "✓" if pred == expected else "✗"
        if pred == expected:
            correct += 1
        print(f"  {pred:<10} {expected:<10} {match:>4}  {conf:>6.1%}  {text[:35]}")

    print(f"\n  샘플 정확도: {correct}/{len(test_cases)} ({correct/len(test_cases):.1%})")

    print(f"\n  [DB INSERT 포맷]")
    rows   = [_make_row(f"t{i}", t) for i, (t, _, __) in enumerate(test_cases[:3])]
    df_t   = pd.DataFrame(rows)
    for r in model.predict_for_db(df_t):
        print(f"  {json.dumps(r, ensure_ascii=False)}")

    print(OUTPUT_SCHEMA)


OUTPUT_SCHEMA = """
╔══════════════════════════════════════════════════════════════════╗
║  LUMIQ 구매전환의도 v4 — 아웃풋 데이터 정의서                  ║
╠══════════════════════════════════════════════════════════════════╣
║  대상 테이블: project.comments_analysis                         ║
╠══════════════════╦═════════╦══════════╦═════════════════════════╣
║  comment_id      ║ VARCHAR ║ NOT NULL ║ 댓글 고유 ID            ║
║  intent_label    ║ VARCHAR ║ NOT NULL ║ NONE/L1/L2/L3            ║
║  intent_level    ║ INT     ║ NOT NULL ║ 0/1/2/3                 ║
║  is_purchase     ║ BOOLEAN ║ NOT NULL ║ level>=2 → TRUE         ║
║  confidence      ║ FLOAT   ║ NOT NULL ║ 0.0~1.0                 ║
╠══════════════════════════════════════════════════════════════════╣
║  NONE (0) 의미없음/스팸                                         ║
║  L1   (1) 단순 긍정/칭찬 (타상품 구매 포함)                     ║
║  L2   (2) 탐색/고민                   is_purchase=TRUE          ║
║  L3   (3) 구매/전환 (즉시+대기 모두)  is_purchase=TRUE          ║
╚══════════════════════════════════════════════════════════════════╝
"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LUMIQ 구매전환의도 모델 v4")
    parser.add_argument("--dummy-csv",  default=None, help="더미 CSV 경로")
    parser.add_argument("--real-csv",   default=None, help="수동 라벨링 CSV 경로")
    parser.add_argument("--use-db",     action="store_true")
    parser.add_argument("--tune",       action="store_true")
    parser.add_argument("--env-path",   default=None)
    parser.add_argument("--predict",    action="store_true")
    args = parser.parse_args()

    if args.predict:
        run_predict()
    else:
        trained = run_train(
            dummy_csv=args.dummy_csv,
            real_csv=args.real_csv,
            use_db=args.use_db,
            tune=args.tune,
            env_path=args.env_path,
        )
        run_predict(model=trained)
