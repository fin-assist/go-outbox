# Изменения

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии и выпуск — [docs/releasing.md](docs/releasing.md). Каждый PR библиотеки, включая документацию, тесты и CI, добавляет строку в «Unreleased»; исключение — полностью ботские PR Renovate только с зависимостями, их сводку по `go.mod` добавляет PR выпуска. При выпуске раздел получает номер версии и дату.

## [Unreleased]

### Несовместимо

- FIN-244: `Dispatcher.Run` немедленно возвращает `postgres.ErrForeignTransaction`, если контекст содержит текущую транзакцию go-postgres. Запускайте worker с lifecycle-контекстом сервиса вне callback `Manager.Run`; транзакционный контекст передавайте только операциям сервиса и Publisher. Отказ не запускает горутины и не обращается к pool; повторный запуск с корректным контекстом разрешён.

### Изменено

- FIN-335: перенесены из go-library-template запреты FIN-294, защита CI FIN-301 и правила CHANGELOG FIN-307. `forbidigo` запрещает все найденные пути к глобальному состоянию (стандартные логгеры, включая `fmt.Print*`, `log.Default`/`SetOutput` и `grpclog`; глобальный реестр метрик, включая `prometheus.DefaultRegisterer`/`DefaultGatherer`; `http.DefaultClient` и функции пакета `http`, включая `http.Serve`/`ServeTLS`, `http.ProxyFromEnvironment` и `x/net/http/httpproxy.FromEnvironment`; `grpc/binarylog.SetSink`, `grpc/encoding/gzip.SetLevel`; глобальные реестры grpc-go `encoding.RegisterCodec`/`RegisterCodecV2`/`RegisterCompressor`, `resolver.Register`/`SetDefaultScheme`, `balancer.Register` и `grpc.EnableTracing`; глобальные провайдеры `otel`; `os/signal`; окружение через `os`, `syscall` и `golang.org/x/sys`, включая чтение всего окружения Win32 `windows.GetEnvironmentStrings`, `ExpandEnvironmentStrings`, `CreateEnvironmentBlock` и `Token.Environ`; функции `flag`, `os.Stdout`/`Stderr`/`Stdin`, временный каталог, `runtime/debug.Set*`, `GOMAXPROCS`, смену рабочего каталога, корня и пути поиска DLL, umask и лимиты ресурсов, а также формы `syscall`/`x/sys` стандартных потоков, временного каталога и командной строки), `depguard` — пустой импорт `expvar`, `net/http/pprof`, `golang.org/x/net/trace`, `grpclog/glogger`, `glog`, `klog`, `automaxprocs`, `automemlimit`, `grpc/xds`, а также целиком `net/http/cgi` и `net/http/fcgi`; `scripts/test_lint_policy.py` проверяет правила закреплённым golangci-lint на пробном модуле; версии prometheus, grpc, otel, x/net и x/sys проба берёт из `go.mod` и закрепляет на max(`go.mod`, ручная версия) с точным совпадением после `tidy`, а закреплённые вручную модули, которых нет в `go.mod`, `tidy` может поднять (не ниже закреплённой по semver), чтобы PR Renovate с новым grpc не ломал пробу. CI не оставляет токен checkout в jobs `lint`, `api`, `migrations`, базовая ветка передаётся в скрипт `migrations` через окружение; `scripts/test_workflow_hardening.py` запрещает `${{` в `run` и checkout без `persist-credentials: false`. `docs/releasing.md` и вступление CHANGELOG требуют строку `[Unreleased]` от каждого PR, кроме полностью ботских PR Renovate, заголовок PR выпуска — с ключом FIN, сводку `go.mod` — в PR выпуска. Нарушений новых запретов в коде библиотеки не найдено; runtime и экспортируемый API не менялись.
- FIN-292: миграция `000002` добавляет индекс `outbox_events_lane (target, next_attempt_at, id)` через `CREATE INDEX CONCURRENTLY` (отдельный файл, вне транзакции); индекс `(next_attempt_at, id)` сохранён. Цикл получателя и разложение событий (`target=''`) выбирают кандидатов только своего `target` по новому индексу, поэтому backlog одного получателя больше не читается на каждой доставке остальным. Строки удалённых маршрутов обслуживает отдельный проход: при старте `Run` и раз в 5 минут (ожидание `Run` ограничено этим сроком и при большем `PollInterval`), пачками вперемешку с fanout. Запрос кандидатов отправляется безымянным выражением, чтобы PostgreSQL не перешёл на generic plan. Совместимо: API и метрики не меняются, код работает и до применения миграции; сервисам после обновления нужно выполнить `migrate up` (источник `outbox`). При падении построения индекса — удалить `INVALID`-индекс и повторить по README.
- FIN-318: `internal/pgtest` больше не копирует go-migrate (по FIN-310): оставлен только выбор тестового PostgreSQL (`TEST_DATABASE_URL`/testcontainers, skip локально, ошибка в CI), создание БД, правила файлов и обход up → down → up, а также применение источника в usage-тестах (`Apply`) делегированы `github.com/fin-assist/go-migrate` v0.1.1. go-migrate импортируется только тестами, но как require модуля входит в граф модулей и повышает минимальную версию go-migrate у сервисов, импортирующих библиотеку (сервисы уже на v0.1.1). Отрицательный интеграционный тест закрепляет, что `Apply` завершает тест при ошибке `migrate.Up`. Исключение `internal/pgtest` из покрытия не изменилось; runtime и экспортируемый API не менялись.
- FIN-312: записи, вошедшие в уже выпущенный тег v0.1.0, перенесены из «Unreleased» в секцию v0.1.0; в docs/releasing.md записано исключение для GitHub Release v0.1.0. Runtime, API и теги не изменены.
- FIN-244: транзакции выбора и fanout доставщика выполняются через go-postgres на одном выделенном соединении; session lock, доставка вне транзакции и сервисная транзакция Publisher сохранены. Контекст чужого Manager отклоняется до SQL и доставки; примеры и тесты проверяют композицию с сервисным Manager.
- Зависимость `github.com/fin-assist/go-postgres` поднята с v0.1.2 до v0.2.0 (Renovate, #14): сервис, обновивший go-outbox, получит go-postgres не ниже v0.2.0. Обновление совместимо: v0.2.0 добавляет OpenPools, API Run/RunMapped и `New(pool)` не изменились.

### Исправлено

- FIN-281: паника функции доставки получателя больше не завершает процесс сервиса: попытка считается неудачной (`delivery_failed`, backoff, `finassist_outbox_delivery_errors_total`), событие остаётся для повтора, значение паники не логируется.

### Добавлено

- FIN-243: по решению владельца только пакет internal/pgtest исключён из общего Go-списка инструментирования всех трёх уровней; его тесты продолжают выполняться, выбор пакетов и пустой список проверяются регрессионными тестами.

- FIN-243: независимые unit/integration/consumer API пороги statements 80% с закреплённым go-test-coverage, проверкой непустого исполнения и штатными artifacts вместо изменённых строк; приватные интерфейсы pool/connection для независимой проверки владения соединением без изменения публичного API, тесты и регламент проверок.

## [v0.1.0] — 2026-10-23

### Добавлено

- FIN-109: transactional Publish, fanout по конфигурации, primary-only worker с LISTEN/poll, безопасные повторы системных gRPC-вызовов, метрики и инструкция внедрения.

- FIN-214: правило Squawk `prefer-bigint-over-int` отключено; ширина целочисленных полей выбирается по диапазону домена.
