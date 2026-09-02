# recap

Конвейер транскрибации и суммаризации аудиозаписей:
NAS -> GPU-транскрибация с диаризацией (GigaAM v3 + pyannote, Windows-служба) ->
summary (локальная или любая OpenAI-совместимая LLM) ->
заметка в Obsidian + сообщение в Telegram.

Полное описание архитектуры и решений: [TZ.md](TZ.md).
Установка с нуля: [DEPLOY.md](DEPLOY.md).

## Состав

| Каталог | Что | Где работает |
|---|---|---|
| `recap-asr/` | сервис транскрибации + диаризации, эталон голоса владельца (нативная Windows-служба, без Docker) | GPU-хост |
| `llm/` | `Modelfile` производной модели Ollama (контекст 16k) | GPU-хост |
| `workflow/` | workflow n8n: код нод + скрипт деплоя через REST API | стек `n8n` на NAS |
| `stack/` | compose стека n8n + экспортер метрик конвейера + скрипт доставки заметок в vault | NAS, docker + планировщик |
| `grafana/` | дашборды: конвейер recap и движок n8n (импортируемые JSON) | Grafana |

Параметры установки — в env-файлах, примеры: `stack/.env.example`,
`stack/deliver-outbox.env.example`, `workflow/.n8n.env.example`.

## Метрики

Три эндпойнта в формате Prometheus, без аутентификации (только LAN): `/metrics` n8n на порту UI,
`recap-exporter` (реестр и outbox конвейера, порт 9819) и `/metrics` сервиса `recap-asr`.
Переход файла в `failed` workflow сам сообщает в Telegram. Дашборды — `grafana/recap.json` (конвейер)
и `grafana/n8n.json` (движок), подробности — DEPLOY.md, раздел «Метрики».

## Потоки

- Источник: `<recordings>/<Подкаталог>/` на NAS, подкаталог = тип записи (звонок, встреча, голосовая заметка).
- Заметки: outbox -> штатный API NAS (планировщик) -> `Notes/Recordings/<год>/` в Obsidian vault -> клиент синхронизации разносит по устройствам. Прямые записи в ФС клиент синхронизации не видит, потому outbox.
- Telegram: summary + транскрипт вложением, только для записей свежее `RECAP_TG_MAX_AGE_DAYS` (дефолт 2 дня).

## Секреты

В репозитории секретов нет: токены Telegram и Hugging Face, `chat_id` и ключ внешней LLM живут в env стека и в env службы (в `recap-asr-service.xml` плейсхолдер).
