# services/calendar_service.py - 캘린더 서비스 / Calendar Service

from googleapiclient.discovery import build
from auth import get_credentials
from datetime import datetime, timedelta


# 캘린더 서비스 객체 생성 / Build Calendar service
def get_calendar_service():
    creds = get_credentials()
    return build('calendar', 'v3', credentials=creds)


# 다가오는 일정 가져오기 / Get upcoming events
def get_upcoming_events(days=7):
    service = get_calendar_service()
    now = datetime.utcnow()
    end = now + timedelta(days=days)

    events_result = service.events().list(
        calendarId='primary',
        timeMin=now.isoformat() + 'Z',
        timeMax=end.isoformat() + 'Z',
        singleEvents=True,
        orderBy='startTime'
    ).execute()

    events = events_result.get('items', [])
    result = []
    for event in events:
        start = event['start'].get('dateTime', event['start'].get('date'))
        result.append({
            'id': event['id'],
            'title': event.get('summary', '제목 없음'),
            'start': start,
            'description': event.get('description', '')
        })
    return result


# 일정 추가 / Add event
def add_event(title, start_datetime, end_datetime, description=''):
    service = get_calendar_service()
    event = {
        'summary': title,
        'description': description,
        'start': {'dateTime': start_datetime, 'timeZone': 'Asia/Seoul'},
        'end': {'dateTime': end_datetime, 'timeZone': 'Asia/Seoul'},
    }
    result = service.events().insert(calendarId='primary', body=event).execute()
    return result.get('htmlLink'), result.get('id')


# 일정 삭제 / Delete event
def delete_event(event_id):
    service = get_calendar_service()
    service.events().delete(calendarId='primary', eventId=event_id).execute()
    return True


# 키워드와 날짜로 일정 검색 / Search event by keyword and optional date
def get_recent_event_id(title_keyword, date=None):
    service = get_calendar_service()
    now = datetime.utcnow()
    past = now - timedelta(days=7)

    events_result = service.events().list(
        calendarId='primary',
        timeMin=past.isoformat() + 'Z',
        timeMax=(now + timedelta(days=30)).isoformat() + 'Z',
        singleEvents=True,
        orderBy='startTime'
    ).execute()

    events = events_result.get('items', [])
    for event in reversed(events):
        title_match = title_keyword.lower() in event.get('summary', '').lower()
        if date:
            event_start = event['start'].get('dateTime', event['start'].get('date', ''))
            date_match = date in event_start
            if title_match and date_match:
                return event['id'], event.get('summary')
        else:
            if title_match:
                return event['id'], event.get('summary')
    return None, None


# 일정 조회 핸들러 / Handle calendar view
def handle_calendar_view(chat, user_input, today):
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


# 일정 추가 핸들러 / Handle calendar add
def handle_calendar_add(chat, user_input, today, last_added_event):
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

    lines = response.text.strip().split('\n')
    info = {}
    for line in lines:
        if ':' in line:
            key, value = line.split(':', 1)
            info[key.strip()] = value.strip()

    title = info.get('TITLE', '새 일정')
    start = info.get('START')
    end = info.get('END')

    if not start or not end:
        return "일정 시간 정보를 인식하지 못했어요. 날짜와 시간을 좀 더 구체적으로 말씀해 주세요."

    try:
        link, event_id = add_event(
            title=title,
            start_datetime=start,
            end_datetime=end,
            description=info.get('DESC', '')
        )
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


# 일정 삭제 핸들러 / Handle calendar delete
def handle_calendar_delete(chat, user_input, last_added_event):
    # "방금", "아까" 등 표현이면 마지막 일정 바로 삭제
    if any(word in user_input for word in ['방금', '아까', '조금전', '바로']):
        if last_added_event['id']:
            delete_event(last_added_event['id'])
            title = last_added_event['title']
            last_added_event['id'] = None
            last_added_event['title'] = None
            return f"'{title}' 일정을 삭제했어요! 🗑️"

    # 키워드와 날짜 추출 / Extract keyword and date
    today_str = datetime.now().strftime('%Y-%m-%d (%A)')
    prompt = (
        f"Today is {today_str}.\n"
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

    event_id, event_title = get_recent_event_id(keyword, date if date else None)

    if event_id:
        delete_event(event_id)
        return f"'{event_title}' 일정을 삭제했어요! 🗑️"
    else:
        return f"'{keyword}' 관련 일정을 찾지 못했어요. 일정 이름을 좀 더 구체적으로 말씀해 주세요."
