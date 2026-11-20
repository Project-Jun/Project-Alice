# services/gmail_service.py - 이메일 서비스 / Email Service

from googleapiclient.discovery import build
from auth import get_credentials
import base64


# Gmail 서비스 객체 생성 / Build Gmail service
def get_gmail_service():
    creds = get_credentials()
    return build('gmail', 'v1', credentials=creds)


# 최근 이메일 가져오기 / Get recent emails
def get_recent_emails(count=5):
    service = get_gmail_service()
    results = service.users().messages().list(userId='me', maxResults=count).execute()
    messages = results.get('messages', [])

    emails = []
    for msg in messages:
        msg_detail = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        headers = msg_detail['payload']['headers']

        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '제목 없음')
        sender = next((h['value'] for h in headers if h['name'] == 'From'), '발신자 없음')
        snippet = msg_detail.get('snippet', '')

        emails.append({
            'id': msg['id'],
            'subject': subject,
            'sender': sender,
            'snippet': snippet
        })

    return emails


# 이메일 요약 핸들러 / Handle email summary requests
def handle_email(chat, user_input):
    emails = get_recent_emails()
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
