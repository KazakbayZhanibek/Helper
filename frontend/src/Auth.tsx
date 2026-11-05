import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ensureCsrf } from "./api";
import { Brand, ErrorBanner, Icon, PackageArt } from "./components";
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
          <span className="eyebrow light">Знания вашей команды</span>
          <h1>
            Хорошую работу
            <br />
            можно передать.
          </h1>
          <p>
            Сохраните опыт мастера в понятных шагах.
            <br />
            Чтобы каждый знал, как сделать правильно.
          </p>
          <PackageArt />
        </div>
        <p className="auth-bottom">
          <Icon name="shield" size={16} /> Только для участников вашей
          организации
        </p>
      </section>
      <section className="auth-form-section">
        <div className="auth-form-wrap">
          <span className="eyebrow">Рабочее пространство</span>
          <h2>{renewEmail ? "Сессия завершилась" : "Рады видеть вас"}</h2>
          <p className="muted">
            {renewEmail
              ? "Войдите снова. Несохранённые изменения остаются в этом окне."
              : "Войдите, чтобы продолжить работу с инструкциями."}
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
                placeholder="name@company.kz"
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
              {busy ? "Входим…" : "Войти в пространство"}
              <Icon name="arrow" />
            </button>
          </form>
          {demoPassword && (
            <div className="demo-login">
              <span className="eyebrow">Локальная демонстрация</span>
              <p>Выберите роль. Данные учебные; права проверяет сервер.</p>
              <div className="demo-roles">
                {[
                  ["admin", "Администратор"],
                  ["author", "Автор"],
                  ["reviewer", "Проверяющий"],
                  ["reader", "Читатель"],
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
            Доступ выдаёт администратор команды.
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
