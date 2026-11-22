import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ensureCsrf } from "./api";
import { Brand, ErrorBanner, Icon } from "./components";
import type { Session } from "./types";
export function Auth({
  onLogin,
  renewEmail,
}: {
  onLogin: (s: Session) => void;
  renewEmail?: string;
}) {
  const [email, setEmail] = useState(renewEmail || "");
  const [password, setPassword] = useState("");
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const demoPassword =
    !renewEmail && import.meta.env.DEV
      ? import.meta.env.VITE_DEMO_PASSWORD
      : "";
  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await ensureCsrf();
      onLogin(
        await api<Session>("/auth/login", {
          method: "POST",
          body: { email, password },
        }),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="auth-layout">
      <section className="auth-story">
        <Brand />
        <div className="auth-intro">
          <h1>
            Книга рецептов
            <br />
            вашей кухни
          </h1>
          <p>
            Откройте ингредиенты и шаги с фотографиями, чтобы готовить вместе с
            автором рецепта.
          </p>
        </div>
        <p className="auth-bottom">
          <Icon name="shield" size={16} /> Доступ для участников кухни
        </p>
      </section>
      <section className="auth-form-section">
        <div className="auth-form-wrap">
          <h2>{renewEmail ? "Сессия завершилась" : "Вход в книгу рецептов"}</h2>
          <p className="muted">
            {renewEmail
              ? "Войдите снова. Несохранённые изменения остаются в этом окне."
              : "Используйте почту, на которую вам выдали доступ."}
          </p>
          <form onSubmit={submit} className="stack">
            <label>
              Электронная почта
              <input
                type="email"
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                placeholder="name@example.com"
                disabled={busy || !!renewEmail}
              />
            </label>
            <label>
              Пароль
              <span className="password-input">
                <input
                  type={visible ? "text" : "password"}
                  autoComplete="current-password"
                  autoFocus={!!renewEmail}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  disabled={busy}
                />
                <button
                  type="button"
                  className="icon-button"
                  onClick={() => setVisible(!visible)}
                  aria-label={visible ? "Скрыть пароль" : "Показать пароль"}
                  aria-pressed={visible}
                >
                  <Icon name="eye" />
                </button>
              </span>
            </label>
            {error && <ErrorBanner error={error} />}
            <button className="button primary full" disabled={busy}>
              {busy ? "Входим…" : "Войти"}
              <Icon name="arrow" />
            </button>
          </form>
          {demoPassword && (
            <div className="demo-login">
              <span className="eyebrow">Локальная демонстрация</span>
              <p>Выберите, под какой ролью войти в учебную кухню.</p>
              <div className="demo-roles">
                {[
                  ["admin", "Администратор"],
                  ["author", "Автор рецептов"],
                  ["reviewer", "Проверяющий"],
                  ["reader", "Готовлю по рецептам"],
                ].map(([role, label]) => (
                  <button
                    className="button subtle"
                    key={role}
                    disabled={busy}
                    onClick={() => {
                      setEmail(`${role}@demo.local`);
                      setPassword(demoPassword);
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          )}
          <p className="auth-help">
            Доступ выдаёт администратор вашей кухни.
            <br />
            Если не получается войти, обратитесь к нему.
          </p>
        </div>
      </section>
    </main>
  );
}

export function RenewSession({
  email,
  onLogin,
}: {
  email: string;
  onLogin: (s: Session) => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
    return () => ref.current?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className="renewal-dialog"
      aria-label="Повторный вход"
      onCancel={(e) => e.preventDefault()}
    >
      <Auth renewEmail={email} onLogin={onLogin} />
    </dialog>
  );
}
