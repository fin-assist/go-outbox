# Изменения

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии и выпуск — [docs/releasing.md](docs/releasing.md). Каждый PR, который меняет API или поведение библиотеки, добавляет строку в «Unreleased»; при выпуске раздел получает номер версии и дату.

## [Unreleased]

### Добавлено

- FIN-109: transactional Publish, fanout по конфигурации, primary-only worker с LISTEN/poll, безопасные повторы системных gRPC-вызовов, метрики и инструкция внедрения.

- FIN-214: правило Squawk `prefer-bigint-over-int` отключено; ширина целочисленных полей выбирается по диапазону домена.
