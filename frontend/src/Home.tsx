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
  const organization = session?.organizations[0];
  const destination = organization ? libraryPath(organization.id) : "/login";
  const enterLabel = organization
    ? "Открыть книгу рецептов"
    : "Войти в книгу рецептов";

  return (
    <div className="public-site recipe-site">
      <a className="skip-link" href="#home-main">
        Перейти к содержимому
      </a>
      <header className="public-header">
        <Brand />
        <nav aria-label="Главное меню">
          <a href="#how-it-works">Как это работает</a>
          <button
            className="button secondary"
            onClick={() => navigate(destination)}
          >
            {organization ? "Мои рецепты" : "Войти"}{" "}
            <Icon name="arrow" size={16} />
          </button>
        </nav>
      </header>
      <main id="home-main">
        <section className="home-hero recipe-hero" aria-labelledby="home-title">
          <div className="home-intro">
            <h1 id="home-title">
              Ваши рецепты
              <br />
              из видео
            </h1>
            <p className="home-lead">
              Снимите, как вы готовите. ИИ предложит шаги с фотографиями из
              записи. Добавьте ингредиенты и порции, чтобы по вашему рецепту мог
              приготовить другой человек.
            </p>
            <div className="button-row">
              <button
                className="button primary"
                onClick={() => navigate(destination)}
              >
                {enterLabel} <Icon name="arrow" size={18} />
              </button>
              <a className="button secondary" href="/example">
                Посмотреть пример
              </a>
            </div>
          </div>
          <figure className="recipe-example">
            <img
              src="/hero-kneading.jpg"
              width="1120"
              height="630"
              fetchPriority="high"
              alt="Рука прижимает тесто к присыпанной мукой поверхности"
            />
            <figcaption>
              <span className="recipe-image-kicker">
                Пример шага · Замес теста
              </span>
              <h2>Прижмите тесто ладонью</h2>
              <p>
                Поднимите край теста, поверните кусок и снова прижмите его к
                столу.
              </p>
              <a
                className="recipe-credit"
                href="https://www.pexels.com/video/a-person-kneading-the-dough-7236946/"
                target="_blank"
                rel="noreferrer"
              >
                Кадр из видео Eva Bronzini / Pexels
              </a>
            </figcaption>
          </figure>
        </section>
        <section
          className="home-process recipe-process"
          id="how-it-works"
          aria-labelledby="process-title"
        >
          <div className="home-section-heading">
            <h2 id="process-title">
              Как составить
              <br />
              видеорецепт
            </h2>
          </div>
          <ol className="home-process-list">
            <li>
              <span aria-hidden="true">01</span>
              <div>
                <h3>Снимите приготовление</h3>
                <p>
                  Загрузите готовое видео или включите камеру. Держите руки и
                  продукты в кадре, а количество и время называйте вслух.
                </p>
              </div>
            </li>
            <li>
              <span aria-hidden="true">02</span>
              <div>
                <h3>Проверьте шаги и фотографии</h3>
                <p>
                  Сверьте черновик ИИ с записью. Уточните движения, внесите
                  ингредиенты и укажите, сколько порций получится. Лимит
                  распознавания виден до запуска.
                </p>
              </div>
            </li>
            <li>
              <span aria-hidden="true">03</span>
              <div>
                <h3>Откройте рецепт своей кухне</h3>
                <p>
                  После проверки опубликуйте рецепт. Участники смогут открыть
                  его с телефона и сверяться с фотографиями во время готовки.
                </p>
              </div>
            </li>
          </ol>
        </section>
      </main>
      <footer className="public-footer">
        <Brand />
        <a href="#home-main">Наверх ↑</a>
      </footer>
    </div>
  );
}
