import type { TodoItem } from "../types/messages";

interface Props {
  todos: TodoItem[];
}

const STATUS_ICONS: Record<string, string> = {
  completed: "\u2705",
  in_progress: "\uD83D\uDD04",
  pending: "\u23F3",
};

export function TodoList({ todos }: Props) {
  if (todos.length === 0) return null;

  return (
    <div className="todo-list" data-testid="todo-list">
      <div className="todo-header">Tasks</div>
      {todos.map((item, i) => (
        <div key={i} className={`todo-item ${item.status}`} data-testid={`todo-item-${i}`}>
          <span className="todo-icon">
            {STATUS_ICONS[item.status] ?? "\u2B1C"}
          </span>
          <span className="todo-content">
            {item.status === "in_progress"
              ? (item.activeForm ?? item.content)
              : item.content}
          </span>
        </div>
      ))}
    </div>
  );
}
