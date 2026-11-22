import { useId, useState, type ReactNode } from "react";
import { api, ApiError } from "./api";
import { ErrorBanner, Icon } from "./components";

export function AccountError({
  error,
  children,
}: {
  error: unknown;
  children?: ReactNode;
}) {
  if (!error) return null;
  const message =
    error instanceof Error ? error.message : "Не удалось выполнить действие.";
  const details =
    error instanceof ApiError ? Object.values(error.fields).flat() : [];
  return (
    <ErrorBanner error={message}>
      {details.length > 0 && (
        <ul>
          {[...new Set(details)].map((text) => (
            <li key={text}>{text}</li>
          ))}
        </ul>
      )}
      {children}
    </ErrorBanner>
  );
}

export function PasswordField({
  fresh = false,
  disabled = false,
  value,
  onChange,
}: {
  fresh?: boolean;
  disabled?: boolean;
  value: string;
  onChange: (value: string) => void;
}) {
  const [visible, setVisible] = useState(false);
  const id = useId();
  return (
    <div className="account-password">
      <label htmlFor={id}>{fresh ? "Новый пароль" : "Пароль"}</label>
      <div className="password-input">
        <input
          id={id}
          name="password"
          type={visible ? "text" : "password"}
          autoComplete={fresh ? "new-password" : "current-password"}
          required
          minLength={fresh ? 12 : undefined}
          maxLength={fresh ? 256 : 4096}
          value={value}
          disabled={disabled}
          aria-describedby={fresh ? id + "-hint" : undefined}
          onChange={(event) => onChange(event.target.value)}
        />
        <button
          className="icon-button"
          type="button"
          onClick={() => setVisible(!visible)}
          aria-label={visible ? "Скрыть пароль" : "Показать пароль"}
          aria-pressed={visible}
        >
          <Icon name="eye" />
        </button>
      </div>
      {fresh && (
        <span className="field-hint" id={id + "-hint"}>
          Не менее 12 символов
        </span>
      )}
    </div>
  );
}

export function VerifyEmailNotice({ email }: { email: string }) {
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
  const [error, setError] = useState<unknown>(null);
  async function resend() {
    setState("sending");
    setError(null);
    try {
      await api("/auth/email/resend", { method: "POST", body: {} });
      setState("sent");
    } catch (err) {
      setError(err);
      setState("idle");
    }
  }
  return (
    <section className="account-verify-note" aria-label="Подтверждение почты">
      <Icon name="info" size={18} />
      <div>
        <strong>Подтвердите почту</strong>
        <p>Откройте ссылку в письме на {email}.</p>
        <AccountError error={error} />
        {state === "sent" ? (
          <p role="status">Запрос принят. Проверьте входящие и папку «Спам».</p>
        ) : (
          <button
            className="text-button"
            type="button"
            onClick={resend}
            disabled={state === "sending"}
          >
            {state === "sending" ? "Отправляем…" : "Отправить письмо ещё раз"}
          </button>
        )}
      </div>
    </section>
  );
}
