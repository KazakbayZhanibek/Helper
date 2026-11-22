import { useEffect, useState, type FormEvent } from "react";
import type { AccountLogin } from "./Auth";
import { api } from "./api";
import { AccountError, PasswordField } from "./AccountFeedback";
import { Loading } from "./components";
import type { Role, Session } from "./types";

export const kitchenRoles: Record<Role, string> = {
  admin: "Администратор",
  author: "Автор",
  reviewer: "Проверяющий",
  reader: "Читатель",
};
type Preview = {
  organization_id: string;
  kitchen_name: string;
  role: Role;
  email_hint: string;
  has_account: boolean;
  matches_session: boolean | null;
};

export function InviteForm({
  token,
  session,
  onLogin,
}: {
  token: string;
  session?: Session | null;
  onLogin: AccountLogin;
}) {
  const [invite, setInvite] = useState<Preview | null>(null);
  const [signedIn, setSignedIn] = useState(session || null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const endpoint = "/auth/invitations/" + encodeURIComponent(token);
  useEffect(() => {
    const controller = new AbortController();
    api<Preview>(endpoint, { signal: controller.signal })
      .then(setInvite)
      .catch((err) => {
        if (!controller.signal.aborted) setError(err);
      });
    return () => controller.abort();
  }, [endpoint]);
  async function accept(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!invite || busy) return;
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    try {
      if (invite.has_account && !signedIn) {
        const account = await api<Session>("/auth/login", {
          method: "POST",
          body: { email: email.trim(), password },
        });
        setSignedIn(account);
        setPassword("");
        const refreshed = await api<Preview>(endpoint);
        setInvite(refreshed);
        if (!refreshed.matches_session) return;
      }
      const body = invite.has_account
        ? {}
        : { display_name: String(form.get("display_name")).trim(), password };
      const changed = await api<Session>(endpoint, { method: "POST", body });
      onLogin(changed, invite.organization_id);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }
  async function switchAccount() {
    setBusy(true);
    setError(null);
    try {
      await api("/auth/logout", { method: "POST" });
      location.reload();
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }
  if (!invite)
    return error ? (
      <>
        <h1>Приглашение недоступно</h1>
        <AccountError error={error} />
        <p className="account-subtitle">
          Попросите администратора кухни прислать новую ссылку.
        </p>
        <a href="/login" className="button secondary full">
          Перейти ко входу
        </a>
      </>
    ) : (
      <Loading label="Открываем приглашение…" />
    );
  const wrongAccount =
    !!signedIn && (invite.matches_session !== true || !invite.has_account);
  return (
    <>
      <h1>Вас приглашают на кухню «{invite.kitchen_name}»</h1>
      <p className="account-subtitle">
        Приглашение для {invite.email_hint}. Ваша роль —{" "}
        {kitchenRoles[invite.role].toLowerCase()}.
      </p>
      {wrongAccount ? (
        <div className="account-form">
          <p>
            Сейчас вы вошли как {signedIn?.user.email}. Войдите с почтой, на
            которую отправили приглашение.
          </p>
          <AccountError error={error} />
          <button
            type="button"
            className="button secondary full"
            disabled={busy}
            onClick={switchAccount}
          >
            Сменить аккаунт
          </button>
        </div>
      ) : (
        <form className="account-form" onSubmit={accept}>
          {!invite.has_account && (
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
          )}
          {invite.has_account && !signedIn && (
            <label>
              Электронная почта
              <input
                type="email"
                name="email"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                required
                disabled={busy}
              />
            </label>
          )}
          {(!invite.has_account || !signedIn) && (
            <PasswordField
              value={password}
              onChange={setPassword}
              fresh={!invite.has_account}
              disabled={busy}
            />
          )}
          {invite.has_account && !signedIn && (
            <a className="account-forgot" href="/forgot-password">
              Забыли пароль?
            </a>
          )}
          <AccountError error={error} />
          <button className="button primary full" disabled={busy}>
            {busy
              ? "Принимаем приглашение…"
              : !invite.has_account
                ? "Создать аккаунт и принять приглашение"
                : signedIn
                  ? "Принять приглашение"
                  : "Войти и принять приглашение"}
          </button>
        </form>
      )}
    </>
  );
}
