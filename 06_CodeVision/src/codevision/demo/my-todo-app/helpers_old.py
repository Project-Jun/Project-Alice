"""예전에 쓰던 도우미 함수 모음 (지금은 쓰지 않음)."""
from datetime import datetime


def format_date(value):
    """날짜를 '2026-11-19' 같은 글자로 바꿔요."""
    return value.strftime("%Y-%m-%d")


def old_save_todo(todos, title):
    """예전 저장 방식: 목록에 바로 추가해요."""
    todos.append({"title": title, "done": False, "created": format_date(datetime.now())})
    return todos
