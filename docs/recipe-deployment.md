# Публикация «Рецептов мастера»

Комплект рассчитан на один сервер с Docker Engine, Docker Compose v2 и постоянным диском. Caddy принимает запросы на 80 и 443, получает сертификат для домена, отдаёт сайт и передаёт запросы API в Gunicorn. PostgreSQL хранит рецепты, проверенные шаги, учётные записи и очередь. Один обработчик разбирает видео через FFmpeg. Исходники и фотографии лежат в отдельном закрытом томе.

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

3. Запустите сервисы. Миграции выполнятся отдельным контейнером до запуска API и обработчика.

```sh
docker compose --env-file .env.production config --quiet
docker compose --env-file .env.production up --build -d
docker compose --env-file .env.production ps -a
```

4. Создайте кухню и её администратора. Команда запросит пароль в терминале и выведет идентификатор кухни. Для следующих участников используйте этот идентификатор и одну из ролей `author`, `reviewer`, `reader`, `admin`.

```sh
docker compose --env-file .env.production exec api python manage.py kitchen_member --new-kitchen "Моя кухня" --email chef@example.org --name "Повар" --role admin
docker compose --env-file .env.production exec api python manage.py kitchen_member --kitchen-id UUID --email cook@example.org --name "Автор" --role author
```

5. Откройте `https://ваш-домен/login`, войдите, создайте рецепт, загрузите короткое видео и дождитесь превью. После проверки и публикации откройте рецепт с ролью читателя. Для диагностики доступны `/api/v1/health/live` и журналы `docker compose --env-file .env.production logs --tail=80 api worker web`.

Перед публикацией реальных материалов проверьте восстановление ваших данных по процедуре ниже и убедитесь, что ежедневное резервное копирование настроено на вашем сервере.

## Обработка и расходы

При первом старте доступны ручная подготовка, загрузка, камера, обработка видео, проверка и публикация. Внешний ИИ включается отдельно. Чтобы разрешить распознавание кадров, добавьте в закрытый файл `.env.production` строки `RECIPE_EXTERNAL_AI=1` и `RECIPE_OPENROUTER_KEY=ваш_ключ`. Затем пересоздайте приложение и разрешите внешний анализ выбранной кухне.

```sh
docker compose --env-file .env.production up -d
docker compose --env-file .env.production exec api python manage.py configure_ai --organization UUID --allow-external
```

Образ использует профиль `openrouter/free`, предел расходов на платные модели равен нулю. Для анализа по-прежнему требуется согласие автора на отправку кадров. При исчерпании бесплатной квоты автор может продолжить ручную разметку. Распознавание речи через whisper.cpp требует отдельно смонтированной локальной модели и исполняемого файла. Ключ и пароль кухни относятся к разным системам.

## Обновление и резервная копия

Перед обновлением остановите запись и обработку. При остановленных приложениях дамп базы и копия каталога медиа относятся к одному состоянию. Пример создаёт файлы с закрытыми правами. Храните копии вне этого сервера, а конфигурацию с секретами храните отдельно в защищённом месте.

```sh
umask 077
backup_dir="../recipe-backup-$(date +%Y%m%d-%H%M%S)"
mkdir "$backup_dir"
docker compose --env-file .env.production stop web worker api
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

Проверка в GitHub Actions собирает оба образа, запускает PostgreSQL, миграции, API, обработчик и Caddy, проверяет локальный сертификат, перенаправление с HTTP, прямую ссылку на форму рецепта, CSRF-cookie, закрытые медиа и готовность очереди.
