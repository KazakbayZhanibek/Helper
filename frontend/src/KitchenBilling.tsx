import { useCallback, useEffect, useState } from "react";
import { api, orgApi } from "./api";
import { AccountError, VerifyEmailNotice } from "./AccountFeedback";
import { Icon, Loading } from "./components";
import { countNoun, subscriptionStatus } from "./format";
import type { Organization, Session } from "./types";
import "./customer.css";

type Plan = {
  code: "free" | "team";
  name: string;
  price_label: string;
  recipe_limit: number;
  member_limit: number;
};
type Billing = {
  plan: Plan["code"];
  status: string;
  period_end: string | null;
  cancel_at_period_end: boolean;
  usage: { recipes: number; members: number };
  plans: Plan[];
  can_manage: boolean;
  checkout_available: boolean;
  portal_available: boolean;
  mode: "test" | "live";
};
type Props = {
  org: Organization;
  user?: Session["user"] & { email_verified?: boolean };
};
function Capacity({
  label,
  used,
  limit,
}: {
  label: string;
  used: number;
  limit: number;
}) {
  return (
    <div className="account-capacity">
      <div>
        <span>{label}</span>
        <strong>
          {used} из {limit}
        </strong>
      </div>
      <progress aria-label={label} value={Math.min(used, limit)} max={limit} />
    </div>
  );
}

export function KitchenBilling(props: Props) {
  return <BillingView key={props.org.id} {...props} />;
}
function BillingView({ org, user }: Props) {
  const [billing, setBilling] = useState<Billing | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [action, setAction] = useState<"" | "checkout" | "portal">("");
  const [refreshing, setRefreshing] = useState(false);
  const returned =
    new URLSearchParams(location.search).get("checkout") === "return";
  const [waiting, setWaiting] = useState(returned);
  const endpoint = orgApi(org.id) + "/billing";
  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const result = await api<Billing>(endpoint, { signal });
        if (signal?.aborted) return null;
        setBilling(result);
        setError(null);
        return result;
      } catch (err) {
        if (!signal?.aborted) setError(err);
        return null;
      }
    },
    [endpoint],
  );
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    async function sync() {
      const result = await load(controller.signal);
      if (controller.signal.aborted) return;
      if (returned && result?.plan === "free" && attempts < 5) {
        attempts++;
        timer = setTimeout(sync, 4000);
      } else {
        setWaiting(false);
      }
    }
    void sync();
    function resumed(event: PageTransitionEvent) {
      if (!event.persisted) return;
      setAction("");
      void load(controller.signal);
    }
    window.addEventListener("pageshow", resumed);
    return () => {
      controller.abort();
      clearTimeout(timer);
      window.removeEventListener("pageshow", resumed);
    };
  }, [load, returned]);
  async function open(which: "checkout" | "portal") {
    if (action) return;
    setAction(which);
    setError(null);
    try {
      const result = await api<{ url: string }>(endpoint + "/" + which, {
        method: "POST",
        body: {},
      });
      const url = new URL(result.url);
      if (url.protocol !== "https:" || url.username || url.password)
        throw new Error("Не удалось открыть защищённую страницу оплаты.");
      window.location.assign(url.href);
    } catch (err) {
      setError(err);
      setAction("");
    }
  }
  async function reconcile() {
    if (refreshing) return;
    if (!billing?.can_manage) {
      await load();
      return;
    }
    setRefreshing(true);
    try {
      setBilling(
        await api<Billing>(endpoint + "/refresh", { method: "POST", body: {} }),
      );
      setError(null);
    } catch (err) {
      setError(err);
    } finally {
      setRefreshing(false);
    }
  }
  const current = billing?.plans.find((plan) => plan.code === billing.plan);
  const upgrade = billing?.plans.find((plan) => plan.code === "team");
  const needsEmail = user?.email_verified === false;
  const lapsed = billing?.status === "active" && billing.plan === "free";
  let renewal = "";
  if (billing?.period_end) {
    const stamp = new Date(billing.period_end);
    if (!Number.isNaN(stamp.getTime())) {
      renewal = new Intl.DateTimeFormat("ru", {
        day: "numeric",
        month: "long",
        year: "numeric",
      }).format(stamp);
    }
  }
  return (
    <section className="customer-page">
      <header className="customer-heading">
        <h1>Тариф и оплата</h1>
        <p>Подписка кухни «{org.name}».</p>
      </header>
      <AccountError error={error}>
        <button className="text-button" onClick={() => void load()}>
          Обновить данные
        </button>
      </AccountError>
      {!billing && !error && <Loading label="Открываем подписку…" />}
      {billing && current && (
        <>
          {returned && (
            <p role="status" className="account-operation-note">
              {waiting
                ? "Проверяем подтверждение платежа…"
                : billing.plan === "team"
                  ? "Тариф «Команда» подключён."
                  : "Подтверждение оплаты пока не поступило."}
              {!waiting && billing.plan !== "team" && (
                <>
                  {" "}
                  <button
                    className="text-button"
                    disabled={refreshing}
                    onClick={() => void reconcile()}
                  >
                    {refreshing ? "Проверяем…" : "Обновить оплату"}
                  </button>
                </>
              )}
            </p>
          )}
          <div className="account-management-layout account-billing-grid">
            <section className="account-plan account-current">
              <span className="account-plan-label">Ваш тариф</span>
              <h2>{current.name}</h2>
              <p className="account-plan-state">
                {lapsed
                  ? renewal
                    ? subscriptionStatus.expired
                    : "Ожидаем подтверждения оплаченного периода"
                  : subscriptionStatus[billing.status] || "Статус уточняется"}
              </p>
              {renewal && (
                <p className="account-period">
                  {billing.plan === "team"
                    ? "Текущий период до"
                    : "Дата окончания периода"}{" "}
                  {renewal}.
                </p>
              )}
              {billing.cancel_at_period_end &&
                billing.status !== "cancelled" && (
                  <p className="account-period">Автопродление отключено.</p>
                )}
              <Capacity
                label="Рецепты"
                used={billing.usage.recipes}
                limit={current.recipe_limit}
              />
              <Capacity
                label="Участники кухни"
                used={billing.usage.members}
                limit={current.member_limit}
              />
              {billing.portal_available && billing.can_manage && (
                <button
                  className="button secondary"
                  onClick={() => void open("portal")}
                  disabled={!!action}
                >
                  {action === "portal" ? "Открываем…" : "Управлять подпиской"}{" "}
                  <Icon name="arrow" size={16} />
                </button>
              )}
              {billing.plan === "team" && !billing.portal_available && (
                <p className="field-hint">
                  Платёжный кабинет недоступен для этого аккаунта.
                </p>
              )}
            </section>
            {upgrade && billing.plan !== "team" && (
              <section className="account-plan account-upgrade">
                <span className="account-plan-label">Для большой кухни</span>
                <h2>{upgrade.name}</h2>
                <p className="account-price">{upgrade.price_label}</p>
                <ul className="account-plan-benefits">
                  <li>
                    <Icon name="book" />
                    {upgrade.recipe_limit}{" "}
                    {countNoun(upgrade.recipe_limit, [
                      "рецепт",
                      "рецепта",
                      "рецептов",
                    ])}
                  </li>
                  <li>
                    <Icon name="plus" />
                    {upgrade.member_limit}{" "}
                    {countNoun(upgrade.member_limit, [
                      "участник",
                      "участника",
                      "участников",
                    ])}
                  </li>
                </ul>
                {billing.can_manage &&
                billing.checkout_available &&
                !needsEmail ? (
                  <>
                    <button
                      className="button primary"
                      onClick={() => void open("checkout")}
                      disabled={!!action}
                    >
                      {action === "checkout"
                        ? "Переходим к оплате…"
                        : "Перейти к оплате"}{" "}
                      <Icon name="arrow" size={16} />
                    </button>
                    {billing.mode === "test" && (
                      <p className="field-hint">
                        Тестовая касса. Используйте тестовую карту.
                      </p>
                    )}
                  </>
                ) : (
                  <p className="account-payment-hint">
                    {!billing.can_manage
                      ? "Тариф выбирает администратор кухни."
                      : needsEmail
                        ? "Для оформления подписки подтвердите почту вашего аккаунта."
                        : "Оформление подписки пока недоступно."}
                  </p>
                )}
              </section>
            )}
          </div>
          {needsEmail && user && <VerifyEmailNotice email={user.email} />}
        </>
      )}
    </section>
  );
}
