from googleapiclient.discovery import build
from auth import get_credentials
import base64

def get_gmail_service():
    creds = get_credentials()
    return build('gmail', 'v1', credentials=creds)

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

if __name__ == '__main__':
    emails = get_recent_emails()
    for i, email in enumerate(emails, 1):
        print(f"\n📧 [{i}]")
        print(f"발신자: {email['sender']}")
        print(f"제목: {email['subject']}")
        print(f"내용 미리보기: {email['snippet']}")