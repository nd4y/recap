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
| `stack/` | compose стека n8n + скрипт доставки заметок в vault | NAS, docker + планировщик |

Параметры установки — в env-файлах, примеры: `stack/.env.example`,
`stack/deliver-outbox.env.example`, `workflow/.n8n.env.example`.

## Потоки

- Источник: `<recordings>/<Подкаталог>/` на NAS, подкаталог = тип записи (звонок, встреча, голосовая заметка).
- Заметки: outbox -> штатный API NAS (планировщик) -> `Notes/Recordings/<год>/` в Obsidian vault -> клиент синхронизации разносит по устройствам. Прямые записи в ФС клиент синхронизации не видит, потому outbox.
- Telegram: summary + транскрипт вложением, только для записей свежее `RECAP_TG_MAX_AGE_DAYS` (дефолт 2 дня).

## Секреты

В репозитории секретов нет: токены Telegram и Hugging Face, `chat_id` и ключ внешней LLM живут в env стека и в env службы (в `recap-asr-service.xml` плейсхолдер).
