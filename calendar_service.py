from googleapiclient.discovery import build
from auth import get_credentials
from datetime import datetime, timedelta

def get_calendar_service():
    creds = get_credentials()
    return build('calendar', 'v3', credentials=creds)

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
    
    if not events:
        print("예정된 일정이 없어요.")
        return []
    
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

if __name__ == '__main__':
    events = get_upcoming_events()
    for i, event in enumerate(events, 1):
        print(f"\n📅 [{i}]")
        print(f"제목: {event['title']}")
        print(f"시작: {event['start']}")
        print(f"설명: {event['description']}")

# 일정 추가 후 링크와 ID 함께 반환 / Return both link and event ID after adding
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

def delete_event(event_id):
    service = get_calendar_service()
    service.events().delete(calendarId='primary', eventId=event_id).execute()
    return True

# 키워드와 날짜로 일정 검색 / Search event by keyword and optional date
def get_recent_event_id(title_keyword, date=None):
    service = get_calendar_service()
    from datetime import datetime, timedelta
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
            # 날짜도 일치하는 경우만 반환 / Return only if date also matches
            event_start = event['start'].get('dateTime', event['start'].get('date', ''))
            date_match = date in event_start
            if title_match and date_match:
                return event['id'], event.get('summary')
        else:
            if title_match:
                return event['id'], event.get('summary')
    return None, None