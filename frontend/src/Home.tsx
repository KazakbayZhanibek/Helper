import { Brand, Icon } from "./components";
import { libraryPath } from "./api";
import type { Navigate, Session } from "./types";

export function Home({
  session,
  navigate,
}: {
  session: Session | null;
  navigate: Navigate;
}) {
  const destination = session?.organizations[0]
    ? libraryPath(session.organizations[0].id)
    : "/login";
  const enterLabel = session ? "Открыть пространство" : "Войти в пространство";
  return (
    <div className="public-site">
      <a className="skip-link" href="#home-main">
        Перейти к содержимому
      </a>
      <header className="public-header">
        <Brand />
        <nav aria-label="О продукте">
          <a href="#how-it-works">Как это работает</a>
          <button
            className="button secondary"
            onClick={() => navigate(destination)}
          >
            {enterLabel}
            <Icon name="arrow" size={16} />
          </button>
        </nav>
      </header>
      <main id="home-main">
        <section className="home-hero" aria-labelledby="home-title">
          <div className="home-intro">
            <span className="eyebrow">Опыт команды — в понятных шагах</span>
            <h1 id="home-title">
              Покажите работу.
              <br />
              Сохраните мастерство.
            </h1>
            <p className="home-lead">
              Превратите запись рабочего процесса в инструкцию для команды. ИИ
              подготовит черновик, а мастер проверит детали по исходному видео.
            </p>
            <button
              className="button primary"
              onClick={() => navigate(destination)}
            >
              {enterLabel}
              <Icon name="arrow" size={18} />
            </button>
            <p className="home-pilot">
              Рабочий пилот · видео с камеры или готовый файл
            </p>
          </div>
          <div
            className="home-example"
            aria-label="Пример структуры будущей инструкции"
          >
            <div className="home-example-head">
              <span className="eyebrow">Из записи в инструкцию</span>
              <span className="home-example-tag">Пример структуры</span>
            </div>
            <div className="home-film">
              <div className="home-film-play">
                <Icon name="play" size={27} />
              </div>
              <div>
                <strong>Рабочий процесс</strong>
                <span>Исходная запись остаётся рядом</span>
              </div>
              <div className="home-film-timeline" aria-hidden="true">
                <span />
                <span />
                <span />
                <span />
                <span />
                <span />
                <span />
                <span />
              </div>
            </div>
            <ol className="home-example-steps">
              <li>
                <span>01</span>
                <div>
                  <strong>Подготовить рабочее место</strong>
                  <small>Сверить с кадрами записи</small>
                </div>
              </li>
              <li>
                <span>02</span>
                <div>
                  <strong>Выполнить операцию</strong>
                  <small>Уточнить движение и параметры</small>
                </div>
              </li>
              <li>
                <span>03</span>
                <div>
                  <strong>Проверить результат</strong>
                  <small>Добавить критерии мастера</small>
                </div>
              </li>
            </ol>
            <div className="home-human-check">
              <Icon name="eye" size={19} />
              <span>Публикация после проверки человеком</span>
            </div>
          </div>
        </section>
        <section
          className="home-process"
          id="how-it-works"
          aria-labelledby="process-title"
        >
          <div className="home-section-heading">
            <span className="eyebrow">Один сайт для всей работы</span>
            <h2 id="process-title">
              От первого кадра
              <br />
              до инструкции для коллег.
            </h2>
            <p>
              Запись, черновик и утверждённая версия живут в одном рабочем
              пространстве команды.
            </p>
          </div>
          <ol className="home-process-list">
            <li>
              <span>01</span>
              <div>
                <h3>Запишите процесс</h3>
                <p>
                  Включите камеру или загрузите готовое видео. Снимайте руки,
                  инструменты и результат действия.
                </p>
              </div>
            </li>
            <li>
              <span>02</span>
              <div>
                <h3>Получите черновик</h3>
                <p>
                  ИИ последовательно разбирает запись по кадрам. Неясные
                  фрагменты пересматривает подробнее в пределах заданного
                  лимита.
                </p>
              </div>
            </li>
            <li>
              <span>03</span>
              <div>
                <h3>Добавьте опыт мастера</h3>
                <p>
                  Проверьте действия и время, уточните параметры. Движения между
                  кадрами и скрытые операции могут остаться незамеченными.
                </p>
              </div>
            </li>
            <li>
              <span>04</span>
              <div>
                <h3>Откройте инструкцию команде</h3>
                <p>
                  После проверки опубликуйте версию с пошаговым описанием и
                  исходным видео.
                </p>
              </div>
            </li>
          </ol>
        </section>
        <section className="home-budget" aria-labelledby="budget-title">
          <div>
            <span className="eyebrow light">Расходы под контролем</span>
            <h2 id="budget-title">
              Сначала лимит.
              <br />
              Затем анализ.
            </h2>
          </div>
          <div>
            <p>
              Перед запуском видно количество фрагментов и доступных обращений к
              ИИ. Когда бюджета недостаточно, анализ останавливается и
              показывает, что осталось проверить.
            </p>
            <p>
              В тестовом режиме подписки действует лимит запросов. При работе
              через поддерживаемый API расходы учитываются отдельно в долларах.
            </p>
          </div>
        </section>
        <section className="home-next" aria-labelledby="next-title">
          <div>
            <span className="eyebrow">Начните с одного процесса</span>
            <h2 id="next-title">
              То, что знает мастер,
              <br />
              сможет повторить команда.
            </h2>
          </div>
          <button
            className="button primary"
            onClick={() => navigate(destination)}
          >
            {enterLabel}
            <Icon name="arrow" size={18} />
          </button>
        </section>
      </main>
      <footer className="public-footer">
        <Brand />
        <p>Записать. Проверить. Передать опыт.</p>
        <a href="#home-title">Наверх ↑</a>
      </footer>
    </div>
  );
}
