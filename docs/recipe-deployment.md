# Публикация «Рецептов мастера»

Комплект рассчитан на сервер с Docker Engine, Docker Compose v2 и постоянным диском. Caddy обслуживает HTTPS, PostgreSQL хранит рецепты, подписки и учётные записи. Сервис `worker` обрабатывает видео, а `mail` доставляет письма регистрации, приглашений и восстановления пароля. Видео и фотографии хранятся в закрытом томе.

## Сервер и первый запуск

1. Направьте DNS-запись вашего домена на сервер. Откройте входящие TCP-порты 80 и 443. Для HTTP/3 также можно открыть UDP 443.
2. Получите исходники проекта и перейдите в корень репозитория. Создайте отдельный файл конфигурации. При первом запуске следующий пример запрашивает имя домена и генерирует два случайных секрета. Он завершится без записи, если файл уже существует.

```sh
(
  set -eu
  set -C
  umask 077
  printf 'Домен сайта (например, recipes.example.org): '
  read -r recipe_hostname
  {
    printf 'RECIPE_HOST=%s\n' "$recipe_hostname"
    printf 'RECIPE_SECRET_KEY=%s\n' "$(openssl rand -hex 48)"
    printf 'RECIPE_DB_PASSWORD=%s\n' "$(openssl rand -hex 32)"
  } > .env.production
)
```

3. Для регистрации клиентов подключите почтовый ящик или транзакционный SMTP-сервис. Оператор SMTP выдаёт адрес сервера, порт, имя пользователя и пароль. Подтвердите домен отправителя у почтового провайдера. Запишите фактические значения в закрытый `.env.production`. Для STARTTLS обычно используется следующий вариант.

```dotenv
RECIPE_EMAIL_FROM=recipes@example.org
RECIPE_SMTP_HOST=smtp.example.org
RECIPE_SMTP_PORT=587
RECIPE_SMTP_USER=recipes@example.org
RECIPE_SMTP_PASSWORD=
RECIPE_SMTP_TLS=1
RECIPE_SMTP_SSL=0
```

   Заполните `RECIPE_SMTP_PASSWORD` выданным паролем. Если провайдер использует TLS сразу при соединении на порту 465, задайте `RECIPE_SMTP_PORT=465`, `RECIPE_SMTP_SSL=1` и `RECIPE_SMTP_TLS=0`. Ссылки в письмах ведут на HTTPS-домен из `RECIPE_HOST`.

4. Запустите сервисы. Миграции выполнятся отдельным контейнером до запуска приложения.

```sh
docker compose --env-file .env.production config --quiet
docker compose --env-file .env.production up --build -d
docker compose --env-file .env.production ps -a
```

5. Откройте `https://ваш-домен/register` и зарегистрируйтесь с почтовым ящиком, который можете проверить. Подтвердите адрес из письма, пригласите второго пользователя через «Команда» и примите приглашение в другой сессии браузера. Пройдите `/forgot-password` и войдите с новым паролем. Владелец кухни управляет своей командой и подпиской.

6. Для сотрудника, который обслуживает весь сервис, создайте отдельную операторскую учётную запись в консоли сервера.

```sh
docker compose --env-file .env.production exec api python manage.py createsuperuser
```

   Django запросит email, имя и пароль. После входа откройте `/customers` для поиска клиентских кухонь по названию или email администратора. Привилегия суперпользователя даёт доступ ко всему списку клиентов.

7. Создайте рецепт под владельцем кухни, загрузите короткое видео и дождитесь превью. После публикации откройте рецепт под читателем. Для диагностики доступны `/api/v1/health/live` и журналы `docker compose --env-file .env.production logs --tail=80 api mail worker web`. При временном сбое SMTP задача остаётся в базе для повторной отправки с увеличивающимся интервалом. После исправления долгого сбоя пользователь может запросить новое письмо.

## Подписки

Каждая новая кухня получает тариф «Старт». Для приёма оплаты создайте подписочный продукт в Dodo Payments, подключите подписанный webhook и пройдите оформление в тестовом режиме. В `.env.production` используются переменные `RECIPE_DODO_MODE`, `RECIPE_DODO_BUSINESS_ID`, `RECIPE_DODO_PRODUCT_ID`, `RECIPE_DODO_API_KEY` и `RECIPE_DODO_WEBHOOK_SECRET`. После изменения конфигурации выполните `docker compose --env-file .env.production up -d`. Пошаговая процедура настройки и перехода на реальные платежи описана в [руководстве по подпискам](customer-billing.md).

Перед публикацией реальных материалов проверьте восстановление ваших данных по процедуре ниже и убедитесь, что ежедневное резервное копирование настроено на вашем сервере.

## Обработка и расходы

При первом старте доступны ручная подготовка, загрузка, камера, обработка видео, проверка и публикация. Чтобы предложить бесплатное распознавание кадров, добавьте в закрытый файл `.env.production` строки `RECIPE_EXTERNAL_AI=1` и `RECIPE_OPENROUTER_KEY=ваш_ключ`. Затем пересоздайте приложение. Администратор кухни с подтверждённой почтой может разрешить внешний анализ в интерфейсе кухни.

```sh
docker compose --env-file .env.production up -d
```

Образ использует профиль `openrouter/free`, предел расходов на платные модели равен нулю. Для анализа по-прежнему требуется согласие автора на отправку кадров. При исчерпании бесплатной квоты автор может продолжить ручную разметку. Распознавание речи через whisper.cpp требует отдельно смонтированной локальной модели и исполняемого файла. Ключ и пароль кухни относятся к разным системам.

## Обновление и резервная копия

Перед обновлением остановите запись и обработку. При остановленных приложениях дамп базы и копия каталога медиа относятся к одному состоянию. Пример создаёт файлы с закрытыми правами. Храните копии вне этого сервера, а конфигурацию с секретами храните отдельно в защищённом месте.

```sh
umask 077
backup_dir="../recipe-backup-$(date +%Y%m%d-%H%M%S)"
mkdir "$backup_dir"
docker compose --env-file .env.production stop web mail worker api
docker compose --env-file .env.production exec -T db pg_dump -U recipes -d recipes --format=custom > "$backup_dir/recipes.dump"
docker compose --env-file .env.production run --rm --no-deps -T api tar -C /var/lib/kitchen/media -czf - . > "$backup_dir/media.tgz"
docker compose --env-file .env.production up -d
```

После получения новой версии проекта выполните `docker compose --env-file .env.production up --build -d` и проверьте страницу входа, выдачу рецептов и последнее видео. Обычное завершение `docker compose --env-file .env.production down` сохраняет именованные тома. Для восстановления разверните ту же версию на отдельном сервере со свежими пустыми томами и копией конфигурации. Перед проверкой на временном домене укажите его в `RECIPE_HOST`.

```sh
docker compose --env-file .env.production build
docker compose --env-file .env.production up -d --wait db
docker compose --env-file .env.production exec -T db pg_restore --exit-on-error --no-owner -U recipes -d recipes < "$backup_dir/recipes.dump"
docker compose --env-file .env.production run --rm --no-deps -T api tar -C /var/lib/kitchen/media -xzf - < "$backup_dir/media.tgz"
docker compose --env-file .env.production up -d
```

HTTPS обеспечивается Caddy, а порт API доступен внутри сети контейнеров. Параметр Django `DJANGO_TRUST_HTTPS_PROXY=1` предполагает, что только доверенный прокси может передать заголовок исходной схемы запроса. При изменении топологии сохраните это ограничение. [HTTPS и прокси в Django](https://docs.djangoproject.com/en/5.2/ref/settings/#secure-proxy-ssl-header) · [Заголовки reverse_proxy в Caddy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#headers)

В GitHub Actions собираются оба образа и запускается серверный комплект. Проверка проходит регистрацию двух клиентов через HTTPS, подтверждает адрес по письму от отдельного обработчика, сверяет бесплатный тариф, принимает приглашение и отзывает доступ. Одновременно проверяются CSRF-cookie, выдача медиа и изоляция кухонь. Письма этого прогона остаются в тестовом контейнере.
