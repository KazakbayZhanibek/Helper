import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError, orgApi } from "./api";
import { AccountError, VerifyEmailNotice } from "./AccountFeedback";
import { date, Icon, Loading } from "./components";
import { kitchenRoles } from "./InviteForm";
import type { Organization, Role, Session } from "./types";
import "./customer.css";

type Member = { id: string; email: string; display_name: string; role: Role };
type Invitation = { id: string; email: string; role: Role; expires_at: string };
type Team = { members: Member[]; invitations: Invitation[] };
const grantRoles = {
  reader: "Открывает опубликованные рецепты и готовит по шагам.",
  author: "Добавляет рецепты и отправляет их на проверку.",
  reviewer: "Проверяет шаги и публикует рецепты для кухни.",
};

type Props = {
  org: Organization;
  user?: Session["user"] & { email_verified?: boolean };
};
export function KitchenTeam(props: Props) {
  return <TeamView key={props.org.id + props.org.role} {...props} />;
}
function TeamView({ org, user }: Props) {
  const [team, setTeam] = useState<Team | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [error, setError] = useState<unknown>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<keyof typeof grantRoles>("reader");
  const [issued, setIssued] = useState<
    (Invitation & { invite_url: string }) | null
  >(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const base = orgApi(org.id);
  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoadError(null);
      try {
        setTeam(await api<Team>(base + "/team", { signal }));
      } catch (err) {
        if (!signal?.aborted) setLoadError(err);
      }
    },
    [base],
  );
  useEffect(() => {
    const controller = new AbortController();
    if (org.role === "admin") void load(controller.signal);
    return () => controller.abort();
  }, [load, org.role]);
  async function issue(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice("");
    try {
      const result = await api<Invitation & { invite_url: string }>(
        base + "/invitations",
        { method: "POST", body: { email: email.trim(), role } },
      );
      setIssued(result);
      setCopied(false);
      setEmail("");
      setNotice("Приглашение создано.");
      await load();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }
  async function remove(id: string, kind: "invitations" | "team") {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice("");
    try {
      await api(base + "/" + kind + "/" + id, { method: "DELETE" });
      if (issued?.id === id) setIssued(null);
      setConfirm(null);
      setNotice(
        kind === "team" ? "Доступ к кухне закрыт." : "Приглашение отозвано.",
      );
      await load();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }
  function removal(item: Invitation | Member, kind: "invitations" | "team") {
    const member = kind === "team";
    if (confirm !== item.id)
      return (
        <button
          type="button"
          className="text-button account-row-action"
          disabled={busy}
          aria-label={`${member ? "Убрать участника" : "Отозвать приглашение"} ${item.email}`}
          onClick={() => setConfirm(item.id)}
        >
          {member ? "Убрать" : "Отозвать"}
        </button>
      );
    return (
      <div className="account-change-confirm">
        <span>{member ? "Закрыть доступ?" : "Отозвать ссылку?"}</span>
        <button
          className="text-button danger-text"
          disabled={busy}
          onClick={() => void remove(item.id, kind)}
        >
          Да
        </button>
        <button
          className="text-button"
          disabled={busy}
          onClick={() => setConfirm(null)}
        >
          Отмена
        </button>
      </div>
    );
  }
  async function copy() {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.invite_url);
      setCopied(true);
    } catch {
      setNotice("Выделите ссылку в поле и скопируйте её.");
    }
  }
  if (org.role !== "admin")
    return (
      <section className="customer-page">
        <h1>Команда кухни</h1>
        <p>Составом команды управляет администратор кухни.</p>
      </section>
    );
  return (
    <section className="customer-page">
      <header className="customer-heading">
        <h1>Команда кухни</h1>
        <p>Управляйте доступом к рецептам кухни «{org.name}».</p>
      </header>
      <AccountError error={loadError}>
        <button className="text-button" onClick={() => void load()}>
          Попробовать ещё раз
        </button>
      </AccountError>
      {!team && !loadError && <Loading label="Загружаем участников…" />}
      {team && (
        <div className="account-management-layout">
          <div className="account-roster">
            <section aria-labelledby="account-members-heading">
              <h2 id="account-members-heading">
                Участники <small>{team.members.length}</small>
              </h2>
              <ul className="account-people">
                {team.members.map((member) => (
                  <li key={member.id}>
                    <span className="account-initial" aria-hidden="true">
                      {(member.display_name || member.email)
                        .trim()[0]
                        ?.toUpperCase()}
                    </span>
                    <div className="account-person">
                      <strong>{member.display_name}</strong>
                      <span>{member.email}</span>
                    </div>
                    <span className="account-role">
                      {kitchenRoles[member.role]}
                    </span>
                    {member.role !== "admin" && removal(member, "team")}
                  </li>
                ))}
              </ul>
            </section>
            {team.invitations.length > 0 && (
              <section aria-labelledby="account-pending-heading">
                <h2 id="account-pending-heading">
                  Ожидают ответа <small>{team.invitations.length}</small>
                </h2>
                <ul className="account-people">
                  {team.invitations.map((invite) => (
                    <li key={invite.id}>
                      <div className="account-person">
                        <strong>{invite.email}</strong>
                        <span>
                          {kitchenRoles[invite.role]} · Ссылка до{" "}
                          {date(invite.expires_at)}
                        </span>
                      </div>
                      {removal(invite, "invitations")}
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </div>
          <section
            className="account-sidepanel"
            aria-labelledby="account-invite-heading"
          >
            <h2 id="account-invite-heading">Пригласить участника</h2>
            {user?.email_verified === false && (
              <VerifyEmailNotice email={user.email} />
            )}
            <form className="account-form" onSubmit={issue}>
              <label>
                Электронная почта
                <input
                  type="email"
                  autoComplete="email"
                  autoCapitalize="none"
                  spellCheck={false}
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="name@example.com"
                  required
                  disabled={busy}
                />
              </label>
              <label>
                Роль
                <select
                  value={role}
                  disabled={busy}
                  onChange={(event) =>
                    setRole(event.target.value as keyof typeof grantRoles)
                  }
                  aria-describedby="account-role-hint"
                >
                  {Object.keys(grantRoles).map((key) => (
                    <option value={key} key={key}>
                      {kitchenRoles[key as Role]}
                    </option>
                  ))}
                </select>
              </label>
              <p className="field-hint" id="account-role-hint">
                {grantRoles[role]}
              </p>
              <button
                className="button primary"
                disabled={busy || user?.email_verified === false}
              >
                Создать приглашение <Icon name="arrow" size={16} />
              </button>
            </form>
            {issued && (
              <div className="account-issued">
                <label>
                  Ссылка для {issued.email}
                  <input
                    readOnly
                    value={issued.invite_url}
                    onFocus={(event) => event.currentTarget.select()}
                  />
                </label>
                <button className="text-button" onClick={copy}>
                  {copied ? "Скопировано" : "Скопировать ссылку"}
                </button>
              </div>
            )}
          </section>
        </div>
      )}
      <AccountError error={error}>
        {error instanceof ApiError && error.code === "MEMBER_LIMIT_REACHED" && (
          <a href={`/o/${org.id}/billing`}>Посмотреть тариф кухни</a>
        )}
      </AccountError>
      {notice && (
        <p role="status" className="account-operation-note">
          {notice}
        </p>
      )}
    </section>
  );
}
