# Alice.py - AI 개인 비서 / AI Personal Assistant

# venv 실행이 안되어 있을때 사용 / If venv is not activated
# cd ai_assistant
# .\venv\Scripts\Activate.ps1

# 라이브러리 불러오기 / Import Libraries

from google import genai
from google.genai import types
from dotenv import load_dotenv
from datetime import datetime
import os
import re
import ollama as ollama_client

# 기능 모듈 불러오기 / Import Feature Modules
from gmail_service import get_recent_emails
# 캘린더 연동 모듈
from calendar_service import get_upcoming_events, add_event, delete_event, get_recent_event_id
# 검색 모듈
from search_service import search

# 초기 설정 / Initial Setup

# 환경변수 로드 (.env 파일에서 API 키 읽기) / Load environment variables
load_dotenv()

# Gemini 클라이언트 생성 / Create Gemini client
client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

# Alice 채팅 세션 시작 / Start Alice chat session
chat = client.chats.create(
    model='gemini-2.5-flash',
    config=types.GenerateContentConfig(
        system_instruction=(
            "Your name is Alice. You are a personal AI assistant. "
            "Always respond in Korean, kindly and concisely. "
            "Always refer to yourself as Alice."
        )
    )
)

# 상태 저장 변수 / State Variables
last_added_event = {'id': None, 'title': None}  # 마지막 추가 일정
last_search_results = []                        # 마지막 검색 결과

# 유틸리티 함수 / Utility Functions

# 오늘 날짜와 요일 반환 / Return today's date and weekday
def get_today_info():
    now = datetime.now()
    weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    weekdays_kr = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']
    return {
        'date': now.strftime('%Y-%m-%d'),
        'weekday_en': weekdays[now.weekday()],
        'weekday_kr': weekdays_kr[now.weekday()],
        'full': f"{now.strftime('%Y-%m-%d')} ({weekdays[now.weekday()]})"
    }

# HTML 태그 제거 / Remove HTML tags
def clean_html(text):
    return re.sub(r'<[^>]+>', '', text)

# 이메일 처리기 / Email Handler

# 이메일 요약 요청 처리 / Handle email summary requests
def handle_email(user_input):
    emails = get_recent_emails()

    # 이메일 데이터를 텍스트로 변환 / Convert email data to text
    email_text = "\n".join([
        f"- From: {e['sender']}, Subject: {e['subject']}, Preview: {e['snippet']}"
        for e in emails
    ])
    prompt = (
        f"Here are the recent emails:\n{email_text}\n\n"
        f"User request (respond in Korean): {user_input}"
    )
    response = chat.send_message(prompt)
    return response.text

# 캘린더 처리기 / Calendar Handlers

# 일정 조회 / View calendar events
def handle_calendar_view(user_input, today):
    events = get_upcoming_events()
    event_text = "\n".join([
        f"- Title: {e['title']}, Start: {e['start']}"
        for e in events
    ]) if events else "No upcoming events"

    prompt = (
        f"Today is {today['full']}.\n"
        f"Here are the upcoming events for the next 7 days:\n{event_text}\n\n"
        f"User request (respond in Korean): {user_input}"
    )
    response = chat.send_message(prompt)
    return response.text

# 일정 추가 / Add calendar event
def handle_calendar_add(user_input, today):
    prompt = (
        f"Today is {today['full']} ({today['weekday_en']}).\n"
        f"The user wants to add a calendar event. User input: \"{user_input}\"\n\n"
        f"Date calculation rules:\n"
        f"- 'this Monday' = the Monday of the current week\n"
        f"- 'next Monday' = the Monday of NEXT week (7+ days from today)\n"
        f"- If no time given, use 09:00:00. End time = start + 1 hour.\n\n"
        f"Respond ONLY in this exact format, no other text whatsoever:\n"
        f"TITLE: event title\n"
        f"START: YYYY-MM-DDTHH:MM:SS\n"
        f"END: YYYY-MM-DDTHH:MM:SS\n"
        f"DESC: description (empty if none)\n"
    )
    response = chat.send_message(prompt)

    # 응답을 딕셔너리로 변환 / Response into dictionary
    lines = response.text.strip().split('\n')
    info = {}
    for line in lines:
        if ':' in line:
            key, value = line.split(':', 1)
            info[key.strip()] = value.strip()

    # 필수 정보 확인 / Validate required fields
    title = info.get('TITLE', '새 일정')
    start = info.get('START')
    end = info.get('END')

    if not start or not end:
        return "일정 시간 정보를 인식하지 못했어요. 날짜와 시간을 좀 더 구체적으로 말씀해 주세요."

    try:
        # 캘린더에 일정 추가 / Add event to calendar
        link, event_id = add_event(
            title=title,
            start_datetime=start,
            end_datetime=end,
            description=info.get('DESC', '')
        )

        # 마지막 추가 일정 저장 / Save last added event
        last_added_event['id'] = event_id
        last_added_event['title'] = title
        return (
            f"일정을 추가했어요! 📅\n"
            f"제목: {title}\n"
            f"시작: {start}\n"
            f"캘린더에서 확인: {link}"
        )
    except Exception as e:
        return f"일정 추가 중 오류가 발생했어요: {str(e)}"

# 일정 삭제 / Delete calendar event
def handle_calendar_delete(user_input):
    # "방금", "아까" 등 표현이면 마지막 일정 바로 삭제
    if any(word in user_input for word in ['방금', '아까', '조금전', '바로']):
        if last_added_event['id']:
            delete_event(last_added_event['id'])
            title = last_added_event['title']
            last_added_event['id'] = None
            last_added_event['title'] = None
            return f"'{title}' 일정을 삭제했어요! 🗑️"

    # 키워드와 날짜 추출 / Extract keyword and date
    prompt = (
        f"Today is {get_today_info()['full']}.\n"
        f"The user wants to delete a calendar event. User input: \"{user_input}\"\n"
        f"Extract the event title keyword AND the date if mentioned.\n"
        f"Do NOT include words like 'delete', 'cancel', 'event', 'schedule'.\n"
        f"For example: '3월 3일 김대균 토익 일정 삭제해줘' → KEYWORD: 김대균 토익\n DATE: 2026-03-03\n"
        f"If no date mentioned, leave DATE empty.\n"
        f"Respond ONLY in this exact format:\n"
        f"KEYWORD: keyword\n"
        f"DATE: YYYY-MM-DD\n"
    )
    response = chat.send_message(prompt)

    lines = response.text.strip().split('\n')
    info = {}
    for line in lines:
        if ':' in line:
            key, value = line.split(':', 1)
            info[key.strip()] = value.strip()

    keyword = info.get('KEYWORD', '')
    date = info.get('DATE', '').strip()

    # 키워드와 날짜로 일정 검색 / Search event by keyword and date
    event_id, event_title = get_recent_event_id(keyword, date if date else None)

    if event_id:
        delete_event(event_id)
        return f"'{event_title}' 일정을 삭제했어요! "
    else:
        return f"'{keyword}' 관련 일정을 찾지 못했어요. 일정 이름을 좀 더 구체적으로 말씀해 주세요."

# 검색 핸들러 / Search Handler

def handle_search(user_input):
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

    results = search(search_query, engine=engine)
    if not results or 'error' in results[0]:
        return "검색 결과를 찾지 못했어요. 다시 시도해 주세요."

    # 검색 결과 저장 (후속 질문 대응) / Save for follow-up questions
    last_search_results.clear()
    last_search_results.extend(results)

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
        model='qwen2.5:7b',
        messages=[
            {'role': 'system', 'content': "You are Alice, a personal AI assistant. Always respond in Korean. Never use Chinese."},
            {'role': 'user', 'content': summary_prompt}
        ]
    )
    return response.message.content

# 메인 명령 처리기 / Main Command Processor

def process_command(user_input):
    today = get_today_info()

    # Gemini로 의도 분류 / Classify intent using Gemini
    context_hint = "이전 검색 결과 있음" if last_search_results else "없음"
    intent_prompt = (
        f"Classify the user's intent into ONE of these categories:\n"
        f"SEARCH - wants to search the web\n"
        f"CALENDAR_ADD - wants to add a calendar event\n"
        f"CALENDAR_DELETE - wants to delete a calendar event\n"
        f"CALENDAR_VIEW - wants to view calendar events\n"
        f"EMAIL - wants to check emails\n"
        f"FOLLOWUP - follow-up question about previous search results (context: {context_hint})\n"
        f"CHAT - general conversation\n\n"
        f"User input: \"{user_input}\"\n"
        f"Respond with ONLY the category name, nothing else."
    )
    intent = chat.send_message(intent_prompt).text.strip().upper()

    # 검색 처리 / Handle search
    if intent == 'SEARCH':
        return handle_search(user_input)

    elif intent == 'FOLLOWUP' and last_search_results:
        context = "이전 검색 결과:\n"
        for i, r in enumerate(last_search_results, 1):
            context += f"{i}. {clean_html(r['title'])}: {clean_html(r['snippet'])} ({r['url']})\n"
        context += "\n"
        response = ollama_client.chat(
            model='qwen2.5:7b',
            messages=[
                {'role': 'system', 'content': "You are Alice, a personal AI assistant. Always respond in Korean. Never use Chinese."},
                {'role': 'user', 'content': f"{context}사용자 질문: {user_input}"}
            ]
        )
        return response.message.content

    elif intent == 'EMAIL':
        return handle_email(user_input)

    elif intent == 'CALENDAR_DELETE':
        return handle_calendar_delete(user_input)

    elif intent == 'CALENDAR_ADD':
        return handle_calendar_add(user_input, today)

    elif intent == 'CALENDAR_VIEW':
        return handle_calendar_view(user_input, today)

    else:  # CHAT
        print("[Ollama 모델을 기반으로 처리 중...]")
        response = ollama_client.chat(
            model='qwen2.5:7b',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        "Your name is Alice. You are a personal AI assistant. "
                        "Always respond in Korean, kindly and concisely. Never use Chinese. "
                        "Always refer to yourself as Alice."
                    )
                },
                {'role': 'user', 'content': user_input}
            ]
        )
        return response.message.content

# 프로그램 실행 진입점 / Program Entry Point

if __name__ == '__main__':
    print("=" * 40)
    print("    안녕하세요! 저는 AI 비서 Alice예요.")
    print("    무엇을 도와드릴까요?")
    print("=" * 40)
    print("('종료' 입력시 종료)\n")

    while True:
        user_input = input("나: ").strip()

        if user_input == '종료':
            print("\nAlice: 또 필요한 게 있으면 불러주세요. 안녕히 계세요!")
            break

        if not user_input:
            continue

        print(f"\nAlice: {process_command(user_input)}\n")
