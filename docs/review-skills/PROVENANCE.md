# Источники и лицензии навыков

## Engineering Suite

Пакет `engineering-suite@openai-curated-remote` версии 1.1.1. Его `UPSTREAM_LOCK.json` и
`THIRD_PARTY_NOTICES.md` фиксируют следующие источники выбранных неизменённых файлов:

| Файлы | Источник и точная ревизия | Лицензия |
| --- | --- | --- |
| code-review, codebase-design, domain-modeling и их вспомогательные Markdown-файлы | [Matt Pocock skills](https://github.com/mattpocock/skills/tree/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering), commit `3cca18b368ae95cdbdebbff572ccafa662551015` | [MIT — Matt Pocock](licenses/aihero-LICENSE.txt) |
| api-and-interface-design, security-and-hardening, ci-cd-and-automation | [Addy Osmani agent-skills](https://github.com/addyosmani/agent-skills/tree/6ca0cd7db39b41b1c37e26d335c507ee92382c6d/skills), commit `6ca0cd7db39b41b1c37e26d335c507ee92382c6d` | [MIT — Addy Osmani](licenses/addy-LICENSE.txt) |
| Общий security-checklist.md | [Addy references](https://github.com/addyosmani/agent-skills/blob/6ca0cd7db39b41b1c37e26d335c507ee92382c6d/references/security-checklist.md), та же ревизия | [MIT — Addy Osmani](licenses/addy-LICENSE.txt) |

Сохранена также [лицензия пакета Engineering Suite](licenses/MIT.txt).
Общий справочник расположен в `vendor/references/security-checklist.md`: туда разрешается исходный
путь `../../references/security-checklist.md` из выбранного security-навыка.

## Engineering Skills for Go

Пакет 0.4.0, upstream tag `v0.4.0`: [golangskills.com](https://github.com/ashwingopalsamy/golangskills.com/tree/acadc48f8ede49d9889a7e02da70fa1bfbd61b3d),
commit `acadc48f8ede49d9889a7e02da70fa1bfbd61b3d`.
Сохранены исходные [LICENSE с Copyright 2026 Ashwin Gopalsamy](licenses/go-Apache-2.0.txt)
и [THIRD_PARTY_NOTICES.md](licenses/go-THIRD_PARTY_NOTICES.md). Отдельного upstream `NOTICE` в этой ревизии нет.

## Codex Skills

Источник — независимый каталог [codexskills.dev](https://codexskills.dev/about/), авторство на страницах:
Codex Skills editors. [Условия источника](https://codexskills.dev/about/#licensing) разрешают
использование, изменение и распространение по MIT. Снимок установленных копий — 03.11.2026.
Источник не публикует для этих файлов immutable tag/SHA, отдельную copyright-строку или файл лицензии:
в каждом исходном `SKILL.md` есть только `license: MIT`. Эти объявления сохранены.
[Отдельное уведомление и стандартный текст MIT](licenses/codexskills-MIT.txt) не приписывают
авторство Engineering Suite или fin-assist. Точный снимок фиксируется SHA256 каждого `SKILL.md`:

| Навык и страница источника | SHA256 |
| --- | --- |
| [outbox-pattern](https://codexskills.dev/skills/architecture/outbox-pattern/) | `6fbb0a25f4a83d945c753fa76a95202d5bf9d6c2bb4d2f339e54e3888d8a57a2` |
| [postgres-schema-design](https://codexskills.dev/skills/databases/postgres-schema-design/) | `10ee6ff30f25475df97705320f5037f412017028c001508b93d43eabf9259234` |
| [postgres-query-tuning](https://codexskills.dev/skills/databases/postgres-query-tuning/) | `f9f2cdb92f646b1273d970a9be5bede9fe00cead0837028ffbe2e32c2ecce8c8` |
| [postgres-indexing](https://codexskills.dev/skills/databases/postgres-indexing/) | `4bcedddbdcaf173369f86dec0b7e272055a99bb1f277497d99ec6a124dcd836a` |
| [database-transactions](https://codexskills.dev/skills/databases/database-transactions/) | `385eb6a8957a80a7181449fc46bbf0cb1ac4120be84a2b333ca25a13024e8c24` |
| [zero-downtime-migrations](https://codexskills.dev/skills/databases/zero-downtime-migrations/) | `6ba6fa7e84d0dd3adb9b997a04253512120281f8d71197d42306863c2468fb18` |
| [data-backfills](https://codexskills.dev/skills/databases/data-backfills/) | `78f5dfe12a1e8fa5e13012050209134ec21a2c216a57139a84ce8ef16cbae40b` |
| [connection-pooling](https://codexskills.dev/skills/databases/connection-pooling/) | `1717fbe4d5779563009344b5d3e404f23c41af254bd6d7c369821c1e96d69bc0` |
| [postgres-partitioning](https://codexskills.dev/skills/databases/postgres-partitioning/) | `bb4b2ee2bf75a05bb30af1d1433f65c5b0246d872276d5ec6d506e5c3b636019` |
