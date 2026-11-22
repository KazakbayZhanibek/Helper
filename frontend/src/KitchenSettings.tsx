import { useEffect, useState } from "react";
import { api, orgApi } from "./api";
import { Brand, ErrorBanner, Icon, Loading } from "./components";
import { countNoun, subscriptionStatus } from "./format";
import type { Organization } from "./types";

type AIOptions = { enabled: boolean; can_enable: boolean };

export function KitchenAISettings({ org }: { org: Organization }) {
  const [options, setOptions] = useState<AIOptions | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(0);
  const path = orgApi(org.id) + "/ai-settings";
  useEffect(() => {
    const controller = new AbortController();
    api<AIOptions>(path, { signal: controller.signal })
      .then(setOptions)
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [path, retry]);
  async function toggle() {
    if (!options || busy) return;
    setBusy(true);
    setError("");
    try {
      setOptions(
        await api<AIOptions>(path, {
          method: "POST",
          body: { enabled: !options.enabled },
        }),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <details className="kitchen-ai form-details">
      <summary>Обработка видео с ИИ</summary>
      {error && (
        <ErrorBanner error={error}>
          {!options && (
            <button
              className="text-button"
              onClick={() => {
                setError("");
                setRetry((n) => n + 1);
              }}
            >
              Повторить
            </button>
          )}
        </ErrorBanner>
      )}
      {options ? (
        <>
          <p>
            {options.enabled
              ? "Авторы могут отправлять кадры на разбор после своего согласия."
              : options.can_enable
                ? "Можно разрешить авторам отправлять кадры в бесплатную модель OpenRouter. Каждый раз автор подтверждает отправку."
                : "Внешняя модель для этой кухни сейчас недоступна. Видео и ручная разметка работают."}
          </p>
          {(options.enabled || options.can_enable) && (
            <button
              type="button"
              className="button secondary"
              onClick={toggle}
              disabled={busy}
            >
              {options.enabled
                ? "Отключить ИИ для кухни"
                : "Разрешить бесплатный разбор"}
            </button>
          )}
        </>
      ) : (
        !error && <Loading />
      )}
    </details>
  );
}

type Customer = {
  id: string;
  name: string;
  email: string | null;
  members: number;
  created_at: string;
  plan: string;
  status: string;
  subscription_status: string;
};
type Page = { items: Customer[]; next_offset: number | null };

export function CustomerList({
  onBack,
  onLogout,
}: {
  onBack: () => void;
  onLogout: () => void;
}) {
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    setPage(null);
    setError("");
    const timer = setTimeout(() => {
      api<Page>(
        "/operator/customers?" +
          new URLSearchParams({ q: query, offset: String(offset) }),
        { signal: abort.signal },
      )
        .then(setPage)
        .catch((e) => {
          if (!abort.signal.aborted) setError(e.message);
        });
    }, 150);
    return () => {
      clearTimeout(timer);
      abort.abort();
    };
  }, [offset, query, retry]);
  return (
    <div className="product-shell">
      <header className="kitchen-header">
        <div className="kitchen-bar">
          <Brand />
          <button className="text-button" onClick={onBack}>
            К сайту
          </button>
          <button className="text-button" onClick={onLogout}>
            Выйти
          </button>
        </div>
      </header>
      <main className="main-content customer-list">
        <h1>Клиенты сервиса</h1>
        <label className="search-field">
          <Icon name="search" />
          <input
            type="search"
            aria-label="Поиск клиентов"
            placeholder="Название кухни или почта администратора"
            value={query}
            onChange={(e) => {
              setOffset(0);
              setQuery(e.target.value);
            }}
          />
        </label>
        {error ? (
          <ErrorBanner error={error}>
            <button onClick={() => setRetry((n) => n + 1)}>Повторить</button>
          </ErrorBanner>
        ) : !page ? (
          <Loading />
        ) : (
          <>
            {page.items.length ? (
              <ul>
                {page.items.map((c) => (
                  <li key={c.id}>
                    <div>
                      <strong>{c.name}</strong>
                      <span>{c.email || "Администратор не назначен"}</span>
                    </div>
                    <div>
                      <strong>{c.plan === "team" ? "Команда" : "Старт"}</strong>
                      {c.subscription_status !== "free" && (
                        <span>
                          {subscriptionStatus[c.subscription_status] ||
                            "Статус уточняется"}
                        </span>
                      )}
                      <span>
                        {c.members}{" "}
                        {countNoun(c.members, [
                          "участник",
                          "участника",
                          "участников",
                        ])}{" "}
                        · {new Date(c.created_at).toLocaleDateString("ru-RU")}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p>Кухни не найдены.</p>
            )}
            <div className="button-row">
              {offset > 0 && (
                <button
                  className="button secondary"
                  onClick={() => setOffset(Math.max(0, offset - 25))}
                >
                  Предыдущие
                </button>
              )}
              {page.next_offset != null && (
                <button
                  className="button secondary"
                  onClick={() => setOffset(page.next_offset!)}
                >
                  Следующие
                </button>
              )}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
