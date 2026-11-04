# Источники и лицензии навыков

Снимок установленных существующих навыков — 04.11.2026. Исходные SKILL.md и используемые вспомогательные
файлы скопированы без изменений из установленного набора; проектные правила применения находятся в README.
Ревизия upstream и локально адаптированный снимок различаются: local_differences/adaptation в SNAPSHOT.json
не выдаются за неизменённый upstream. Полные SHA256 всех поставленных исходников и лицензий — в [SNAPSHOT.json](SNAPSHOT.json).

| Навык | Источник / ревизия сравнения | Лицензия |
| --- | --- | --- |
| code-review-and-quality | [https://github.com/addyosmani/agent-skills](https://github.com/addyosmani/agent-skills); `301b02b12bff98e4688e6984af6556802322bbde`; через Engineering Suite 1.1.1 | MIT; [addyosmani-MIT.txt](licenses/addyosmani-MIT.txt); [engineering-suite-MIT.txt](licenses/engineering-suite-MIT.txt) |
| security-and-hardening | [https://github.com/addyosmani/agent-skills](https://github.com/addyosmani/agent-skills); `301b02b12bff98e4688e6984af6556802322bbde`; через Engineering Suite 1.1.1 | MIT; [addyosmani-MIT.txt](licenses/addyosmani-MIT.txt); [engineering-suite-MIT.txt](licenses/engineering-suite-MIT.txt) |
| cicd-security | [https://github.com/briiirussell/cybersecurity-skills](https://github.com/briiirussell/cybersecurity-skills); `18642b272e9fe57ed6bdf3fd96614915e8a4d71d` | MIT; [briiirussell-MIT.txt](licenses/briiirussell-MIT.txt) |
| github-actions-validator | [https://github.com/akin-ozer/cc-devops-skills](https://github.com/akin-ozer/cc-devops-skills); `276af751e659315aaf56d3ad13d7c26f4e72e28a` | Apache-2.0; [akin-ozer-Apache-2.0.txt](licenses/akin-ozer-Apache-2.0.txt) |
| review-go-engineering-change | [https://github.com/ashwingopalsamy/golangskills.com](https://github.com/ashwingopalsamy/golangskills.com); `acadc48f8ede49d9889a7e02da70fa1bfbd61b3d` | Apache-2.0; [go-Apache-2.0.txt](licenses/go-Apache-2.0.txt); [go-THIRD_PARTY_NOTICES.md](licenses/go-THIRD_PARTY_NOTICES.md) |
| go-language-engineering | [https://github.com/ashwingopalsamy/golangskills.com](https://github.com/ashwingopalsamy/golangskills.com); `acadc48f8ede49d9889a7e02da70fa1bfbd61b3d` | Apache-2.0; [go-Apache-2.0.txt](licenses/go-Apache-2.0.txt); [go-THIRD_PARTY_NOTICES.md](licenses/go-THIRD_PARTY_NOTICES.md) |
| go-project-and-api-design | [https://github.com/ashwingopalsamy/golangskills.com](https://github.com/ashwingopalsamy/golangskills.com); `acadc48f8ede49d9889a7e02da70fa1bfbd61b3d` | Apache-2.0; [go-Apache-2.0.txt](licenses/go-Apache-2.0.txt); [go-THIRD_PARTY_NOTICES.md](licenses/go-THIRD_PARTY_NOTICES.md) |
| go-testing-and-verification | [https://github.com/ashwingopalsamy/golangskills.com](https://github.com/ashwingopalsamy/golangskills.com); `acadc48f8ede49d9889a7e02da70fa1bfbd61b3d` | Apache-2.0; [go-Apache-2.0.txt](licenses/go-Apache-2.0.txt); [go-THIRD_PARTY_NOTICES.md](licenses/go-THIRD_PARTY_NOTICES.md) |
| go-security-hardening | [https://github.com/ashwingopalsamy/golangskills.com](https://github.com/ashwingopalsamy/golangskills.com); `acadc48f8ede49d9889a7e02da70fa1bfbd61b3d` | Apache-2.0; [go-Apache-2.0.txt](licenses/go-Apache-2.0.txt); [go-THIRD_PARTY_NOTICES.md](licenses/go-THIRD_PARTY_NOTICES.md) |
| design-postgres-tables | [https://github.com/timescale/pg-aiguide](https://github.com/timescale/pg-aiguide); `5d3b76b5c986693b30753828b404aa66ba34c31a` | Apache-2.0; LICENSE and NOTICE included unchanged; [LICENSE](vendor/design-postgres-tables/LICENSE); [NOTICE](vendor/design-postgres-tables/NOTICE) |
| postgres-query-tuning | [https://codexskills.dev/skills/databases/postgres-query-tuning/](https://codexskills.dev/skills/databases/postgres-query-tuning/); `снимок без immutable revision` | MIT declared in SKILL.md; https://codexskills.dev/about/#licensing; [codexskills-MIT.txt](licenses/codexskills-MIT.txt) |
| postgres-indexing | [https://codexskills.dev/skills/databases/postgres-indexing/](https://codexskills.dev/skills/databases/postgres-indexing/); `снимок без immutable revision` | MIT declared in SKILL.md; https://codexskills.dev/about/#licensing; [codexskills-MIT.txt](licenses/codexskills-MIT.txt) |
| database-transactions | [https://codexskills.dev/skills/databases/database-transactions/](https://codexskills.dev/skills/databases/database-transactions/); `снимок без immutable revision` | MIT declared in SKILL.md; https://codexskills.dev/about/#licensing; [codexskills-MIT.txt](licenses/codexskills-MIT.txt) |
| postgres-database-migration | [https://github.com/timescale/pg-aiguide](https://github.com/timescale/pg-aiguide); `5d3b76b5c986693b30753828b404aa66ba34c31a` | Apache-2.0; LICENSE and NOTICE included unchanged; [LICENSE](vendor/postgres-database-migration/LICENSE); [NOTICE](vendor/postgres-database-migration/NOTICE) |
| outbox-pattern | [https://codexskills.dev/skills/architecture/outbox-pattern/](https://codexskills.dev/skills/architecture/outbox-pattern/); `снимок без immutable revision` | MIT declared in SKILL.md; https://codexskills.dev/about/#licensing; [codexskills-MIT.txt](licenses/codexskills-MIT.txt) |

Для `code-review-and-quality`, `security-and-hardening` исходные references указывают посредника EngineeringSuite 1.1.1.
Сохранено [исходное MIT-уведомление Engineering Suite contributors](licenses/engineering-suite-MIT.txt) из [закреплённой копии go-service-template](https://github.com/fin-assist/go-service-template/blob/77491fb876eb124fd9c085deb24a9ff9432e4514/docs/review-skills/licenses/MIT.txt). Цепочка Addy Osmani → Engineering Suite 1.1.1 → установленный снимок отражена в поле
`intermediary` SNAPSHOT.json. Пакет сейчас не установлен; список `local_differences` означает отличия от
закреплённого Addy upstream, а не утверждение об авторстве каждого отличия. Исходные reference-тексты не менялись.

У поставленных навыков Codex Skills исходные SKILL.md содержат объявление MIT; отдельная copyright-строка
в дистрибутиве отсутствует. Разрешение распространения подтверждено [условиями источника](https://codexskills.dev/about/#licensing).
Сохранено [отдельное уведомление с текстом MIT](licenses/codexskills-MIT.txt); связь каждого навыка с ним указана
в поле `licenses` SNAPSHOT.json и в таблице выше.

Для источников GitHub поставлены исходные LICENSE/NOTICE либо закреплённая лицензия автора; прямые ссылки
приведены в таблице, а связь каждого навыка с файлами лицензий — в поле `licenses` SNAPSHOT.json.

Ссылка `research/corpus-lock.json` в неизменённом go-THIRD_PARTY_NOTICES.md относится к исследовательскому
корпусу upstream, а не к локальной процедуре навыка. Корпус не поставляется; закреплённый источник:
[corpus-lock.json](https://github.com/ashwingopalsamy/golangskills.com/blob/acadc48f8ede49d9889a7e02da70fa1bfbd61b3d/research/corpus-lock.json).
