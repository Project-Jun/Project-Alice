# services/search_service.py - 검색 서비스 / Search Service

from ddgs import DDGS
import requests
import re
import os
from dotenv import load_dotenv

load_dotenv()

# 환경변수 / Environment variables
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
GOOGLE_CSE_ID = os.getenv('GOOGLE_CSE_ID')
NAVER_CLIENT_ID = os.getenv('NAVER_CLIENT_ID')
NAVER_CLIENT_SECRET = os.getenv('NAVER_CLIENT_SECRET')


# HTML 태그 제거 / Remove HTML tags
def clean_html(text):
    return re.sub(r'<[^>]+>', '', text)


# DuckDuckGo 검색 / DuckDuckGo search
def search_duckduckgo(query, max_results=5):
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return [{'title': r['title'], 'url': r['href'], 'snippet': r['body']} for r in results]
    except Exception as e:
        return [{'error': f'DuckDuckGo 검색 실패: {str(e)}'}]


# Google 검색 (현재 비활성화) / Google search (currently disabled)
def search_google(query, max_results=5):
    return [{'error': 'Google 검색은 현재 비활성화 상태예요.'}]


# Naver 검색 / Naver search
def search_naver(query, max_results=5):
    try:
        url = 'https://openapi.naver.com/v1/search/webkr.json'
        headers = {
            'X-Naver-Client-Id': NAVER_CLIENT_ID,
            'X-Naver-Client-Secret': NAVER_CLIENT_SECRET
        }
        params = {'query': query, 'display': max_results}
        response = requests.get(url, headers=headers, params=params)
        data = response.json()
        items = data.get('items', [])
        return [{'title': i['title'], 'url': i['link'], 'snippet': i.get('description', '')} for i in items]
    except Exception as e:
        return [{'error': f'Naver 검색 실패: {str(e)}'}]


# 검색 엔진 선택 및 실행 / Select and run search engine
def search(query, engine='duckduckgo'):
    if engine == 'google':
        return search_google(query)
    elif engine == 'naver':
        return search_naver(query)
    else:
        return search_duckduckgo(query)


# 검색 핸들러 / Handle search request
def handle_search(chat, ollama_client, user_input):
    # 검색 엔진 선택 / Select search engine
    if '구글' in user_input:
        engine = 'duckduckgo'  # 구글 비활성화 상태 / Google disabled
    elif '네이버' in user_input:
        engine = 'naver'
    else:
        engine = 'duckduckgo'

    # 검색어 추출 / Extract search query
    query_prompt = (
        f"Extract only the search keyword from: \"{user_input}\"\n"
        f"Remove action words like '검색해줘', '찾아봐', '알려줘', '네이버로', '네이버에서', '구글로', '구글에서'\n"
        f"Respond with ONLY the keyword, nothing else."
    )
    search_query = chat.send_message(query_prompt).text.strip()
    print(f"[검색 엔진: {engine.upper()} / 검색어: {search_query}]")

    results = search(search_query, engine=engine)
    if not results or 'error' in results[0]:
        return "검색 결과를 찾지 못했어요. 다시 시도해 주세요.", []

    # 검색 결과 포맷 / Format results
    output = f"🔍 {engine.upper()} 검색 결과\n\n"
    for i, r in enumerate(results, 1):
        output += f"{i}. {clean_html(r['title'])}\n"
        output += f"   {clean_html(r['snippet'])}\n"
        output += f"   🔗 {r['url']}\n\n"

    # Ollama로 요약 / Summarize with Ollama
    summary_prompt = (
        f"다음은 '{search_query}'에 대한 검색 결과야:\n{output}\n\n"
        f"검색 결과를 바탕으로 한국어로 간결하게 요약해줘. 목록도 함께 보여줘."
    )
    response = ollama_client.chat(
        model='gemma3:4b',
        messages=[
            {'role': 'system', 'content': "You are Alice, a personal AI assistant. Always respond in Korean. Never use Chinese."},
            {'role': 'user', 'content': summary_prompt}
        ]
    )
    # 결과 텍스트와 원본 결과 목록 반환 / Return summary and raw results for follow-up
    return response.message.content, results


# 후속 질문 핸들러 / Handle follow-up question
def handle_followup(ollama_client, user_input, last_search_results):
    context = "이전 검색 결과:\n"
    for i, r in enumerate(last_search_results, 1):
        context += f"{i}. {clean_html(r['title'])}: {clean_html(r['snippet'])} ({r['url']})\n"
    context += "\n"

    response = ollama_client.chat(
        model='gemma3:4b',
        messages=[
            {'role': 'system', 'content': "You are Alice, a personal AI assistant. Always respond in Korean. Never use Chinese."},
            {'role': 'user', 'content': f"{context}사용자 질문: {user_input}"}
        ]
    )
    return response.message.content
