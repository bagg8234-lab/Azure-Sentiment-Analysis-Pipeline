import logging
import json
import os
import psycopg2
import textwrap
from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()

openai_endpoint = os.environ["OPENAI_ENDPOINT"]
openai_key = os.environ["OPENAI_KEY"]
openai_api_version = os.environ["OPENAI_API_VERSION"]
openai_gpt_model = os.environ["OPENAI_GPT_MODEL"]
openai_embeddings_deployment = os.environ.get("OPENAI_EMBEDDINGS_DEPLOYMENT", "")

PG_HOST     = os.environ.get("DB_HOST")
PG_DATABASE = os.environ.get("DB_NAME")
PG_USER     = os.environ.get("DB_USER")
PG_PASSWORD = os.environ.get("DB_PASSWORD")
PG_PORT     = os.environ.get("DB_PORT", "5432")

openai_client = AzureOpenAI(
    azure_endpoint=openai_endpoint,
    api_key=openai_key,
    api_version=openai_api_version
)

# get_pg_connection — DB 연결 후 search_path를 project 스키마로 고정
def get_pg_connection():
    conn = psycopg2.connect(
        host=PG_HOST, database=PG_DATABASE, user=PG_USER, password=PG_PASSWORD, port=PG_PORT
    )
    # 접속 후 기본 경로를 project 스키마로 고정
    cur = conn.cursor()
    cur.execute("SET search_path TO project, public")
    cur.close()
    return conn


# 1. text_to_sql — 사용자 질문을 PostgreSQL 쿼리로 변환
def text_to_sql(user_query):
    schema_info = textwrap.dedent("""
    [데이터베이스 스키마 정의 (PostgreSQL)]
    - 모든 테이블은 'project' 스키마에 소속되어 있습니다.
    - 쿼리 작성 시 반드시 테이블명 앞에 'project.'를 붙이세요. (예: project.products)
    
    1. project.products: product_id(PK), product_name, brand_name
    2. project.videos: video_id(PK), product_id(FK), title, view_count, is_sponsored
    3. project.video_summary: video_id(PK/FK), total_comment_threads, total_replies, total_all
    4. project.comments: comment_id(PK), video_id(FK), text, likes
    5. project.comment_cleansed: comment_id(PK/FK), original_text, cleaned_text, is_spam, mentioned_brands
    6. project.comments_analysis: comment_id(PK/FK), sentiment_score, crisis_flag
    7. project.platform_reviews: review_id(PK), platform, product_id(FK), rating, review_text
    8. project.platform_review_cleansed: review_id(PK/FK), cleaned_text, keywords, is_spam
    9. project.platform_reviews_analysis: review_id(PK/FK), sentiment_score, sentiment_label, crisis_flag, analyzed_at, attribute_tags, sentiment_pos, sentiment_neg
    10. project.video_metrics: video_id(PK/FK), positive_ratio, avg_sentiment, conversion_index
    11. project.product_metrics: product_id(PK/FK), overall_score, grade
    12. project.reports: report_id(PK), product_id(FK), report_path
    """)

    dynamic_examples_text = textwrap.dedent("""
    [참고 퓨샷(Few-shot) 예시]
    # 예시 1
    질문: 올리브영에서 별점이 3점 이하인 리뷰 원문과 해당 상품의 이름을 알려줘
    응답:
    {
        "reasoning": "project.platform_reviews와 project.products 테이블을 JOIN하여 조건에 맞는 리뷰 텍스트와 상품명을 조회합니다.",
        "sql": "SELECT pr.review_text, pr.rating, p.product_name FROM project.platform_reviews pr JOIN project.products p ON pr.product_id = p.product_id WHERE pr.platform = %s AND pr.rating <= %s LIMIT 5",
        "parameters": ["oliveyoung", 3.0]
    }

    # 예시 2
    질문: 최근 영상 중에서 위기 징후(crisis_flag)가 있는 부정적인 댓글 내용 좀 뽑아줘
    응답:
    {
        "reasoning": "project.comments 테이블과 project.comments_analysis 테이블을 JOIN하여 crisis_flag가 TRUE인 댓글을 필터링합니다.",
        "sql": "SELECT c.text, ca.sentiment_score FROM project.comments c JOIN project.comments_analysis ca ON c.comment_id = ca.comment_id WHERE ca.crisis_flag = %s ORDER BY ca.sentiment_score ASC LIMIT 5",
        "parameters": [true]
    }
    """)

    messages = [
        {"role": "system", "content": textwrap.dedent(f"""
            당신은 데이터 분석가이자 PostgreSQL 데이터베이스 전문가입니다.
            제공된 스키마를 바탕으로 사용자의 질문에 답할 수 있는 SQL 쿼리를 작성하세요.
            
            {schema_info}
            
            [데이터베이스 쿼리 작성 규칙 및 제약사항]
            1. 문법 최적화: 반드시 PostgreSQL 표준 문법을 사용하세요.
            2. 보안 최적화(SQL Injection 방어): 절대 WHERE 절 내에 검색 대상 리터럴 값을 직접 하드코딩하지 마세요. 사용자의 입력값은 무조건 '%s' 기호로 치환하여 `parameters` 배열에 순서대로 분리해야 합니다.
            3. 결과 제한: 애플리케이션 과부하를 막기 위해 항상 쿼리 끝에 'LIMIT 5'를 추가하세요.
            
            출력 형식 (JSON):
            {{
                "reasoning": "조건식 도출 과정 및 테이블 JOIN 논리",
                "sql": "작성된 매개변수화된 SELECT 쿼리",
                "parameters": ["매핑할", "문자열", "혹은", "숫자"]
            }}
            
            {dynamic_examples_text}
        """)},
        {"role": "user", "content": user_query}
    ]

    response = openai_client.chat.completions.create(
        model=openai_gpt_model,
        messages=messages,
        temperature=0.1
    )

    return response.choices[0].message.content


# 2. query_similar_data — Text-to-SQL 실행 후 DB 결과 반환
def query_similar_data(user_query):
    logging.info("LLM을 사용하여 PostgreSQL 쿼리를 생성합니다.\n- 사용자 질문: %s", user_query)

    # Text-to-SQL 호출
    query_json_str = text_to_sql(user_query)
    logging.info(f"LLM 응답 결과: {query_json_str}")

    try:
        # 마크다운 코드블록 제거 후 파싱
        clean_str = query_json_str.strip()
        clean_str = clean_str.replace("```json", "").replace("```", "").strip()
        
        parsed_data = json.loads(clean_str)
        sql_query = parsed_data.get("sql")
        parameters = tuple(parsed_data.get("parameters", []))
    except json.JSONDecodeError:
        logging.error("SQL JSON 파싱 오류")
        return []

    logging.info(f"실행할 SQL문: {sql_query} | 파라미터: {parameters}")
    
    # PostgreSQL에서 쿼리 실행
    results = []
    try:
        conn = get_pg_connection()
        cursor = conn.cursor()
        cursor.execute(sql_query, parameters)
        rows = cursor.fetchall()
        
        # 컬럼명 추출
        col_names = [desc[0] for desc in cursor.description]
        
        # 결과를 딕셔너리 리스트로 변환
        for row in rows:
            row_dict = dict(zip(col_names, row))
            # AI가 읽기 편하게 하나의 텍스트로 합침
            row_text = " | ".join([f"{k}: {v}" for k, v in row_dict.items()])
            results.append({"textRepresentation": row_text})
            
    except Exception as e:
        logging.error(f"쿼리 실행 에러: {str(e)}")
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals(): conn.close()
    
    return results


# 3. generate_response — 검색 결과를 컨텍스트로 최종 답변 생성
def generate_response(user_query):
    # 1. 관련 데이터 검색 (Retrieval)
    context_data = query_similar_data(user_query)
    
    if not context_data:
        return "데이터베이스에서 관련 정보를 찾을 수 없거나, 쿼리 실행 중 오류가 발생했습니다."

    # 2. 컨텍스트 구성
    context = "다음은 질문과 관련된 데이터베이스 조회 결과입니다:\n\n"
    for i, data in enumerate(context_data, 1):
        context += f"데이터 {i}: {data['textRepresentation']}\n"
    
    # 3. OpenAI 모델을 사용하여 최종 응답 생성
    messages = [
        {"role": "system", "content": """당신은 브랜드 및 리뷰 데이터 분석 전문가입니다.
        제공된 데이터를 바탕으로 정확하고 유익한 정보를 아래 형식에 맞춰 간결하게 답변하세요.

        [답변 규칙]
        1. 3~5줄 이내로 핵심만 요약
        2. 수치가 있으면 반드시 포함
        3. 불필요한 추측이나 장황한 설명 금지
        4. 마지막 줄에 한 줄 결론으로 마무리

        [답변 형식 예시]
        📊 분석 결과
        - 핵심 내용 1 (수치 포함)
        - 핵심 내용 2 (수치 포함)

        💡 결론: 한 줄 요약
        """},        
        {"role": "user", "content": f"질문: {user_query}\n\n컨텍스트:\n{context}"}
    ]
    
    response = openai_client.chat.completions.create(
        model=openai_gpt_model,
        messages=messages,
        temperature=0.3 
    )
    
    return response.choices[0].message.content