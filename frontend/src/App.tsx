import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, libraryPath } from "./api";
import { Auth, RenewSession } from "./Auth";
import { Library, NewRecipe } from "./Library";
import { Editor } from "./Editor";
import { Reader } from "./Reader";
import { Home } from "./Home";
import { ExampleRecipe } from "./ExampleRecipe";
import { Brand, ErrorBanner, Icon, Loading } from "./components";
import type { Session } from "./types";
import "./recipe-shell.css";
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
  useEffect(() => {
    const expire = () => setExpired(true);
    window.addEventListener("session-expired", expire);
    return () => window.removeEventListener("session-expired", expire);
  }, []);
  function login(s: Session) {
    setSession(s);
    const next = new URLSearchParams(location.search).get("next");
    if (next?.startsWith("/o/") && !next.startsWith("//")) navigate(next);
    else if (!location.pathname.startsWith("/o/"))
      navigate(libraryPath(s.organizations[0]?.id || ""));
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
  if (!session)
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
        <Auth onLogin={login} />
      </>
    );
  const parts = path.split("/").filter(Boolean);
  const requestedOrg = parts[0] === "o" ? parts[1] : null;
  const org =
    session.organizations.find((o) => o.id === requestedOrg) ||
    (!requestedOrg ? session.organizations[0] : undefined);
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
  const invalidRoute = path !== "/login" && (!collectionRoute || !validDetail);
  return (
    <>
      <div className="app-shell">
        <a className="skip-link" href="#main">
          Перейти к содержимому
        </a>
        <aside className="sidebar">
          <button
            className="brand-button"
            onClick={() => navigate(libraryPath(org.id))}
            aria-label="Рецепты мастера — книга рецептов"
          >
            <Brand />
          </button>
          {session.organizations.length > 1 ? (
            <label className="recipe-kitchen-selector">
              <span>Кухня</span>
              <select
                value={org.id}
                onChange={(event) => navigate(libraryPath(event.target.value))}
              >
                {session.organizations.map((kitchen) => (
                  <option key={kitchen.id} value={kitchen.id}>
                    {kitchen.name}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <div className="organization-card">
              <span className="org-symbol">{org.name[0]}</span>
              <div>
                <strong>{org.name}</strong>
                <small>
                  {session.demo_mode ? "Локальная кухня" : "Кухня команды"}
                </small>
              </div>
            </div>
          )}
          <div className="nav-label">Моя кухня</div>
          <nav aria-label="Главное меню">
            <button
              className={!id ? "active" : ""}
              onClick={() => navigate(libraryPath(org.id))}
            >
              <Icon name="book" />
              Книга рецептов
              <Icon name="chevron" size={14} />
            </button>
            {org.role !== "reader" && (
              <button
                className={isNew ? "active" : ""}
                onClick={() => navigate(libraryPath(org.id) + "/new")}
              >
                <Icon name="plus" />
                Создать рецепт
              </button>
            )}
          </nav>
          <div className="sidebar-note recipe-sidebar-help">
            <button className="text-button" onClick={() => navigate("/")}>
              О сервисе <Icon name="arrow" size={14} />
            </button>
          </div>
          <div className="profile">
            <span className="avatar">
              {session.user.display_name.slice(0, 1) || "М"}
            </span>
            <div>
              <strong>{session.user.display_name}</strong>
              <small>{roleName[org.role]}</small>
            </div>
            <button
              className="icon-button"
              onClick={() => void logout()}
              disabled={logoutBusy}
              aria-label="Выйти из учётной записи"
            >
              <Icon name="logout" size={18} />
            </button>
          </div>
        </aside>
        <div className="workspace">
          <header className="topbar">
            <div>
              <Icon name="grid" size={16} />
              <span>Моя кухня</span>
              <span className="topbar-divider">/</span>
              <strong>
                {isNew
                  ? "Новый рецепт"
                  : isEditor
                    ? "Редактор"
                    : id
                      ? "Просмотр"
                      : "Книга рецептов"}
              </strong>
            </div>
            <span className="pilot-badge">
              <span />
              {session.demo_mode ? "Локальная кухня" : "Командный доступ"}
            </span>
          </header>
          <main
            id="main"
            className={`main-content ${isEditor ? "editor-page" : ""}`}
            key={`${org.id}/${id || ""}/${parts[4] || ""}`}
          >
            {error && <ErrorBanner error={error} />}{" "}
            {invalidRoute ? (
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
              <Library org={org} navigate={navigate} />
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
