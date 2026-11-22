import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import { api, ApiError, libraryPath } from "./api";
import { Auth, RenewSession } from "./Auth";
import { Library, NewRecipe } from "./Library";
import { KitchenTeam } from "./KitchenTeam";
import { KitchenBilling } from "./KitchenBilling";
import { VerifyEmailNotice } from "./AccountFeedback";
import { CustomerList, KitchenAISettings } from "./KitchenSettings";
import { Editor } from "./Editor";
import { Reader } from "./Reader";
import { Home } from "./Home";
import { ExampleRecipe } from "./ExampleRecipe";
import { Brand, ErrorBanner, Icon, Loading } from "./components";
import type { Session } from "./types";
import "./recipe-shell.css";
import "./product.css";
const roleName = {
  admin: "Администратор",
  author: "Автор рецептов",
  reviewer: "Проверяющий",
  reader: "Читатель рецептов",
};
export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [path, setPath] = useState(location.pathname);
  const example = path === "/example" || path === "/example/";
  const dirty = useRef(false);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [expired, setExpired] = useState(false);
  const setDirtyGuard = useCallback((value: boolean) => {
    dirty.current = value;
  }, []);
  const navigate = useCallback((next: string) => {
    if (
      dirty.current &&
      !window.confirm(
        "Есть несохранённые изменения или незавершённая загрузка. Покинуть страницу?",
      )
    )
      return;
    dirty.current = false;
    history.pushState({}, "", next);
    setPath(new URL(next, location.origin).pathname);
    window.scrollTo({ top: 0, behavior: "instant" });
  }, []);
  function followLink(event: MouseEvent<HTMLAnchorElement>) {
    if (
      event.button ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    event.preventDefault();
    navigate(event.currentTarget.pathname);
  }
  useEffect(() => {
    const pop = () => {
      if (
        dirty.current &&
        !window.confirm(
          "Есть несохранённые изменения или незавершённая загрузка. Покинуть страницу?",
        )
      ) {
        history.pushState({}, "", path);
        return;
      }
      dirty.current = false;
      setPath(location.pathname);
    };
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, [path]);
  const check = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setSession(await api<Session>("/auth/me"));
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401))
        setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    if (!example) void check();
  }, [check, example]);
  const awaitingVerification = session?.user.email_verified === false;
  useEffect(() => {
    if (!awaitingVerification) return;
    const controller = new AbortController();
    const refresh = () => {
      api<Session>("/auth/me", { signal: controller.signal })
        .then((updated) => {
          if (updated.user.email_verified && !controller.signal.aborted) {
            setSession((previous) =>
              previous?.user.id === updated.user.id ? updated : previous,
            );
          }
        })
        .catch(() => {});
    };
    window.addEventListener("focus", refresh);
    return () => {
      controller.abort();
      window.removeEventListener("focus", refresh);
    };
  }, [awaitingVerification]);
  useEffect(() => {
    const expire = () => setExpired(true);
    window.addEventListener("session-expired", expire);
    return () => window.removeEventListener("session-expired", expire);
  }, []);
  function login(s: Session, organizationId?: string) {
    setSession(s);
    setExpired(false);
    if (organizationId) {
      navigate(libraryPath(organizationId));
      return;
    }
    const next = new URLSearchParams(location.search).get("next");
    if (next?.startsWith("/o/") && !next.startsWith("//")) navigate(next);
    else if (!location.pathname.startsWith("/o/"))
      navigate(
        !s.organizations.length && s.user.is_operator
          ? "/customers"
          : libraryPath(s.organizations[0]?.id || ""),
      );
  }
  async function logout() {
    if (
      dirty.current &&
      !window.confirm(
        "Есть несохранённые изменения или незавершённая загрузка. Выйти?",
      )
    )
      return;
    setLogoutBusy(true);
    setError("");
    try {
      await api("/auth/logout", { method: "POST" });
      dirty.current = false;
      setSession(null);
      navigate("/login");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        dirty.current = false;
        setSession(null);
        setExpired(false);
        navigate("/login");
      } else setError((e as Error).message);
    } finally {
      setLogoutBusy(false);
    }
  }
  if (example) return <ExampleRecipe />;
  if (loading)
    return (
      <div className="app-loading">
        <Brand />
        <Loading label="Открываем книгу рецептов…" />
      </div>
    );
  if (path === "/") return <Home session={session} navigate={navigate} />;
  const accountRoute =
    [
      "/register",
      "/forgot-password",
      "/reset-password",
      "/verify-email",
    ].includes(path) || path.startsWith("/invite/");
  if (!session || accountRoute)
    return (
      <>
        {error ? (
          <div className="service-error">
            <ErrorBanner error={error}>
              <button className="text-button" onClick={() => void check()}>
                Повторить подключение
              </button>
            </ErrorBanner>
          </div>
        ) : null}
        <Auth key={path} path={path} session={session} onLogin={login} />
      </>
    );
  const parts = path.split("/").filter(Boolean);
  const requestedOrg = parts[0] === "o" ? parts[1] : null;
  const org =
    session.organizations.find((o) => o.id === requestedOrg) ||
    (!requestedOrg ? session.organizations[0] : undefined);
  if (
    (path === "/customers" || (path === "/login" && !org)) &&
    session.user.is_operator
  )
    return (
      <CustomerList
        onBack={() => navigate(org ? libraryPath(org.id) : "/")}
        onLogout={() => void logout()}
      />
    );
  if (!org)
    return (
      <main className="empty-state">
        <Brand />
        <h1>Нет доступа к этой кухне</h1>
        <p>
          Обратитесь к администратору или войдите под другой учётной записью.
        </p>
        <button className="button secondary" onClick={() => void logout()}>
          Выйти
        </button>
      </main>
    );
  const id = parts[3];
  const isNew = id === "new";
  const isEditor = !!id && !isNew && ["edit", "review"].includes(parts[4]);
  const collectionRoute =
    parts[0] === "o" && ["recipes", "instructions"].includes(parts[2]);
  const validDetail =
    parts.length === 3 ||
    parts.length === 4 ||
    (parts.length === 5 && !isNew && isEditor);
  const setting =
    parts.length === 3 &&
    parts[0] === "o" &&
    ["team", "billing"].includes(parts[2])
      ? parts[2]
      : "";
  const invalidRoute =
    !setting && path !== "/login" && (!collectionRoute || !validDetail);
  const tabs = [
    {
      label: "Рецепты",
      url: libraryPath(org.id),
      icon: "book",
      selected: !setting,
    },
    ...(org.role === "admin"
      ? [
          {
            label: "Команда",
            url: `/o/${org.id}/team`,
            icon: "people",
            selected: setting === "team",
          },
          {
            label: "Подписка",
            url: `/o/${org.id}/billing`,
            icon: "card",
            selected: setting === "billing",
          },
        ]
      : []),
  ];
  return (
    <>
      <div className="product-shell">
        <a className="skip-link" href="#main">
          К содержимому
        </a>
        <header className="kitchen-header">
          <div className="kitchen-bar">
            <a
              className="kitchen-brand"
              href={libraryPath(org.id)}
              onClick={followLink}
              aria-label="Рецепты мастера — к рецептам"
            >
              <Brand />
            </a>
            <nav className="kitchen-tabs" aria-label="Разделы кухни">
              {tabs.map((tab) => (
                <a
                  key={tab.url}
                  href={tab.url}
                  aria-current={tab.selected ? "page" : undefined}
                  onClick={followLink}
                >
                  <Icon name={tab.icon} size={18} />
                  {tab.label}
                </a>
              ))}
            </nav>
            <details className="account-menu" key={path}>
              <summary aria-label="Меню аккаунта">
                <span className="avatar">
                  {session.user.display_name.slice(0, 1)}
                </span>
                <span className="account-name">
                  {session.user.display_name}
                </span>
                <Icon name="down" size={15} />
              </summary>
              <div className="account-menu-body">
                <strong>{session.user.email}</strong>
                <span>{roleName[org.role]}</span>
                {session.organizations.length > 1 ? (
                  <label>
                    Кухня
                    <select
                      value={org.id}
                      onChange={(e) => navigate(libraryPath(e.target.value))}
                    >
                      {session.organizations.map((k) => (
                        <option key={k.id} value={k.id}>
                          {k.name}
                        </option>
                      ))}
                    </select>
                  </label>
                ) : (
                  <span>{org.name}</span>
                )}
                {session.user.is_operator && (
                  <button onClick={() => navigate("/customers")}>
                    Клиенты сервиса
                  </button>
                )}
                <button disabled={logoutBusy} onClick={() => void logout()}>
                  <Icon name="logout" size={16} /> Выйти
                </button>
              </div>
            </details>
          </div>
        </header>
        <div className="workspace">
          <main
            id="main"
            className={`main-content ${isEditor ? "editor-page" : ""}`}
            key={`${org.id}/${setting}/${id || ""}/${parts[4] || ""}`}
          >
            {error && <ErrorBanner error={error} />}{" "}
            {setting && org.role !== "admin" ? (
              <ErrorBanner error="Этот раздел доступен администратору кухни." />
            ) : setting === "team" ? (
              <>
                <KitchenTeam org={org} user={session.user} />
                <KitchenAISettings org={org} />
              </>
            ) : setting === "billing" ? (
              <KitchenBilling org={org} user={session.user} />
            ) : invalidRoute ? (
              <div className="empty-state">
                <h1>Страница не найдена</h1>
                <button
                  className="button primary"
                  onClick={() => navigate(libraryPath(org.id))}
                >
                  Открыть книгу рецептов
                </button>
              </div>
            ) : isNew ? (
              org.role === "reader" ? (
                <ErrorBanner error="Для создания рецептов нужны права автора, проверяющего или администратора." />
              ) : (
                <NewRecipe
                  org={org}
                  navigate={navigate}
                  setDirtyGuard={setDirtyGuard}
                />
              )
            ) : isEditor ? (
              org.role === "reader" ? (
                <ErrorBanner error="Рабочая версия доступна только авторским ролям." />
              ) : (
                <Editor
                  key={`${session.user.id}:${org.id}:${id}`}
                  userId={session.user.id}
                  org={org}
                  id={id}
                  navigate={navigate}
                  setDirtyGuard={setDirtyGuard}
                />
              )
            ) : id ? (
              <Reader org={org} id={id} navigate={navigate} />
            ) : (
              <>
                {session.user.email_verified === false && (
                  <VerifyEmailNotice email={session.user.email} />
                )}
                <Library org={org} navigate={navigate} />
              </>
            )}
          </main>
        </div>
      </div>
      {expired && (
        <RenewSession
          email={session.user.email}
          onLogin={(s) => {
            setSession(s);
            setExpired(false);
          }}
        />
      )}
    </>
  );
}
