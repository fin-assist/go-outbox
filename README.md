# go-outbox

Transactional outbox на PostgreSQL: событие пишется в транзакции изменения, доставщик в каждом поде отправляет его получателям системным gRPC-вызовом с повторами. Без брокера. Нужна производителям событий — `user` и `bank-statement-data`. Задача — [FIN-109](https://finassist.youtrack.cloud/issue/FIN-109).

Создана из шаблона [`fin-assist/go-library-template`](https://github.com/fin-assist/go-library-template) ([FIN-118](https://finassist.youtrack.cloud/issue/FIN-118)).

- [docs/releasing.md](docs/releasing.md) — версии, что несовместимо, CHANGELOG, выпуск, миграции, Renovate.
- [CHANGELOG.md](CHANGELOG.md) — изменения по версиям.
- [AGENTS.md](AGENTS.md) — карта и правила для агентов, Code Review Rules.

## Автоматизация обновлений зависимостей

[Политика Renovate](docs/renovate-policy.md) (FIN-211): одна актуальная release/main, автомерж полностью ботских PR после обязательного CI, один повтор красного CI и закрытие с явным повтором через Dependency Dashboard. AndroidX/Compose остаются на ручной проверке. Исходные группировки, ограничения версий и расписания — `renovate-base.json`; generated target и quarantine — ветка `renovate-policy`. Активация требует отдельного токена и строгой защиты веток; по умолчанию выключена.

## Раскладка

```
doc.go               package documentation: what the library does and what it needs from the service
migrations.go        Migrations, MigrationsName, MigrationsTable for the service's migration runner
migrations/          SQL migrations of the library, embedded (NNN_name.up.sql / .down.sql)
internal/pgtest/     PostgreSQL for tests and the up -> down -> up check, same rules as the service runner
scripts/             coverage execution guards and regressions, CHANGELOG checks (Python, standard library)
docs/releasing.md    versions and releases
```

Код библиотеки — в корневом пакете (импорт `github.com/fin-assist/go-outbox` даёт `outbox.Publisher.Publish`); подпакеты — когда у частей разные потребители (`identity/grpcserver` и `identity/httpmw`), внутренние детали — в `internal/`.

## Внедрение

Библиотека не выпускает тег сама: после выпуска владельцем подключите
`go get github.com/fin-assist/go-outbox@vX.Y.Z`. Не используйте `replace` в PR сервиса.

1. Добавьте `{Name: outbox.MigrationsName, Table: outbox.MigrationsTable, FS: outbox.Migrations}`
   в `MigrationSources()` перед миграциями сервиса. Версии изолированы таблицей
   `outbox_schema_migrations`. Запустите миграции на primary перед стартом worker.
2. Соберите `publisher, err := outbox.NewPublisher(db, time.Now)`, где `db` реализует
   `Tx(context.Context) (pgx.Tx, bool)`. Внутри **той же транзакции**, что изменяет профиль,
   вызовите `publisher.Publish(ctx, "user.timezone_changed", userID, payload)`.
   `payload` — результат `proto.Marshal` с явными `user_id` и `settings_version`.
   Максимум: тема 200 байт, ключ 256 байт, payload 4 МиБ. Пустые тема/ключ запрещены.
   Без транзакции возвращается `ErrNoTransaction`, неправильное событие — `ErrInvalidEvent`.
   Ошибки БД не несут SQL и данных; `errors.Is` сохраняет отмену/дедлайн, `errors.As`
   к `*pgconn.PgError` возвращает только SQLSTATE и фиксированный безопасный текст.
   Тема и ключ проверяются на корректный UTF-8 до SQL. При любой ошибке сервис откатывает свою транзакцию.
   `pg_notify` выполняется в ней же: PostgreSQL разошлёт уведомление только после commit.
3. Для каждого получателя соберите `GRPCDelivery(conn, tokens, method, requestFactory,
   replyFactory)`. Например, `usersettingsv1.UserSettingsSyncService_SetUserTimezone_FullMethodName`,
   `func() proto.Message { return &usersettingsv1.SetUserTimezoneRequest{} }` и фабрика ответа.
   Соединение создаёт сервис по своему транспортному контракту: HTTP/2 внутри WireGuard.
   `tokens, err := identity.NewProjectedSource(projectedPath)` из опубликованной
   `github.com/fin-assist/go-identity v0.1.0` читает актуальный projected ServiceAccount token
   с audience `finassist-internal`. Core принимает маленький `TokenSource` интерфейс;
   usage-тест проверяет совместимость с этим опубликованным модулем и contracts `gen/go/v0.4.0`.
   Helper передаёт **только** `x-service-authorization: Bearer ...`; metadata пользователя,
   версии клиента и входящие токены не копируются. RPC получает deadline 30 секунд от worker.
4. Соберите `outbox.NewDispatcher(outbox.Options{Pool: primaryPool, Now: time.Now,
   Logger: logger, Registerer: registry, Routes: outbox.Routes{
   "user.timezone_changed": {"assistant": sendAssistant, "bank-statement-data": sendStatements},
   }})`. Запустите `Run(ctx)` как задание сервиса. `Run` стартует работу, конструктор — нет.
   Настройте pool на CNPG `-rw` endpoint, выделенный pool с минимум `число уникальных target + 3` соединениями.
   Конструктор проверяет MaxConns; оставьте этот запас свободным для dispatcher. Тем и
   уникальных получателей допускается не больше 64; число горутин ограничено получателями
   плюс одна для наблюдаемости, fanout выполняется в Run; общий LISTEN держит отдельное соединение.
   Для транзакций производителя используйте отдельный pool или обеспечьте этот свободный запас.
   Poll по умолчанию 3 секунды; независимые пачки содержат до 50 событий или доставок. Полная пачка продолжается сразу; уведомление ускоряет пробуждение.
   Сервис отменяет worker context и ждёт `Run` перед закрытием pool.
5. Получатель реализует идемпотентность: `settings_version` применяется только при большей
   версии; `calc_version` — по точному правилу ключа метрики в контракте (снимок принимается
   при версии **не меньше** сохранённой). Устаревшее событие и tombstone — OK без действий.
   Обработчик удаления повторно отвечает OK. Порядок доставки не гарантирован.

### Схема, транзакции и восстановление

`outbox_events` хранит исходное событие с пустым `target`; доставщик **атомарно** заменяет
его строками для текущих получателей. У каждой свой retry. Добавление получателя действует
на ещё не разложенные события; уже существующие доставки не пересоздаются. Удаление
получателя из конфигурации не удаляет оставшиеся строки: они остаются с
`route_unconfigured`, пока оператор не восстановит маршрут. Неизвестная тема сохраняется
и перепроверяется через 5 минут. Исходное `created_at` переносится во все задания.

В схеме нет CHECK, триггеров и серверных функций. `bigint` identity выбран по числу
**всех** вставок за срок жизни (fanout и DELETE не возвращают номера), а не по текущему backlog.
Индекс `(next_attempt_at,id)` обслуживает выборку готовых записей.

Короткая транзакция с `FOR UPDATE SKIP LOCKED` выбирает кандидатов страницами по 50,
получает session advisory lock `-id` и завершается **до** сети. Пустые страницы активных
session locks обходятся, поэтому первые 50 занятых строк не блокируют следующие.
Отрицательное пространство одноключевых advisory locks зарезервировано библиотекой;
не используйте его в других компонентах той же БД. Соединение выделено одной доставке
и **физически закрывается**, даже при ошибке/cancel: lock никогда не возвращается в pool.
Это сознательная цена простого надёжного cleanup; при настройке нагрузки учитывайте
новое DB-соединение на каждую доставку. Каждый target обслуживается отдельным долгоживущим циклом без общего барьера;
fanout и общий LISTEN работают независимо. Пока один получатель ждёт RPC, новые события
раскладываются и доставляются другим. После первой неудачной попытки своей пачки
получатель ждёт уведомления или poll, его backoff сохраняется в БД. Между живыми worker одна строка не доставляется
одновременно. При падении соединения или неопределённом результате RPC возможна
повторная, в том числе пересекающаяся с незавершённой старой попыткой, доставка —
получатели обязаны атомарно проверять версию/tombstone. Exactly-once не обещается.

Успех удаляет строку; ошибка даёт паузу 1, 2, 4… секунды с джиттером до 5 минут без
ограничения числа попыток. В `last_error` только `delivery_failed` / `route_unconfigured`:
ответы соседа могут содержать ПДн, поэтому они не сохраняются. Падение после commit,
до fanout, между RPC и DELETE либо во время переключения primary оставляет durable
задание. Повторы БД и LISTEN восстанавливаются через poll; успешное продвижение зависит
от CNPG и доступного primary. Отмена во время RPC оставляет строку для следующего worker.

### Метрики и обязательный алерт

Регистрируются в переданном реестре, без глобального состояния. Коллекторы обновляются
независимо от доставки каждые `min(PollInterval, 30 секунд)`, поэтому
медленный получатель не замораживает алерт. Ошибка наблюдения не выдаётся за нулевой backlog.

| Метрика | Значение |
|---|---|
| `finassist_outbox_rows{target}` | Число строк |
| `finassist_outbox_oldest_age_seconds{target}` | Возраст старейшей строки, секунды |
| `finassist_outbox_delivery_errors_total{target}` | Ошибки доставок |

`target` — только имена из конфигурации, пустая строка для событий до fanout и
`unconfigured` для удалённых маршрутов; ключи, темы, user ID и тексты ошибок метками не становятся.
Добавьте обязательный алерт `finassist_outbox_oldest_age_seconds > 3600` для **всех**
получателей и неразложенных событий; он включает зависшее удаление аккаунта. Пример:

```yaml
- alert: OutboxDeliveryStuck
  expr: finassist_outbox_oldest_age_seconds > 3600
  for: 1m
  labels:
    severity: critical
  annotations:
    summary: "Outbox не доставлен больше часа"
```

Метрики backlog — снимок последнего успешного наблюдения; стандартные алерты доступности worker,
БД и scrape нужны отдельно. Запись `last_error` помогает отличить отсутствующий маршрут
от ошибки RPC без раскрытия пользовательского payload.

## Проверки

CI (`.github/workflows/ci.yml`):

| Job | Что |
|---|---|
| `go` | `go mod tidy` без изменений, `go vet`, `go test -race` с PostgreSQL 18 в Docker (testcontainers), включая «вверх → вниз → вверх» для миграций; тесты скриптов; формат `CHANGELOG.md`; три независимых порога statements ≥80%, сводка и artifacts |
| `lint` | golangci-lint (`.golangci.yml`): `forbidigo` против обхода `finerr` и против глобальных логгера, реестра метрик и переменных окружения; `depguard` против фреймворков |
| `api` | `gorelease`: изменения экспортируемого API против последнего тега и предлагаемая версия; на PR выпуска — проверка номера из `CHANGELOG.md` |
| `migrations` (PR) | существующие миграции не изменены, squawk по новым; ширина integer выбирается по диапазону домена (FIN-214) |

Тег `vX.Y.Z` запускает `.github/workflows/release.yml`: проверка тега и GitHub Release с текстом из `CHANGELOG.md`.

Локально:

```bash
make test          # Docker for testcontainers, or TEST_DATABASE_URL=postgres://... to use your PostgreSQL
make lint
make cover         # independent unit/integration/API statements gates
make api           # API changes against the latest tag
make release-check VERSION=v0.2.0
```

### Покрытие statements (FIN-243)

`go-test-coverage/v2` закреплён на v2.20.1. Unit, integration и consumer API должны **каждый** покрывать не менее
80% statements production-пакетов, с одним знаменателем для всех трёх наборов. Список `-coverpkg` берётся из
`go list -race ./...`; по явному решению владельца FIN-243 исключается только точный пакет `<module>/internal/pgtest`.
Вложенные и похожие по имени пакеты остаются включены; других исключений файлов или пакетов нет.
Пустой список инструментирования приводит к ошибке. Все тесты по-прежнему запускаются через `./...`, включая harness.
Округление инструмента принимает 79,95% как 80,0%. Generated production-код и остальные helpers в обычных `.go` вне исключённого пакета входят в расчёт.
Combined — дополнительный union через штатный comma-profile инструмента; он не заменяет отдельные пороги.

`make cover-unit`, `make cover-integration`, `make cover-api` собирают свежие профили и JSON-логи с `-race` и
ограничением времени, затем проверяют порог. `make cover` выполняет все три проверки. Инвентаризация с `-race`
отклоняет тесты без префикса уровня. Unit не используют сеть/БД; integration работают с настоящими зависимостями;
API проверяют публичный Go API со стороны потребителя, а stateful-сценарии — с реальной БД в транзакции потребителя.
Исполняемые `Example…` входят в API; примеры без Output остаются проверкой компиляции.

Пустой suite, skip/fail фактического теста и отсутствующий/пустой/header-only профиль — ошибка. Недоступный PostgreSQL
в CI также приводит к ошибке. Обязательный job `go` требует успеха всех шести collection/check шагов. Профили
`unit.out`, `integration.out`, `api.out`, JSON-логи и штатные отчёты сохраняются в artifacts, результаты — в сводке CI.
Совместимость Go API отдельно проверяет прежний job `api` (`gorelease`). Собственного расчёта процентов и комментария
в PR больше нет. Порог дополняет ревью полноты сценариев; недостигнутые критерии указываются явно.
