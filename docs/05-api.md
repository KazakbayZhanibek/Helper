# Контракты API

[К полному плану](00-overview.md)

## Общие правила

Base path `/api/v1`. JSON UTF-8, snake_case. UTC timestamps RFC3339 с `Z`; интервалы видео в миллисекундах. В API используется UUID организации, не произвольное название. Все tenant-routes начинаются `/orgs/{org_id}/`.

Сессионная cookie, CSRF в заголовке `X-CSRFToken` для POST/PUT/PATCH/DELETE. Login тоже защищён CSRF. Каждый ответ содержит `X-Request-ID`. Сервер не принимает роль, стоимость, состояние job или storage_key от клиента как доверенные значения.

Объект из другой организации возвращает 404 без подтверждения существования. Для известной организации и недопустимого действия роли — 403. Отсутствие сессии — нормализованная 401 согласно 03. Списки: `items`, `next_cursor`, `total` только там, где подсчёт дёшев и разрешён; cursor opaque, page size по умолчанию 20, максимум 100.

Роли в таблицах: U — signed-in, M — любой активный участник, W — author/reviewer/admin, R — reviewer/admin, A — admin. W имеет доступ к рабочим материалам своей организации; приватные между авторами черновики откладываются и это должно быть согласовано с пилотом.

## Авторизация и участники

| Метод и маршрут | Роль | Вход и результат |
|---|---|---|
| GET `/auth/csrf` | — | 200, установка CSRF cookie; никаких иных изменений |
| POST `/auth/login` | — | email/password; 200 user + organizations; generic error при неверных данных |
| POST `/auth/logout` | U | 204, отзыв текущей сессии |
| GET `/auth/me` | U | Профиль, активные membership и возможности |
| POST `/auth/password-reset` | — | email; всегда нейтральный 202 |
| POST `/auth/password-reset/confirm` | — | token/new_password; 204 либо безопасная ошибка |
| POST `/invitations/accept` | —/U | token, имя/пароль нового пользователя или действующая сессия; 200 membership |
| GET `/orgs/{org}/members` | A | Участники и статусы |
| POST `/orgs/{org}/invitations` | A | email, role; 201, отправка после commit |
| DELETE `/orgs/{org}/invitations/{id}` | A | 204, идемпотентный отзыв |
| PATCH `/orgs/{org}/members/{id}` | A | role/is_active; 200; последний admin защищён |
| GET/PATCH `/orgs/{org}/settings` | A | Разрешённые настройки; API key никогда не возвращается |

## Инструкции и загрузка

| Метод и маршрут после `/orgs/{org}` | Роль | Вход и результат |
|---|---|---|
| GET `/instructions` | M | q, language, status, cursor; читателю только current publications |
| POST `/instructions` | W | title, purpose, language; Idempotency-Key; 201 draft |
| GET `/instructions/{id}` | M | Reader получает current snapshot; W дополнительно workflow metadata |
| POST `/instructions/{id}/uploads` | W | filename, size_bytes, language, rights_confirmed; 201 upload_id/limits |
| POST `/uploads/{upload_id}/content` | W | multipart file; streaming; 202 asset_id/job_id |
| DELETE `/uploads/{upload_id}` | W | 204 отмена незавершённой передачи; completed требует обычного удаления source |
| GET `/jobs/{id}` | W | state, stage, error_code, retry_allowed, manual_allowed |
| POST `/jobs/{id}/retry` | W | Idempotency-Key; 202; нет второго активного одинакового задания |
| POST `/jobs/{id}/cancel` | W | 202 cancel_requested; окончательный state виден отдельно |
| GET `/assets/{id}/playback` | M | Авторизация, внутренний redirect Nginx; поддержка Range |
| GET `/assets/{id}/thumbnail` | M | Та же проверка полномочий на instruction |
| GET `/assets/{id}/transcript` | W | raw/corrected segments, revision |
| PATCH `/transcripts/{id}` | W | If-Match; исправления текста по segment_id; новая revision |

Reader не получает необработанный оригинал и исходную расшифровку. В D1 endpoint скачивания оригинала не нужен; обслуживание оригинала — контролируемая операция автора через отдельное последующее решение.

## Редактирование и проверка

| Метод и маршрут после `/orgs/{org}/instructions/{id}` | Роль | Контракт |
|---|---|---|
| GET `/working-copy` | W | current revision, editable, fields, steps, warnings, ETag |
| PUT `/working-copy` | W | Полный небольшой документ; If-Match обязателен; 200 + новый ETag |
| POST `/draft-proposals` | W | transcript_id/revision и base working revision; 202 job |
| GET `/draft-proposals/{proposal_id}` | W | Предложение, предупреждения и версии источников |
| POST `/draft-proposals/{proposal_id}/apply` | W | If-Match; заменяет шаги только после явного подтверждения, stale → 409 |
| POST `/submit-review` | W | If-Match; 200 workflow in_review |
| POST `/withdraw-review` | W | If-Match; 200 draft; инвалидировать review confirmations |
| POST `/review-comments` | R | step_id optional, text, blocking; 201 |
| POST `/review-comments/{comment_id}/resolve` | R | resolution; 200; влияет на revision |
| POST `/review/check-step` | R | step_id, checked, If-Match; обновляет отметку и revision |
| POST `/return-to-draft` | R | If-Match, reason; 200 draft |
| POST `/publish` | R | If-Match, confirmations, Idempotency-Key; 201 version |
| POST `/create-draft` | W | source_version_id; 201 editable working copy или 409 existing draft |
| GET `/versions` | W | История версий без edit capability |
| GET `/versions/{version_id}` | W | Конкретный immutable snapshot |
| POST `/withdraw-publication` | R | reason, current_version_id; 200 hidden; stale → 409 |
| DELETE `` | A | Idempotency-Key; 202 deletion_id, доступ скрыт немедленно |

Читатель получает snapshot только через основной GET инструкции; arbitrary version endpoint остаётся W. Для чтения открытой версии клиент сохраняет version_id, а последующий GET сообщает, появилась ли новая версия. Это не разрешает reader читать произвольную старую версию после отзыва.

## Обратная связь и эксплуатация

POST `/orgs/{org}/instructions/{id}/feedback`: M, version_id, step_id optional, category, body, Idempotency-Key → 201. GET `/orgs/{org}/feedback`: W. PATCH `/orgs/{org}/feedback/{id}`: W, state/resolution. GET `/orgs/{org}/deletions/{id}`: A. GET `/health/live`: без auth, минимальный 200. GET `/health/ready`: только внутренний мониторинг/ограниченный доступ, проверка зависимостей без секретов.

## Схема сохранения рабочей копии

```json
{
  "title": "Комплектация набора",
  "purpose": "Собрать и проверить комплект перед упаковкой",
  "language": "ru",
  "steps": [
    {
      "id": "2e530a4c-ff97-421e-94e2-7a0bdc5ee741",
      "title": "Проверьте состав набора",
      "description": "Сравните предметы с показанным в записи списком.",
      "start_ms": 12000,
      "end_ms": 24000,
      "evidence_segment_ids": ["78b91d6a-2978-4a9f-8aac-e0f2268de8e0"]
    }
  ]
}
```

Новый шаг имеет `id: null`; сервер назначает UUID и возвращает его. Порядок массива задаёт ordinal. Клиент не может прислать checked_by/origin=ai/published_by. Metadata происхождения обновляется service исходя из операции. Ограничение JSON body 256 KiB; лимиты вложенности и количества шагов проверяются до сохранения.

## Версии и повторные запросы

GET working copy возвращает `ETag: "wc-<uuid>-r17"`. PUT, review и publish требуют `If-Match` этого значения. Отсутствие — 428 PRECONDITION_REQUIRED; несовпадение — 412 REVISION_CONFLICT. Сервер возвращает actual_revision и безопасный reload URL, но не стирает клиентскую форму.

Idempotency-Key требуется для create instruction, create upload, retry, generate, publish, feedback и delete. Scope: организация + actor + route. Повтор с тем же payload получает прежний результат. Пока первая операция выполняется — 409 REQUEST_IN_PROGRESS с retry_after. Другой payload при том же key — 409 IDEMPOTENCY_KEY_REUSED. Для streaming content session_id сам задаёт однократность; второй completed upload не заменяет оригинал.

## Единый формат ошибки

```json
{
  "error": {
    "code": "REVISION_CONFLICT",
    "message": "Инструкцию изменили в другом окне. Загрузите актуальную версию.",
    "field_errors": {},
    "request_id": "a850ad99-18a2-474a-8688-c21a4e258788",
    "retryable": false,
    "details": {"actual_revision": 18}
  }
}
```

| HTTP | Коды |
|---|---|
| 400 | VALIDATION_ERROR, INVALID_TIME_RANGE, INVALID_STATE, UNSUPPORTED_MEDIA |
| 401 | AUTH_REQUIRED, SESSION_EXPIRED |
| 403 | FORBIDDEN, CSRF_FAILED, ORGANIZATION_SUSPENDED |
| 404 | NOT_FOUND, INVITATION_INVALID; чужие ID не раскрывать |
| 409 | REQUEST_IN_PROGRESS, IDEMPOTENCY_KEY_REUSED, ACTIVE_JOB_EXISTS, STALE_PROPOSAL, LAST_ADMIN |
| 412/428 | REVISION_CONFLICT / PRECONDITION_REQUIRED |
| 413 | FILE_TOO_LARGE, BODY_TOO_LARGE |
| 422 | MEDIA_REJECTED, MODEL_OUTPUT_INVALID |
| 429 | RATE_LIMITED, AI_BUDGET_EXCEEDED, STORAGE_QUOTA_EXCEEDED; Retry-After только когда известен |
| 503 | DEPENDENCY_UNAVAILABLE; не возвращать сырой stack trace |

Временные provider failures записываются в job; polling может вернуть 200 со state failed, поскольку сам HTTP-запрос прошёл успешно. Клиент не должен ожидать HTTP 500 для каждого неудачного видео.

## Контрактная проверка

OpenAPI генерируется сервером и экспортируется в `contracts/openapi.yaml` в T016/T064. Типы frontend строятся из него. Примеры в этом документе — проект контракта; реальный OpenAPI должен покрыть все перечисленные операции. При изменении API обновить документ, generated types, mocks и затронутые тесты одним изменением.
