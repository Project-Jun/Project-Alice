# search_service.py - 웹 검색 서비스 / Web Search Service

# 필요한 라이브러리 불러오기 / Import required libraries
from ddgs import DDGS
import requests
import os
from dotenv import load_dotenv

load_dotenv()

# Google 검색 설정 / Google search config
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
GOOGLE_CSE_ID = os.getenv('GOOGLE_CSE_ID')

# Naver 검색 설정 / Naver search config
NAVER_CLIENT_ID = os.getenv('NAVER_CLIENT_ID')
NAVER_CLIENT_SECRET = os.getenv('NAVER_CLIENT_SECRET')

# DuckDuckGo 검색 / DuckDuckGo search (기본 검색 / default search)
def search_duckduckgo(query, max_results=5):
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return [{'title': r['title'], 'url': r['href'], 'snippet': r['body']} for r in results]
    except Exception as e:
        return [{'error': f'DuckDuckGo 검색 실패: {str(e)}'}]

# Google 검색 / Google search
def search_google(query, max_results=5):
    # Google Custom Search API 비활성화 / Google search disabled
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