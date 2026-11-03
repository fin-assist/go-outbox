# Навыки PR-ревью (FIN-421)

Перед содержательным ревью выберите по diff минимальный достаточный набор, прочитайте файлы `SKILL.md`
из проверяемой ревизии и выполните применимые процедуры. Название без чтения и применения не считается
использованием навыка. При изменении нескольких областей выберите навыки для каждой; весь каталог по умолчанию
читать не нужно. Обязательные проверки и полномочия заданы в корневом `AGENTS.md`.

| Навык | Применять при |
| --- | --- |
| [code-review](vendor/engineering-suite/code-review/SKILL.md) | Любой PR: проверка реализации относительно задачи и правил репозитория. |
| [codebase-design](vendor/engineering-suite/codebase-design/SKILL.md) | Изменения границ модулей, связности и публичных интерфейсов. |
| [domain-modeling](vendor/engineering-suite/domain-modeling/SKILL.md) | Изменения предметной модели и терминов. |
| [api-and-interface-design](vendor/engineering-suite/api-and-interface-design/SKILL.md) | REST, gRPC, DTO и публичные интерфейсы. |
| [security-and-hardening](vendor/engineering-suite/security-and-hardening/SKILL.md) | Доверенные границы, персональные данные, авторизация и ввод. |
| [ci-cd-and-automation](vendor/engineering-suite/ci-cd-and-automation/SKILL.md) | CI, сборка, выпуск и автоматизация. |
| [review-go-engineering-change](vendor/engineering-skills-for-go/review-go-engineering-change/SKILL.md) | Общее ревью изменений Go. |
| [go-service-boundaries](vendor/engineering-skills-for-go/go-service-boundaries/SKILL.md) | Границы сервисов и их взаимодействие. |
| [go-project-and-api-design](vendor/engineering-skills-for-go/go-project-and-api-design/SKILL.md) | Структура Go-пакетов и публичный API. |
| [go-testing-and-verification](vendor/engineering-skills-for-go/go-testing-and-verification/SKILL.md) | Тесты и проверка поведения Go. |
| [outbox-pattern](vendor/codexskills/outbox-pattern/SKILL.md) | Транзакционный outbox, доставка событий и потребители. |
| [postgres-schema-design](vendor/codexskills/postgres-schema-design/SKILL.md) | Новые таблицы, ограничения и типы PostgreSQL. |
| [postgres-query-tuning](vendor/codexskills/postgres-query-tuning/SKILL.md) | Медленные запросы и планы EXPLAIN. |
| [postgres-indexing](vendor/codexskills/postgres-indexing/SKILL.md) | Индексы PostgreSQL и их влияние на запросы. |
| [database-transactions](vendor/codexskills/database-transactions/SKILL.md) | Изоляция, блокировки и конкурирующие записи. |
| [zero-downtime-migrations](vendor/codexskills/zero-downtime-migrations/SKILL.md) | Изменения схемы без остановки сервиса. |
| [data-backfills](vendor/codexskills/data-backfills/SKILL.md) | Массовое заполнение и исправление данных. |
| [connection-pooling](vendor/codexskills/connection-pooling/SKILL.md) | Пулы соединений и их настройки. |
| [postgres-partitioning](vendor/codexskills/postgres-partitioning/SKILL.md) | Секционирование таблиц PostgreSQL. |

SQL и PostgreSQL навыки поставляются только в Go-бэкенд; в конкретном ревью они применяются лишь к
затронутым SQL/хранению/транзакциям. Навыки Android поставляются только в Android-репозиторий.
Для `contracts` применимы общие навыки и Go-навыки при изменении handwritten Go или генераторов.


## Доступность и ограничения

Все материалы лежат в `docs/review-skills/vendor/` этого репозитория. Читайте их по относительным
путям из PR head; локальные установленные плагины владельца не требуются. Если файл недоступен, назовите
ограничение и продолжайте обязательные проходы из `AGENTS.md`. Не приписывайте навыку выполненные проверки,
если его инструкция не была прочитана. Навык не разрешает merge, публикацию, доступ к секретам или UI-тесты.


## Источники, лицензии и обновление

- `engineering-suite` 1.1.1, MIT: неизменённые файлы навыков из установленного пакета; [лицензия](licenses/MIT.txt).
- `engineering-skills-for-go` 0.4.0, Apache-2.0: [исходный проект](https://github.com/ashwingopalsamy/golangskills.com), [лицензия](licenses/Apache-2.0.txt).
- Навыки PostgreSQL и outbox: [Codex Skills](https://codexskills.dev/), установленные копии от 03.11.2026, MIT (обозначено в каждом `SKILL.md`); исходные тексты и уведомления сохранены.

Копии зафиксированы в Git. При обновлении сверяйте источник и лицензию, переносите `SKILL.md`
и используемые им файлы `references/`, проверяйте ссылки и обновляйте этот каталог одним PR.
Изменение Go-шаблона не обновляет другие репозитории автоматически. Проверка сценариев выбора —
[acceptance.md](acceptance.md).
