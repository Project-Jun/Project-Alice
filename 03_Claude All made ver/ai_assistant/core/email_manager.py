"""
이메일 관리 모듈
Gmail SMTP (앱 비밀번호) 또는 Gmail API 지원
이메일 읽기, 작성, 전송 기능
"""
import smtplib
import imaplib
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from datetime import datetime
from typing import Optional


class EmailManager:
    """이메일 송수신 관리"""

    def __init__(self, config: dict):
        self.config = config["email"]
        self.smtp_host = self.config.get("smtp_host", "smtp.gmail.com")
        self.smtp_port = self.config.get("smtp_port", 587)
        self.user = self.config.get("smtp_user", "")
        self.password = self.config.get("smtp_password", "")

    def send_email(self, to: str, subject: str, body: str, cc: str = "", html: bool = False) -> bool:
        """이메일 전송"""
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = self.user
            msg["To"] = to
            msg["Subject"] = subject
            if cc:
                msg["Cc"] = cc

            content_type = "html" if html else "plain"
            msg.attach(MIMEText(body, content_type, "utf-8"))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.user, self.password)
                recipients = [to] + ([cc] if cc else [])
                server.sendmail(self.user, recipients, msg.as_string())
            return True
        except Exception as e:
            raise Exception(f"이메일 전송 실패: {e}")

    def get_recent_emails(self, folder: str = "INBOX", limit: int = 10) -> list[dict]:
        """최근 이메일 목록 가져오기 (IMAP)"""
        imap_host = self.smtp_host.replace("smtp", "imap")
        emails = []

        try:
            with imaplib.IMAP4_SSL(imap_host) as imap:
                imap.login(self.user, self.password)
                imap.select(folder)

                _, msg_ids = imap.search(None, "ALL")
                ids = msg_ids[0].split()[-limit:]

                for msg_id in reversed(ids):
                    _, msg_data = imap.fetch(msg_id, "(RFC822)")
                    msg = email.message_from_bytes(msg_data[0][1])

                    subject = self._decode_header(msg.get("Subject", ""))
                    sender = self._decode_header(msg.get("From", ""))
                    date_str = msg.get("Date", "")
                    body = self._get_body(msg)

                    emails.append({
                        "id": msg_id.decode(),
                        "subject": subject,
                        "from": sender,
                        "date": date_str,
                        "body": body[:500],  # 미리보기
                    })
        except Exception as e:
            raise Exception(f"이메일 읽기 실패: {e}")

        return emails

    def _decode_header(self, value: str) -> str:
        if not value:
            return ""
        parts = []
        for part, enc in decode_header(value):
            if isinstance(part, bytes):
                parts.append(part.decode(enc or "utf-8", errors="replace"))
            else:
                parts.append(part)
        return "".join(parts)

    def _get_body(self, msg) -> str:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        return part.get_payload(decode=True).decode("utf-8", errors="replace")
                    except Exception:
                        pass
        else:
            try:
                return msg.get_payload(decode=True).decode("utf-8", errors="replace")
            except Exception:
                pass
        return ""

    def build_draft_prompt(self, instructions: str) -> str:
        """AI 이메일 초안 작성 프롬프트"""
        return (
            f"다음 요청에 맞는 이메일 초안을 작성해주세요.\n"
            f"제목과 본문을 구분하여 작성하고, 한국어로 정중하고 전문적으로 작성해주세요.\n\n"
            f"요청: {instructions}"
        )

    def format_email_list(self, emails: list[dict]) -> str:
        """이메일 목록을 AI 컨텍스트용 텍스트로 변환"""
        if not emails:
            return "받은 이메일이 없습니다."
        result = [f"📧 최근 이메일 {len(emails)}개:"]
        for i, em in enumerate(emails, 1):
            result.append(
                f"{i}. [{em['date'][:16]}] {em['from']}\n"
                f"   제목: {em['subject']}\n"
                f"   내용: {em['body'][:200]}..."
            )
        return "\n\n".join(result)
