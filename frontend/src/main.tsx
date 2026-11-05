import { StrictMode, Component, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";
class ErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Interface error", error.name, info.componentStack);
  }
  render() {
    return this.state.failed ? (
      <main className="empty-state">
        <h1>Не удалось открыть страницу</h1>
        <p>Обновите страницу. Несохранённые изменения могут быть потеряны.</p>
        <button className="button primary" onClick={() => location.reload()}>
          Обновить
        </button>
      </main>
    ) : (
      this.props.children
    );
  }
}
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
