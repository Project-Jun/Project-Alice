from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import Todo

router = APIRouter()


class TodoIn(BaseModel):
    title: str


@router.get("/todos")
def get_todos(db: Session = Depends(get_db)):
    return db.query(Todo).all()


@router.post("/todos")
def create_todo(todo: TodoIn, db: Session = Depends(get_db)):
    item = Todo(title=todo.title)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/todos/{todo_id}")
def delete_todo(todo_id: int, db: Session = Depends(get_db)):
    db.query(Todo).filter(Todo.id == todo_id).delete()
    db.commit()
    return {"ok": True}
