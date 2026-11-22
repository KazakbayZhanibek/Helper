import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { api, ensureCsrf, libraryPath } from "./api";
import { AccountError, PasswordField } from "./AccountFeedback";
import { InviteForm } from "./InviteForm";
import { Brand, Icon } from "./components";
import type { Navigate, Session } from "./types";
import "./customer.css";

export type AccountLogin = (session: Session, organizationId?: string) => void;
type Mode = "login" | "register" | "forgot" | "reset" | "verify";
const modes: Record<string, Mode> = {
  "/register": "register",
  "/forgot-password": "forgot",
  "/reset-password": "reset",
  "/verify-email": "verify",
};

function AccountFrame({ children }: { children: ReactNode }) {
  return (
    <div className="account-entrance">
      <header className="account-masthead">
        <a href="/" aria-label="Рецепты мастера — главная">
          <Brand />
        </a>
        <a className="account-example-link" href="/example">
          Посмотреть пример <Icon name="arrow" size={15} />
        </a>
      </header>
      <main className="account-stage">
        <div className="account-sheet">{children}</div>
        <figure className="account-illustration">
          <img
            src="/examples/vegetables/vegetables.jpg"
            alt="Повар нарезает свежие овощи на разделочной доске"
            width="960"
            height="540"
          />
          <figcaption>
            <span>Из книги рецептов</span>
            <a href="/example">
              Овощная нарезка <Icon name="arrow" size={18} />
            </a>
          </figcaption>
        </figure>
      </main>
    </div>
  );
}

const titles = {
  login: "С возвращением",
  register: "Создайте свою кухню",
  forgot: "Забыли пароль?",
  reset: "Придумайте новый пароль",
  verify: "Подтвердите почту",
};
const buttons = {
  login: "Войти",
  register: "Создать бесплатную кухню",
  forgot: "Отправить ссылку",
  reset: "Сохранить пароль",
  verify: "Подтвердить почту",
};
const pending = {
  login: "Входим…",
  register: "Создаём кухню…",
  forgot: "Отправляем…",
  reset: "Сохраняем…",
  verify: "Подтверждаем…",
};

function AccessForm({
  mode,
  onLogin,
  renewEmail,
  session,
}: {
  mode: Mode;
  onLogin: AccountLogin;
  renewEmail?: string;
  session?: Session | null;
}) {
  const [email, setEmail] = useState(renewEmail || "");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [complete, setComplete] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const params = new URLSearchParams(location.search);
  const uid = params.get("uid"),
    token = params.get("token");
  const missingToken =
    (mode === "verify" || mode === "reset") && (!uid || !token);
  const demoPassword =
    !renewEmail && mode === "login" && import.meta.env.DEV
      ? import.meta.env.VITE_DEMO_PASSWORD
      : "";
  const goToKitchen = session?.organizations[0]
    ? libraryPath(session.organizations[0].id)
    : "/login";
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || missingToken) return;
    const data = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    try {
      if (mode === "login") {
        await ensureCsrf();
        onLogin(
          await api<Session>("/auth/login", {
            method: "POST",
            body: { email: email.trim(), password },
          }),
        );
      } else if (mode === "register") {
        onLogin(
          await api<Session>("/auth/register", {
            method: "POST",
            body: {
              email: email.trim(),
              password,
              display_name: String(data.get("display_name")).trim(),
              kitchen_name: String(data.get("kitchen_name")).trim(),
            },
          }),
        );
      } else {
        const endpoint = {
          forgot: "/auth/password/forgot",
          reset: "/auth/password/reset",
          verify: "/auth/email/verify",
        }[mode];
        const body =
          mode === "forgot"
            ? { email: email.trim() }
            : mode === "reset"
              ? { uid, token, password }
              : { uid, token };
        await api(endpoint, { method: "POST", body });
        setPassword("");
        setComplete(true);
      }
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }
  if (complete) {
    const title =
      mode === "forgot"
        ? "Проверьте почту"
        : mode === "reset"
          ? "Пароль изменён"
          : "Почта подтверждена";
    return (
      <section className="account-success" aria-live="polite">
        <span className="account-success-mark">
          <Icon name="check" size={24} />
        </span>
        <h1>{title}</h1>
        <p>
          {mode === "forgot"
            ? `Если для ${email} есть аккаунт, на этот адрес придёт ссылка для восстановления. Проверьте также папку «Спам».`
            : mode === "reset"
              ? "Теперь можно войти с новым паролем."
              : "Теперь вы можете пользоваться возможностями своей кухни."}
        </p>
        <a
          className="button primary full"
          href={mode === "verify" ? goToKitchen : "/login"}
        >
          {mode === "verify" && session ? "Открыть кухню" : "Перейти ко входу"}
        </a>
        {mode === "forgot" && (
          <button className="text-button" onClick={() => setComplete(false)}>
            Указать другую почту
          </button>
        )}
      </section>
    );
  }
  return (
    <>
      <h1>{renewEmail ? "Войдите снова" : titles[mode]}</h1>
      {renewEmail ? (
        <p className="account-subtitle">
          Сессия завершилась. Ваши изменения остались в этом окне.
        </p>
      ) : mode === "login" ? (
        <p className="account-subtitle">Войдите, чтобы открыть свои рецепты.</p>
      ) : mode === "register" ? (
        <p className="account-subtitle">
          Сохраните первый рецепт и пригласите тех, с кем готовите.
        </p>
      ) : mode === "forgot" ? (
        <p className="account-subtitle">
          Отправим ссылку для восстановления на почту вашего аккаунта.
        </p>
      ) : mode === "verify" && !missingToken ? (
        <p className="account-subtitle">
          Нажмите кнопку, чтобы связать этот адрес с вашим аккаунтом.
        </p>
      ) : null}
      {missingToken ? (
        <div className="account-success">
          <p role="alert">
            В ссылке не хватает данных. Откройте её целиком из письма.
          </p>
          <a
            href={mode === "reset" ? "/forgot-password" : goToKitchen}
            className="button secondary full"
          >
            {mode === "reset" ? "Запросить новую ссылку" : "Перейти в аккаунт"}
          </a>
        </div>
      ) : (
        <form onSubmit={submit} className="account-form">
          {mode === "register" && (
            <div className="account-name-fields">
              <label>
                Ваше имя
                <input
                  name="display_name"
                  autoComplete="name"
                  required
                  maxLength={120}
                  disabled={busy}
                />
              </label>
              <label>
                Название кухни
                <input
                  name="kitchen_name"
                  autoComplete="organization"
                  placeholder="Моя кухня"
                  required
                  maxLength={120}
                  disabled={busy}
                />
              </label>
            </div>
          )}
          {(mode === "login" || mode === "register" || mode === "forgot") && (
            <label>
              Электронная почта
              <input
                name="email"
                type="email"
                autoComplete={mode === "login" ? "username" : "email"}
                autoCapitalize="none"
                spellCheck={false}
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                required
                maxLength={254}
                placeholder="name@example.com"
                disabled={busy || !!renewEmail}
              />
            </label>
          )}
          {(mode === "login" || mode === "register" || mode === "reset") && (
            <PasswordField
              fresh={mode !== "login"}
              value={password}
              onChange={setPassword}
              disabled={busy}
            />
          )}
          {mode === "login" && !renewEmail && (
            <a className="account-forgot" href="/forgot-password">
              Забыли пароль?
            </a>
          )}
          <AccountError error={error}>
            {mode === "reset" && (
              <a href="/forgot-password">Запросить новую ссылку</a>
            )}
            {mode === "verify" && <a href={goToKitchen}>Вернуться в аккаунт</a>}
          </AccountError>
          <button className="button primary full" disabled={busy}>
            {busy ? pending[mode] : buttons[mode]}{" "}
            <Icon name="arrow" size={17} />
          </button>
        </form>
      )}
      {demoPassword && (
        <details className="account-demo">
          <summary>Учебная кухня</summary>
          <div className="demo-roles">
            {[
              ["admin", "Администратор"],
              ["author", "Автор"],
              ["reviewer", "Проверяющий"],
              ["reader", "Читатель"],
            ].map(([role, label]) => (
              <button
                className="button subtle"
                type="button"
                key={role}
                disabled={busy}
                onClick={() => {
                  setEmail(role + "@demo.local");
                  setPassword(demoPassword);
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </details>
      )}
      {!renewEmail && mode === "login" && (
        <p className="account-switch">
          Впервые здесь? <a href="/register">Создать аккаунт</a>
        </p>
      )}
      {!renewEmail && mode === "register" && (
        <p className="account-switch">
          Уже есть аккаунт? <a href="/login">Войти</a>
        </p>
      )}
      {mode === "forgot" && (
        <p className="account-switch">
          <a href="/login">Вернуться ко входу</a>
        </p>
      )}
    </>
  );
}

export function Auth({
  onLogin,
  renewEmail,
  session,
  path = location.pathname,
}: {
  onLogin: AccountLogin;
  renewEmail?: string;
  session?: Session | null;
  path?: string;
  navigate?: Navigate;
}) {
  if (renewEmail)
    return (
      <div className="account-sheet account-renew">
        <AccessForm mode="login" renewEmail={renewEmail} onLogin={onLogin} />
      </div>
    );
  const clean = path.replace(/\/+$/, "");
  const invite = clean.match(/^\/invite\/([^/]+)$/);
  if (clean === "/register" && session) {
    const kitchen = session.organizations[0];
    return (
      <AccountFrame>
        <h1>Вы уже вошли</h1>
        <p className="account-subtitle">Ваш аккаунт — {session.user.email}.</p>
        <a
          className="button primary full"
          href={kitchen ? libraryPath(kitchen.id) : "/login"}
        >
          {kitchen ? "Открыть мою кухню" : "Открыть аккаунт"}
        </a>
      </AccountFrame>
    );
  }
  return (
    <AccountFrame>
      {invite ? (
        <InviteForm
          key={invite[1]}
          token={invite[1]}
          session={session}
          onLogin={onLogin}
        />
      ) : (
        <AccessForm
          key={clean}
          mode={modes[clean] || "login"}
          onLogin={onLogin}
          session={session}
        />
      )}
    </AccountFrame>
  );
}

export function RenewSession({
  email,
  onLogin,
}: {
  email: string;
  onLogin: AccountLogin;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className="renewal-dialog"
      aria-label="Повторный вход"
      onCancel={(event) => event.preventDefault()}
    >
      <Auth renewEmail={email} onLogin={onLogin} />
    </dialog>
  );
}
