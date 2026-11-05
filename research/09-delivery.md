# Воспроизводимые проверки и CI

Независимый агент №9 · 29 октября 2026 · локальный первый срез.

## Добавлено

- `.github/workflows/checks.yml`: два backend-запуска — SQLite и PostgreSQL 17; отдельная сборка frontend с Node.js 24.
- Backend на Python 3.12 устанавливается из `requirements.lock`; frontend — через `npm ci` из `frontend/package-lock.json`.
- Backend job выполняет Django system check, проверку отсутствующих миграций, миграции пустой базы, интеграционные тесты и проверку исходного плана.
- Триггеры из `0002_domain_guards.py` попадут в тестовую PostgreSQL-базу через обычные миграции pytest-django. Тесты используют rollback транзакций и временный media-каталог; демонстрационная база и реальные записи им не нужны.
- У workflow только `contents: read`; сохранение Git credentials отключено. Используются `push`, `pull_request` и ручной запуск. Деплоя, публикации и `pull_request_target` нет.
- `Makefile`: `prepare`, `dev`, `check`, `check-plan`, `check-backend`, `build`. По умолчанию используется `.venv/bin/python`; подготовка и запуск делегированы `scripts/dev.py`.

Пароль PostgreSQL в workflow предназначен только для одноразового service-контейнера CI. Он не является паролем пользователя или рабочего сервера.

## Фактически проверено

| Проверка | Результат |
|---|---|
| `make check-plan` | PASS: 72 задачи, 42 требования, 104 локальные ссылки |
| `manage.py check` | PASS: 0 issues |
| `makemigrations --check --dry-run` | PASS: No changes detected |
| `.venv/bin/python -m pytest -q` | PASS: 33 passed in 0.91s на SQLite |
| YAML parser и наличие jobs/database matrix | PASS |
| `make --dry-run prepare dev build` | PASS: корректные команды |
| `make build` | TypeScript прошёл; Vite остановился на отсутствующем `src/styles.css`, который параллельно завершал frontend-агент |

`make build` уже использует созданный основным агентом `scripts/vite-run.mjs`. Ошибка символа `#` в пути рабочего каталога в этом прогоне больше не возникала.

## Границы результата

Workflow подготовлен локально, не отправлен в remote и не выполнялся на GitHub. PostgreSQL, его триггеры и конкурентные запросы локально не проверялись. Наличие PostgreSQL job не означает успешный прогон. Перед выпуском нужно получить зелёный CI и отдельно проверить конкурентную публикацию/идемпотентность на PostgreSQL. Полный `make check` требует повторного успешного frontend build после завершения CSS.

Версии официальных GitHub Actions проверены по опубликованным релизам: [checkout 7.0.1](https://github.com/actions/checkout/releases), [setup-python 7.0.0](https://github.com/actions/setup-python/releases), [setup-node 7.1.0](https://github.com/actions/setup-node/releases). Теги версий зафиксированы в workflow; service image зафиксирован на ветке PostgreSQL 17.
