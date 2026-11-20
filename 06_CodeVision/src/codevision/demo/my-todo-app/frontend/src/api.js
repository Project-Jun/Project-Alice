const API = 'http://localhost:8000'

/** 할 일 목록을 서버에서 가져와요. */
export async function fetchTodos() {
  const res = await fetch(`${API}/todos`)
  return res.json()
}

/** 새 할 일을 서버에 저장해요. */
export async function addTodo(title) {
  const res = await fetch(`${API}/todos`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
  return res.json()
}

/** 할 일을 지워요. */
export async function deleteTodo(id) {
  await fetch(`${API}/todos/${id}`, { method: 'DELETE' })
}
