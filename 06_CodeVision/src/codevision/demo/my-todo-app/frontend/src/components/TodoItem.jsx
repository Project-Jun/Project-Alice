import { deleteTodo } from '../api'

export default function TodoItem({ todo, onDeleted }) {
  async function handleDelete() {
    await deleteTodo(todo.id)
    onDeleted()
  }

  return (
    <li>
      <input type="checkbox" defaultChecked={todo.done} />
      <span>{todo.title}</span>
      <button onClick={handleDelete}>삭제</button>
    </li>
  )
}
