import { useEffect, useState } from "react";
import { api, libraryPath } from "./api";
import { Brand, Icon, ErrorBanner, Loading } from "./components";
import { countNoun } from "./format";
import type { Navigate, Session } from "./types";

type Plan = {
  code: string;
  name: string;
  recipe_limit: number;
  member_limit: number;
  price_label: string;
};

export function Home({
  session,
  navigate,
}: {
  session: Session | null;
  navigate: Navigate;
}) {
  const kitchen = session?.organizations[0];
  const operator = !kitchen && session?.user.is_operator;
  const entry = kitchen
    ? libraryPath(kitchen.id)
    : operator
      ? "/customers"
      : "/register";
  const [plans, setPlans] = useState<Plan[]>([]);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    api<{ plans: Plan[] }>("/billing/plans", { signal: abort.signal })
      .then((value) => setPlans(value.plans))
      .catch((e) => {
        if (!abort.signal.aborted) setError(e.message);
      });
    return () => abort.abort();
  }, [retry]);
  return (
    <div className="public-site recipe-site product-home">
      <a className="skip-link" href="#home-main">
        Перейти к содержимому
      </a>
      <header className="public-header">
        <Brand />
        <nav aria-label="Главное меню">
          <a href="#plans">Тарифы</a>
          <a className="button secondary" href={session ? entry : "/login"}>
            {kitchen ? "Моя кухня" : operator ? "Клиенты" : "Войти"}
          </a>
        </nav>
      </header>
      <main id="home-main">
        <section className="sales-hero">
          <div>
            <h1>
              Ваша кухня.
              <br />
              Ваша книга
              <br />
              <em>рецептов.</em>
            </h1>
            <p>
              Сохраните приготовление на видео, добавьте продукты и покажите
              каждый шаг команде.
            </p>
            <div className="button-row">
              <button
                className="button primary"
                onClick={() => navigate(entry)}
              >
                {kitchen
                  ? "Открыть книгу"
                  : operator
                    ? "Клиенты сервиса"
                    : "Создать кухню бесплатно"}{" "}
                <Icon name="arrow" size={17} />
              </button>
            </div>
          </div>
          <a className="home-example" href="/example">
            <div className="home-example-image">
              <img
                src="/examples/vegetables/vegetables.jpg"
                width="960"
                height="540"
                alt="Нарезанный кабачок и сладкий перец на разделочной доске"
                fetchPriority="high"
              />
              <span aria-hidden="true">
                <Icon name="play" size={23} />
              </span>
            </div>
            <div className="home-example-caption">
              <div>
                <small>Видеорецепт · 3 шага</small>
                <strong>Овощная нарезка из кабачка и перца</strong>
              </div>
              <Icon name="arrow" size={22} />
            </div>
          </a>
        </section>
        <section id="plans" className="sales-plans">
          <h2>Тарифы для кухни</h2>
          {error ? (
            <ErrorBanner error={error}>
              <button
                className="text-button"
                onClick={() => {
                  setError("");
                  setRetry((n) => n + 1);
                }}
              >
                Загрузить тарифы
              </button>
            </ErrorBanner>
          ) : !plans.length ? (
            <Loading label="Загружаем тарифы…" />
          ) : (
            <div className="plan-list">
              {plans.map((plan) => (
                <div key={plan.code}>
                  <h3>{plan.name}</h3>
                  <p>
                    До {plan.recipe_limit} рецептов · {plan.member_limit}{" "}
                    {countNoun(plan.member_limit, [
                      "участник",
                      "участника",
                      "участников",
                    ])}
                  </p>
                  <strong>{plan.price_label}</strong>
                </div>
              ))}
            </div>
          )}
        </section>
      </main>
      <footer className="public-footer">
        <Brand />
        <a href="/example">
          Открытый видеорецепт <Icon name="arrow" size={16} />
        </a>
      </footer>
    </div>
  );
}
