"""Build the planning backlog; this does not implement or complete app tasks."""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROWS = r"""
T001|D0|A|2|-|FR05|Зафиксировать один рабочий процесс|docs/pilot-brief.md|Описать пользователя, операцию, вход, результат, текущий способ обучения и признаки полезной инструкции.|Есть один пример операции, границы пилота и список неизвестного; нет обещания подтверждённого спроса.
T002|D0|A|6|T001|FR13,FR16,FR24,NF05,NF06|Прототип полного маршрута|frontend prototype; docs/ux-feedback.md|Собрать библиотеку, загрузку, редактор, проверку и читателя на фиктивных данных; показать двум людям.|Зафиксированы наблюдения прохождения на телефоне и клавиатурой, три главные проблемы и исправления.
T003|D0|A|6|T001|FR30|Проверить проблему и найти пилот|docs/pilot-interviews.md|Подготовить вопросы, провести согласованные командой интервью с мастером, новичком и руководителем; выбрать один процесс.|Есть свидетельства текущей проблемы, контакт владельца решения и согласие на пробу либо честный статус отсутствия партнёра.
T004|D0|C|3|T001|FR29|Определить правила использования материалов|docs/data-agreement.md|Описать разрешённые записи, роли, хранение, удаление и внешнюю передачу; отделить свои демо от компании.|Для каждого материала известны источник и разрешённый режим; внешний AI по умолчанию выключен.
T005|D0|B|3|T004|FR28,NF12|Задать лимит бюджета и критерии инфраструктуры|docs/budget.md|Собрать реальные датированные предложения для сервера/хранения/API, формулу расходов и потолок; покупок без решения нет.|В таблице отделены предложения от одобренных сумм; если суммы не выбраны, платные вызовы заблокированы.
T006|D0|A|4|T002|FR24|Создать frontend основу|frontend; frontend lockfile|Настроить React/TypeScript/Vite, router, базовые токены стиля, lint/typecheck и один компонентный тест.|Чистая установка по lock проходит, локальный экран работает, нет секретов и фиктивных production данных.
T007|D0|B|5|T001|FR01|Создать backend основу|backend/config; dependency lock|Настроить Django/DRF, свою User-модель до первой миграции, test settings и PostgreSQL подключение.|Миграции проходят на пустой БД, health работает, test command повторяем, DEBUG запрещён в pilot.
T008|D0|C|5|T006,T007|FR30|Настроить воспроизводимые проверки|CI config; scripts; CONTRIBUTING.md|Зафиксировать команды lint/typecheck/backend tests и формат PR; тестовые секреты отделить.|Проверки проходят в чистой среде, намеренная ошибка типа/теста останавливает pipeline.
T009|D0|C|4|T004|FR07,FR10|Подготовить медиа и ручной эталон|fixtures manifest; annotation template|Снять или взять разрешённые демо, разметить шаги и границы, подготовить корректный/повреждённый/беззвучный ролик.|Все файлы имеют hash и происхождение, эталон проверен человеком, большие бинарные файлы не помещены в Git.
T010|D0|B|10|T007|FR02,FR03,FR05,FR20|Создать модели и ограничения данных|backend models/migrations|Реализовать сущности 04 для D0, tenant constraints, is_open, revisions, уникальности; определить миграции D1.|ORM-тест отвергает чужие связи, порядок шагов меняется транзакционно, миграции обратимы где заявлено.
T011|D0|B|8|T010|FR01,NF01|Реализовать сессии, CSRF и reset|backend/accounts|Login/logout/me/reset, password validators, normalized 401/403, CSRF и rate limits.|Положительные и отрицательные auth тесты проходят; reset не раскрывает наличие email; токен одноразовый.
T012|D0|B|6|T011|FR01,FR02,FR04|Приглашения и управление участниками|backend/organizations; mail templates|Создать приглашения с TTL/hash, принять/отозвать, менять role/active; защитить последнего admin.|Повтор и гонка accept не дублируют membership; конкурирующая демоция не оставляет организацию без admin.
T013|D0|B|6|T010,T011|FR02,FR03,FR04,NF01|Единая проверка полномочий|backend access services; tests|Ввести scoped queries и policy actions, нейтральный отказ чужим IDs, suspended/deleted проверки.|Матрица ролей проверена на инструкции и связанных объектах; отключение действует со следующего запроса.
T014|D0|A|5|T006,T012|FR02,FR04|Экран участников|frontend/features/members|Список, invite, роль, отключение, истёкшее приглашение, LAST_ADMIN, loading/error состояния.|Reader не видит управление; admin проходит invite и revoke; серверный отказ отображается без потери формы.
T015|D0|A|6|T006,T011|FR01,FR23|Интерфейс входа и сессии|frontend/features/auth; api client|Вход, reset, accept invite, protected routes, return-to после QR, обработка expiry и CSRF.|QR возвращает на внутреннюю страницу после входа; внешний redirect запрещён; секреты не хранятся в localStorage.
T016|D0|B|4|T011,T012,T013|FR03,FR15|Зафиксировать OpenAPI и типы|contracts/openapi.yaml; frontend/src/api|Настроить генерацию схемы ошибок, auth, ролей, ETag и типов; добавить contract check в CI.|Frontend использует generated types; изменение server schema без обновления контракта обнаруживается проверкой.
T017|D0|B|10|T010,T013,T020|FR05,FR06,FR07|Создание инструкции и потоковая загрузка|backend/media_assets; upload services|Создать draft, UploadSession, atomic quota reservation, streaming temp file, size/hash checks и cancel.|Обрыв, двойная отправка, неверный размер и гонка квоты не дают лишних файлов/расхода; RAM не растёт на размер файла.
T018|D0|A|6|T015,T017|FR06,FR08,FR09,NF07|Экран загрузки и состояния обработки|frontend/features/upload|Выбор файла/языка, лимиты, XHR progress, cancel/retry, polling с backoff, ручной путь.|Перезагрузка восстанавливает job; процент показывает только bytes upload; нет вечного спиннера при failed.
T019|D0|C|8|T009,T010|FR07,FR10,NF10|Проверка и подготовка видео|backend/processing/media|FFprobe/FFmpeg с bounds, rotation, 720p playback, thumbnail, isolated temp и безопасными аргументами.|Допустимый, портретный, HEVC, тихий и повреждённый примеры ведут себя ожидаемо; timeout убивает процесс.
T020|D0|C|10|T008,T010|FR08,FR09,NF07,NF10,NF11|Надёжный исполнитель фоновых заданий|backend/processing/jobs|DB jobs, lease/heartbeat/fencing, on_commit dispatch, recovery, cancel и retry; broker несёт только ID.|Crash и двойная доставка дают один принятый результат; потеря dispatch восстанавливается; stale attempt не пишет.
T021|D0|B|6|T013,T017,T019,T020|FR03,FR10|Закрытая выдача видео|backend media views; infra/nginx|Связать prepare job с upload; авторизовать playback/thumbnail через internal location и Range.|Reader не читает draft/чужое видео/оригинал; прямой internal URL запрещён; Range/HEAD/416 проверены на стенде.
T022|D0|B|8|T010,T013,T016|FR13,FR14,FR15,NF08|API ручного редактора|backend/instructions/services|GET/PUT working copy, validate bounds/evidence, If-Match, reorder, сброс checked, комментарии старых шагов.|Два PUT дают success и 412; неверный шаг откатывает весь запрос; закрытая/in_review копия не изменяется.
T023|D0|A|12|T002,T015,T022|FR13,FR14,FR16|Редактор шагов и preview|frontend/features/editor|Добавить/удалить/переставить шаг, название/описание/время, ошибки, сохранение и preview reader.|Можно вручную собрать инструкцию из трёх шагов; ошибки у нужных полей; preview совпадает с будущим snapshot.
T024|D0|A|8|T021,T023|FR10,FR14,FR24|Плеер с переходом к шагу|frontend video components|Видео, controls, seek start, pause end, speed, fullscreen, duration/rotation, отказ загрузки.|Работает после жеста пользователя на телефоне; время вне duration не принимается; ошибка медиа не стирает шаги.
T025|D0|A|5|T022,T023|FR15,NF04,NF08|Автосохранение без потери правок|frontend editor save queue|Debounce 1500 ms, один inflight, latest pending, ETag update, conflict UI, dirty navigation.|Гонки, offline и expiry не показывают ложное сохранение; локальный текст остаётся доступным после 412.
T026|D0|B|7|T022|FR17,FR18,FR19|Серверный процесс проверки|backend review services|Submit/withdraw/return, frozen copy, review comments, resolve blocker и check-step с revision.|Author не снимает блокер; правка сбрасывает проверки; возврат требует причину; stale ETag отклоняется.
T027|D0|A|6|T024,T025,T026|FR17,FR18,FR19|Экран проверки и замечаний|frontend/features/review|Видео рядом со шагами, подтверждения, blocking comments, return и publish summary.|Reviewer видит все непроверенные шаги и блокеры; author может исправить после возврата, без обхода прав.
T028|D0|B|8|T026,T021|FR19,FR20,FR21|Публикация и неизменяемые версии|backend publication service|Transaction locks, snapshot/hash, version number, current pointer, idempotency, withdraw и create-draft.|PUB01–PUB04 проходят; новая правка не меняет старый snapshot; отзыв не возвращает прежнюю версию автоматически.
T029|D0|A|4|T027,T028|FR20,FR21|История и новая рабочая версия|frontend/features/versions|История для W, создание draft из версии, отказ при активном draft, withdrawal с причиной.|Reader не получает history endpoint; новая версия не подменяет открытый текст без сообщения.
T030|D0|B|5|T028|FR22,NF03|Поиск по текущим публикациям|backend/search|Список, q/language/status/cursor; PostgreSQL simple, tenant filters, transactional search update.|Поиск не выдаёт draft/withdrawn/чужое; RU/KK термины и 100 записей проверены, без обещаний морфологии KK.
T031|D0|A|6|T015,T030|FR22|Библиотека инструкций|frontend/features/library|Поиск, фильтры, карточки, состояния empty/loading/error, clear filters, role-aware actions.|Длинные названия и нулевая выдача читаемы; старый поисковый ответ не заменяет более новый запрос.
T032|D0|A|7|T024,T028,T031|FR23,FR24,NF06|Мобильный читатель и QR|frontend/features/reader|Текущий snapshot, шаги, видео, next/previous, QR защищённого URL, new-version banner.|360 px без горизонтального скролла; QR не содержит секрет; старую отозванную версию нельзя загрузить снова.
T033|D0|B|4|T028|FR25|API замечаний читателя|backend/feedback|Create/list/resolve с version и snapshot_step_id, категориями, правами и idempotency.|Замечание остаётся у правильной версии после новой публикации; чужие IDs и duplicate submit обработаны.
T034|D0|A|4|T032,T033|FR25|Форма ошибки и очередь замечаний|frontend/features/feedback|Кнопка у шага, форма, подтверждение, список для W, решение с пояснением.|Reader видит результат отправки; ошибка сети позволяет повтор без дубля; автор находит исходную версию.
T035|D0|B|7|T013,T020,T028|FR26|Удаление и немедленный запрет доступа|backend deletion services|Soft delete, cancel jobs, DeletionRecord, audit и durable tombstone event; endpoint статуса.|Сразу исчезают search/media; повтор удаления идемпотентен; поздний job не восстанавливает данные.
T036|D0|C|5|T019,T020,T035|FR26|Физическая уборка файлов и производных|backend cleanup worker|Удалять original/playback/audio/thumbnail и контент БД; cleanup временных/stale sessions и безопасный retry.|DEL01 первая часть проходит, квота освобождается один раз, active lease temp не удаляется.
T037|D0|C|4|T011,T017,T028,T035|FR27,NF11|Структурированный журнал|backend/audit; logging config|События по allowlist, request/job IDs, redaction, retention и операторский поиск.|Тесты/просмотр логов не находят tokens, cookie, transcript и provider key; действие связано с actor/object.
T038|D0|C|8|T013,T021,T028,T035|FR02,FR03,FR04,NF01|Полная матрица отрицательных проверок|backend permission tests|Параметризовать org/role/lifecycle для всех текущих endpoints, медиа и связей.|SEC01–SEC06 проходят; нет исключённых endpoints без явного объяснения и последующей задачи.
T039|D0|C|8|T018,T025,T029,T032,T034|FR30,NF08|Сквозной ручной процесс|e2e tests; evidence report|Автоматизировать login→upload→manual→review→publish→reader→feedback и сетевые ошибки.|Сценарий повторяется из чистых fixtures; секретные данные не попадают в screenshots/CI artifacts.
T040|D0|A|6|T039|FR24,NF05,NF06|Мобильная и клавиатурная приёмка|docs/ux-acceptance.md; UI fixes|Проверить реальные телефоны, focus/dialogs, long text, Cyrillic/KK, reduced motion и slow network.|Записаны устройства/браузеры, исправлены блокирующие проблемы, основная задача доступна без drag.
T041|D0|C|8|T054,T036,T037|FR30,NF09|Резервирование и первая репетиция restore|infra backup/restore scripts|Согласованный DB/media backup, manifest, encrypted external target, retention, восстановление закрытой среды.|Данные и media hashes совпали; измерены RPO/RTO; результат не основан на одном successful backup exit.
T042|D0|A|4|T038,T039,T040|FR30|Приёмка ручного продукта|docs/d0-acceptance.md|Вместе с B/C пройти D0 criteria, сверить реальные данные и журнал дефектов.|Все блокирующие критерии выполнены либо D0 остаётся неготовым; есть версия и свидетельства, а не только чекбоксы.
T043|D1|C|8|T009,T019,T020|FR11,NF10|ASR и расшифровка с сегментами|backend processing ASR adapter|Переиспользовать установленный runtime, получить RU/KK/mixed сегменты, raw/corrected separation и revision.|На разрешённых образцах есть реальные результаты, длительность/RAM; no-audio не запускает бессмысленную генерацию.
T044|D1|C|6|T004,T005,T020|FR12,FR28,FR29|Адаптер составления шагов|backend processing providers|Fake/Disabled/External providers, typed errors, timeout, versioned config и usage; разрешение реального API отдельно.|Fake покрывает сбои; внешняя отправка без настройки запрещена; разрешённый запрос записывает usage без текста в логах.
T045|D1|C|6|T043,T044|FR12|Prompt и строгая проверка предложения|backend draft schema validator|Реализовать steps-v1, JSON Schema, evidence/time semantic validation, предупреждения и input size limits.|Выдуманные IDs/числа/лишние поля/31 шаг/команды в transcript не применяются; pipeline не публикует.
T046|D1|C|8|T045|FR11,FR12|Отложенный набор и оценка качества|fixtures AI evaluations; docs/ai-eval.md|Разделить tuning/holdout, разметить независимые эталоны, измерить качество по языкам и ошибки.|Есть отчёт с размером набора, источниками, версиями и неудачами; пороги не подгоняются под финальный тест.
T047|D1|B|6|T022,T043,T045|FR11,FR12,FR15|Transcript/proposal API и применение|backend transcript/proposal services|Миграции D1, correction revision, generation base revision, apply с проверкой актуальности и audit.|Исправление transcript инвалидирует checks; stale proposal даёт 409 и не затирает ручной текст.
T048|D1|A|6|T024,T047|FR11|Редактор расшифровки|frontend/features/transcript|Сегменты с временем, переход в видео, correction, raw text доступен для сверки, revision conflict.|Исправление не переписывает raw; длинная расшифровка читаема; ошибки и неопределённость видны.
T049|D1|A|6|T048,T047|FR09,FR12,FR13|Принятие черновика ИИ|frontend/features/proposals|Показать предложение/источники/warnings, подтвердить замену шагов, обработать stale и manual fallback.|Применение только явным действием; одновременная правка не теряется; все checks после применения сброшены.
T050|D1|B|6|T005,T044,T047|FR28,FR29,NF12|Атомарный учёт расходов|backend budget ledger|Добавить reservation/settlement, pricing version, дневной предел, неизвестный outcome и kill switch.|Гонка двух запросов не превышает лимит; все retries учитываются; key/бюджет не доступны frontend.
T051|D1|C|6|T043,T045,T050|FR08,FR09,NF10,NF11|Сбои реальной ASR/ИИ обработки|processing retry/cancel tests|Проверить provider 429/5xx, invalid JSON, cancel subprocess, lease loss, provider disable и повтор стадии.|Сохранены готовое видео и ручные шаги, нет бесконечных retry, orphan outputs убраны.
T052|D1|C|6|T046,T049,T050,T051|FR12,FR29,FR30|Сквозные и негативные тесты D1|e2e AI tests; provider fixtures|Проверить actual approved provider отдельно от fixtures; включить prompt injection и чужие evidence.|Разрешённый путь проходит, неподтверждённый контент не публикуется, budget/data gate нельзя обойти.
T053|D0|B|4|T005,T019|NF10,NF12|Выбор тестового хостинга|docs/hosting-decision.md|Сопоставить результаты медиа с ресурсами и требованиями данных, выбрать конфигурацию после решения владельца.|Есть конфигурация/стоимость/владелец/регион; если нет решения, продолжать локально без заявления о доступном стенде.
T054|D0|C|8|T008,T021,T053|FR30,NF10|Развёртывание staging|infra/compose; nginx; release scripts|HTTPS, private volumes, DB/broker internal, worker/recovery, migrations, environment validation.|С чистого сервера воспроизводится manual smoke; direct media запрещено; DEBUG/secrets проверены.
T055|D0|C|5|T037,T054|NF07,NF11|Мониторинг и рабочие инструкции|infra monitoring; docs/runbooks|Метрики queue/disk/latency/backup, health endpoints, thresholds и инструкция действий.|Искусственный сбой виден и объясним; мониторинг не раскрывает содержимое и внутренние endpoints наружу.
T056|D0|C|6|T030,T039,T054|NF02,NF03,NF04,NF10|Нагрузочная проверка пилота|load scripts; docs/performance.md|10 users, 100 instructions, p95 metadata/search/save, media durations/RSS; фиксировать стенд.|Есть фактические значения и ошибки; превышение целей ведёт к исправлению/изменению лимита с обоснованием.
T057|D0|A|4|T003,T004,T032,T034|FR30|Подготовка пользовательского эксперимента|docs/pilot-protocol.md|Выбрать 5 процессов, сценарии для мастера/читателя, baseline времени, форму согласия и обратной связи.|Назначен владелец проверки контента; если партнёра нет, явно указан demo-only и следующий способ поиска.
T058|D1|A|4|T052,T063,T068|FR30|Техническая приёмка D1|docs/d1-acceptance.md|Собрать отчёты качества/затрат/ошибок и пройти D1 criteria с командой.|Решение с перечнем поддержанных языков и ограничений; провал критерия оставляет функцию экспериментальной.
T059|D1|A|8|T057,T058|FR30|Пилот с мастером и читателями|docs/pilot-results.md|Пять утверждённых инструкций, пять читателей, шесть парных задач по времени; наблюдение без подсказок.|Фактическая полезность, правки и время записаны; отсутствие партнёра не заменено вымышленными отзывами.
T060|D1|A|6|T059|FR13,FR24,FR30|Исправить основные проблемы пилота|scoped fixes; updated tests|Выбрать три наиболее частых препятствия, назначить владельцев и исправить без расширения всего продукта.|Каждая проблема воспроизведена до и проверена после; обновлены затронутые критерии и инструкции.
T061|D2|A|4|T059|FR30|Проверить коммерческую модель|docs/business-validation.md|Сравнить готовность продолжать/платить, стоимость инструкции, альтернативы и поддержку; провести ценовые интервью.|Цена обозначена гипотезой до оплаты/решения клиента; есть критерий продолжить, изменить сегмент или остановить расширение.
T062|D0|C|4|T041,T055|FR30,NF09,NF11|Руководство эксплуатации и передачи|docs/operator-handbook.md|Описать install/update/restore/delete/revoke, владельцев секретов, инцидент и экспорт при уходе пилота.|Другой участник выполняет smoke и restore по тексту без устных подсказок автора.
T063|D0|B|4|T042,T054,T056,T057,T062,T064,T065,T066|FR30|Выпустить D0|release record; evidence|Проверить критерии и отсутствие P0/P1, согласовать доступ к стенду, отметить точную версию.|Ручной продукт работает в согласованной среде; AI выключен; demo и pilot статусы различаются.
T064|D0|B|4|T016,T034,T035,T067|FR02,FR15,FR30|Сверить API, типы и документацию|contract validation report|Перечислить все D0 endpoints, статусы, роли и schema, обновить generated types и fixtures.|Нет дрейфа API/UX; неизвестные маршруты не используются frontend; 401/403/404/412/428 проверены.
T065|D0|C|4|T036,T041|FR26,NF09|Удаление после восстановления|restore/deletion test report|Удалить инструкцию, восстановить более старый backup закрыто, применить durable tombstones и очистку.|Удалённый source, snapshots и derivatives не доступны после открытия; сроки retention объяснены.
T066|D0|B|4|T038,T054|FR03,FR29,NF01|Проверить угрозы и настройки релиза|docs/security-review.md|Пройти секреты, upload isolation, CSRF, last admin, private media, dependency advisories и external AI default.|Критичные находки закрыты проверками; нет заявления о полном security audit без соответствующего объёма.
T067|D0|A|4|T013,T015,T035|FR26,FR28,FR29|Настройки и удаление в UI|frontend/features/settings|Лимиты, используемое место, admin confirmation delete, статус удаления; внешняя AI настройка пока off.|Не-admin не изменяет настройки; отмена диалога ничего не удаляет; удаляемая инструкция сразу скрыта.
T068|D1|C|4|T037,T049,T050|FR27,FR28,FR30|Метрики полезности и затрат|backend metrics; report query|Собирать события create/review/publish/open-step/feedback без текста, usage и длительность редактирования.|Отчёт по инструкции строится без слежения за личной эффективностью; определён знаменатель каждой метрики.
T069|D1|C|6|T046,T052|FR11,FR12|Решение по казахскому языку|docs/kk-evaluation.md|Разделить RU/KK/mixed результаты, оценить новый KK набор при доступности, зафиксировать терминологические ошибки.|Если данных мало/порог не пройден, KK AI обозначен экспериментальным; ручной KK текст работает.
T070|D1|A|3|T059,T060|FR30|Пересмотреть план после пилота|docs/pilot-retrospective.md|Сравнить оценки/часы, повторное использование, ошибки и стоимость, выбрать следующий узкий результат.|Есть конкретное решение и его наблюдаемые основания; нет автоматического обещания масштабирования.
T071|D2|A|3|T061,T070|FR30|План развития на следующие месяцы|docs/next-quarter.md|Ранжировать актуальность, перевод, Q&A, offline и интеграции по подтверждённым запросам.|У каждой выбранной функции есть пользователь, критерий пользы, стоимость и отдельная спецификация до реализации.
T072|D1|B|4|T058,T060,T069,T070|FR30|Выпустить проверенный D1|D1 release record; refreshed contracts|Повторить затронутые тесты, обновить API/схемы/документы после AI миграций, backup и release notes.|Есть рабочая версия с явными языковыми ограничениями, реальными результатами и планом поддержки, без P0/P1.
""".strip()

tasks = []
for line in ROWS.splitlines():
    parts = line.split('|')
    assert len(parts) == 10, line
    tid, tier, owner, hours, deps, reqs, title, artifact, work, done = parts
    tasks.append(dict(id=tid, tier=tier, owner=owner, hours=int(hours),
                      dependencies=[] if deps == '-' else deps.split(','),
                      requirements=reqs.split(','), title=title, artifact=artifact,
                      work=work, done=done, status='pending'))
ids = {t['id'] for t in tasks}
assert len(ids) == len(tasks) == 72
by_id = {t['id']: t for t in tasks}
visited, visiting = set(), set()
def visit(tid):
    assert tid in ids, tid
    assert tid not in visiting, 'dependency cycle ' + tid
    if tid in visited:
        return
    visiting.add(tid)
    for dep in by_id[tid]['dependencies']:
        assert not (by_id[tid]['tier'] == 'D0' and by_id[dep]['tier'] != 'D0'), tid
        visit(dep)
    visiting.remove(tid)
    visited.add(tid)
for tid in ids:
    visit(tid)
covered = set(r for t in tasks for r in t['requirements'])
assert set('FR%02d' % i for i in range(1, 31)) <= covered
assert set('NF%02d' % i for i in range(1, 13)) <= covered
totals = Counter()
for t in tasks:
    totals[t['tier']] += t['hours']
    totals[t['owner']] += t['hours']
data = {'as_of': '2026-10-28', 'estimate_kind': 'initial human effort, not measured',
        'reserve_factor': 1.25, 'tasks': tasks}
(ROOT/'planning/tasks.json').write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
lines = ['# Задачи разработки', '', '[К полному плану](00-overview.md)', '',
         'Все 72 задачи имеют статус **pending**. Документация и схема уже подготовлены, но это не означает выполнения задач приложения. Порядок определяется зависимостями, а не только номером.', '',
         'Оценки — начальные человеко-часы сосредоточенной работы с проверкой, с применением ИИ, но без обещания его ускорения. Сверху заложить 25% на интеграцию, обнаруженные ошибки и неопределённость. Ожидание партнёра/доступа не входит в часы и может удлинить календарь. После первых пяти завершённых задач заменить оценки измерениями.', '',
         'A — продукт и интерфейс; B — сервер и данные; C — обработка, ИИ, инфраструктура и проверки. Владелец организует результат; другой участник проверяет критичное изменение. Имена и доступные часы ещё не сообщены.', '',
         '| Объём | Часы без резерва | С резервом 25% |', '|---|---:|---:|']
for tier in ['D0','D1','D2']:
    lines.append('| '+tier+' (добавочные задачи) | '+str(totals[tier])+' | '+str(round(totals[tier]*1.25, 1))+' |')
base = totals['D0']+totals['D1']
lines += ['| D0 + D1 вместе | '+str(base)+' | '+str(round(base*1.25,1))+' |', '',
          '## Распределение', '', '| Участник | D0 | Добавление D1 | D2 |', '|---|---:|---:|---:|']
for owner in 'ABC':
    vals = [sum(t['hours'] for t in tasks if t['owner']==owner and t['tier']==tier) for tier in ['D0','D1','D2']]
    lines.append('| '+owner+' | '+' | '.join(map(str, vals))+' |')
lines += ['', '## Реестр', '', '| ID | Результат | Владелец | Уровень | Часы | Зависимости |', '|---|---|---|---|---:|---|']
for t in tasks:
    lines.append('| [{0}](#{1}) | {2} | {3} | {4} | {5} | {6} |'.format(t['id'], t['id'].lower(), t['title'], t['owner'], t['tier'], t['hours'], ', '.join(t['dependencies']) or '—'))
lines += ['', '## Карточки работ', '']
for t in tasks:
    lines += ['<a id="'+t['id'].lower()+'"></a>', '', '### '+t['id']+' — '+t['title'], '',
              '**Статус:** pending · **Уровень:** '+t['tier']+' · **Владелец:** '+t['owner']+' · **Оценка:** '+str(t['hours'])+' ч.', '',
              '**Начать после:** '+(', '.join(t['dependencies']) or 'можно начинать сейчас')+'. **Требования:** '+', '.join(t['requirements'])+'.', '',
              '**Где появляется результат:** `'+t['artifact']+'`. Это целевые артефакты реализации; их наличие ещё нужно создать и проверить.', '',
              '**Сделать:** '+t['work'], '', '**Готово, когда:** '+t['done'], '']
lines += ['## Общий порядок закрытия', '',
          'Исполнитель прикладывает фактический результат проверки и обновляет статус в planning/tasks.json только после реализации. Проверяющий сверяет критерий и зависимости. Для доступа, публикации, удаления и внешней передачи обязателен обзор другим участником. Если меняется контракт или исходная оценка, обновить связанные документы. Не закрывать задачу только потому, что модель написала код.', '',
          'Машиночитаемый реестр: [planning/tasks.json](../planning/tasks.json). Генератор первоначального реестра — planning/build_tasks.py; он задаёт исходные pending-статусы. После начала работы не запускать его поверх актуальных статусов: менять канонический tasks.json и отдельно обновлять представление.', '']
(ROOT/'docs/11-tasks.md').write_text('\n'.join(lines))
print(json.dumps({'tasks':len(tasks),'totals':dict(totals),'D0_D1':base,'with_reserve':base*1.25},ensure_ascii=False))
