import { useEffect, useState } from 'react'
import { fetchTodos } from '../api'
import TodoItem from './TodoItem'

export default function TodoList() {
  const [todos, setTodos] = useState([])

  useEffect(() => {
    fetchTodos().then(setTodos)
  }, [])

  return (
    <ul>
      {todos.map((todo) => (
        <TodoItem
          key={todo.id}
          todo={todo}
          onDeleted={() => setTodos(todos.filter((t) => t.id !== todo.id))}
        />
      ))}
    </ul>
  )
}
