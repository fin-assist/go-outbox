# go-outbox

Transactional outbox на PostgreSQL: событие пишется в транзакции изменения, доставщик в каждом поде отправляет его получателям системным gRPC-вызовом с повторами. Без брокера. Нужна производителям событий — `user` и `bank-statement-data`. Задача — [FIN-109](https://finassist.youtrack.cloud/issue/FIN-109).

Создана из шаблона [`fin-assist/go-library-template`](https://github.com/fin-assist/go-library-template) ([FIN-118](https://finassist.youtrack.cloud/issue/FIN-118)); сейчас здесь только каркас, реализация — [FIN-109](https://finassist.youtrack.cloud/issue/FIN-109).

- [docs/releasing.md](docs/releasing.md) — версии, что несовместимо, CHANGELOG, выпуск, миграции, Renovate.
- [CHANGELOG.md](CHANGELOG.md) — изменения по версиям.
- [AGENTS.md](AGENTS.md) — карта и правила для агентов, Code Review Rules.

## Раскладка

```
doc.go               package documentation: what the library does and what it needs from the service
migrations.go        Migrations, MigrationsName, MigrationsTable for the service's migration runner
migrations/          SQL migrations of the library, embedded (NNN_name.up.sql / .down.sql)
internal/pgtest/     PostgreSQL for tests and the up -> down -> up check, same rules as the service runner
scripts/             coverage of changed lines, CHANGELOG checks (Python, standard library)
docs/releasing.md    versions and releases
```

Код библиотеки — в корневом пакете (импорт `github.com/fin-assist/go-outbox` даёт `outbox.Publish`); подпакеты — когда у частей разные потребители (`identity/grpcserver` и `identity/httpmw`), внутренние детали — в `internal/`.

## Внедрение

Появится вместе с реализацией ([FIN-109](https://finassist.youtrack.cloud/issue/FIN-109)): подключение `go get github.com/fin-assist/go-outbox@vX.Y.Z`, источник миграций в `MigrationSources()` сервиса, сборка в `wire()`, интерцепторы и задания, чек-лист нового метода, метрики и алерты.

## Проверки

CI (`.github/workflows/ci.yml`):

| Job | Что |
|---|---|
| `go` | `go mod tidy` без изменений, `go vet`, `go test -race` с PostgreSQL 18 в Docker (testcontainers), включая «вверх → вниз → вверх» для миграций; тесты скриптов; формат `CHANGELOG.md`; покрытие изменённых строк — в сводке прогона и комментарием в PR |
| `lint` | golangci-lint (`.golangci.yml`): `forbidigo` против обхода `finerr` и против глобальных логгера, реестра метрик и переменных окружения; `depguard` против фреймворков |
| `api` | `gorelease`: изменения экспортируемого API против последнего тега и предлагаемая версия; на PR выпуска — проверка номера из `CHANGELOG.md` |
| `migrations` (PR) | существующие миграции не изменены, squawk по новым |

Тег `vX.Y.Z` запускает `.github/workflows/release.yml`: проверка тега и GitHub Release с текстом из `CHANGELOG.md`.

Локально:

```bash
make test          # Docker for testcontainers, or TEST_DATABASE_URL=postgres://... to use your PostgreSQL
make lint
make cover         # cover.out and the table of changed lines against origin/main
make api           # API changes against the latest tag
make release-check VERSION=v0.2.0
```

Цель покрытия — не ниже **80 %** изменённых исполняемых строк production-кода; CI не падает, порог проверяется на ревью (AGENTS.md, Tests). Модули `github.com/fin-assist/*` берутся из git, минуя прокси (`GOPRIVATE`, задан в `Makefile` и CI).

## Автоматизация обновлений зависимостей

[Политика Renovate](docs/renovate-policy.md) (FIN-211): одна актуальная release/main, автомерж полностью ботских PR после обязательного CI, один повтор красного CI и закрытие с явным повтором через Dependency Dashboard. AndroidX/Compose остаются на ручной проверке. Исходные группировки, ограничения версий и расписания — `renovate-base.json`; generated target и quarantine — ветка `renovate-policy`. Активация требует отдельного токена и строгой защиты веток; по умолчанию выключена.
