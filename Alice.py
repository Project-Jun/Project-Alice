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
import ollama as ollama_client

# 서비스 모듈 불러오기 / Import Service Modules
from services.gmail_service import handle_email
from services.calendar_service import handle_calendar_view, handle_calendar_add, handle_calendar_delete
from services.search_service import handle_search, handle_followup

# 초기 설정 / Initial Setup

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
last_search_results = []                          # 마지막 검색 결과

# 유틸리티 함수 / Utility Functions

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

    if intent == 'SEARCH':
        print("[🔍 검색 모드 - Gemini 의도분류 + 검색 엔진 + Ollama 요약]")
        result, results = handle_search(chat, ollama_client, user_input)
        last_search_results.clear()
        last_search_results.extend(results)
        return result

    elif intent == 'FOLLOWUP' and last_search_results:
        print("[🔍 후속 질문 모드 - Ollama 처리 중...]")
        return handle_followup(ollama_client, user_input, last_search_results)

    elif intent == 'EMAIL':
        print("[📧 이메일 모드 - Gemini 처리 중...]")
        return handle_email(chat, user_input)

    elif intent == 'CALENDAR_DELETE':
        print("[📅 일정 삭제 모드 - Gemini 처리 중...]")
        return handle_calendar_delete(chat, user_input, last_added_event)

    elif intent == 'CALENDAR_ADD':
        print("[📅 일정 추가 모드 - Gemini 처리 중...]")
        return handle_calendar_add(chat, user_input, today, last_added_event)

    elif intent == 'CALENDAR_VIEW':
        print("[📅 일정 조회 모드 - Gemini 처리 중...]")
        return handle_calendar_view(chat, user_input, today)

    else:  # CHAT
        print("[Ollama 모델을 기반으로 처리 중...]")
        response = ollama_client.chat(
            model='gemma3:4b',
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
    print("    안녕하세요! AI 비서 Alice예요.")
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
