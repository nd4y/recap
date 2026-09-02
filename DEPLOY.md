# Развёртывание recap

Пошаговая установка с нуля. Архитектура и обоснования решений — в [TZ.md](TZ.md).

## Что понадобится

- **NAS** (или любой Linux-хост с Docker), на котором лежат записи и Obsidian vault.
  Скрипт доставки заметок написан под Synology DSM; для другого NAS см. §2.5.
- **GPU-хост** с Windows: видеокарта NVIDIA с CUDA, 12 ГБ VRAM или больше
  (GigaAM + pyannote и LLM работают по очереди, вместе не помещаются), Python 3.12, Ollama.
- **Telegram-бот** (создаётся в BotFather за минуту).
- **Obsidian vault**, синхронизируемый с NAS (клиент синхронизации NAS, не Obsidian Sync).
- Учётная запись **Hugging Face** — модели pyannote gated, нужен read-токен.

Сеть: с NAS должны быть доступны порты GPU-хоста `8000/tcp` (recap-asr) и `11434/tcp` (Ollama).
Всё живёт в локальной сети, аутентификации у этих сервисов нет.

Далее в командах: `<base>` — каталог установки на GPU-хосте (например `D:\recap-asr`),
`<gpu-host>` — его адрес, `<nas>` — адрес NAS.

## 1. GPU-хост

### 1.1 Файлы и venv

Скопировать содержимое `recap-asr/` в `<base>`. В PowerShell:

```powershell
cd <base>
py -3.12 -m venv venv
.\venv\Scripts\pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
.\venv\Scripts\pip install -r requirements.txt
```

Версии torch/torchaudio именно 2.8.0: torchaudio 2.9 ломает pyannote 3.3.2 на импорте.
Индекс `cu128` — для карт Blackwell (RTX 50xx); для старых карт подойдёт `cu124`.

GigaAM на PyPI нет. Если на хосте есть git:

```powershell
.\venv\Scripts\pip install git+https://github.com/salute-developers/GigaAM.git
```

Если git нет — из архива:

```powershell
Invoke-WebRequest https://github.com/salute-developers/GigaAM/archive/refs/heads/main.zip -OutFile gigaam.zip
Expand-Archive gigaam.zip -DestinationPath .
.\venv\Scripts\pip install .\GigaAM-main
```

Если установка GigaAM подтянула другую версию torch — повторить первую команду с torch 2.8.0.

### 1.2 ffmpeg

gigaam зовёт `ffmpeg` по имени даже для wav, а у службы Windows нет пользовательского PATH.
Положить статическую сборку в `<base>\bin\ffmpeg.exe` (любая сборка, например с gyan.dev).
Другой путь — через `RECAP_FFMPEG_DIR` в xml службы.

### 1.3 Hugging Face

1. На huggingface.co принять условия моделей `pyannote/speaker-diarization-3.1` и
   `pyannote/segmentation-3.0` (обе gated).
2. Создать read-токен: Settings → Access Tokens.

Токен понадобится в §1.4 и §1.6.

### 1.4 Первый запуск вручную

Перед установкой службы убедиться, что всё поднимается:

```powershell
$env:HF_TOKEN = "hf_..."
$env:HF_HOME = "<base>\hf-cache"
.\venv\Scripts\python.exe server.py
```

В другом окне:

```powershell
curl http://127.0.0.1:8000/health
curl -F "file=@<любой m4a>" -F num_speakers=2 http://127.0.0.1:8000/v1/audio/transcriptions
```

Первый запрос качает веса (GigaAM в `<base>\gigaam-cache`, pyannote в `HF_HOME`) и занимает несколько минут.
Дальше звонок на 1–2 минуты обрабатывается за 15–20 с, включая загрузку моделей в VRAM.
Ожидаемый ответ — JSON с `segments` и `speakers`; `is_owner` пока везде `false`, эталона ещё нет.

### 1.5 Эталон голоса владельца

Взять 2–3 исходящих звонка **разным** собеседникам, где владелец явно говорит:

```powershell
.\venv\Scripts\python.exe enroll.py call1.m4a call2.m4a call3.m4a
```

Скрипт диаризует каждый звонок на двух спикеров, находит голос, повторяющийся во всех, и сохраняет
`<base>\owner_ref.npy`. В конце печатает sanity-check «owner vs other» по каждому звонку:
владелец должен быть заметно выше `0.5`, собеседники — заметно ниже (на реальных данных 0.7–0.9 против 0.0–0.2).
Если разрыва нет — взять другие звонки.

### 1.6 Служба Windows

1. Скачать WinSW 2.x (`WinSW-x64.exe`), положить в `<base>` как `recap-asr-service.exe`.
2. В `recap-asr-service.xml` заменить `__HF_TOKEN__` на токен. Пути в xml заданы через `%BASE%`
   (каталог exe), править их не нужно.
3. Установить и запустить:

```powershell
.\recap-asr-service.exe install
.\recap-asr-service.exe start
.\recap-asr-service.exe status
curl http://127.0.0.1:8000/health
```

Служба Automatic, после перезагрузки поднимается сама. Логи — `<base>\logs`.

### 1.7 Firewall

Открыть 8000/tcp только для локальной сети:

```powershell
netsh advfirewall firewall add rule name="recap-asr" dir=in action=allow protocol=TCP localport=8000 remoteip=<LAN-подсеть>
```

Проверить с NAS: `curl http://<gpu-host>:8000/health`.

### 1.8 Ollama

1. Ollama должна слушать не только localhost: системная переменная окружения `OLLAMA_HOST=0.0.0.0`,
   перезапустить Ollama. Firewall на 11434/tcp — по аналогии с §1.7.
2. Модель:

```powershell
ollama pull gemma3:12b
ollama create gemma3-recap -f llm\Modelfile
```

Производная модель нужна из-за контекста: OpenAI-совместимый endpoint Ollama игнорирует `num_ctx`
из запроса, а дефолтных 4–8k на длинную запись не хватает.

Проверить с NAS: `curl http://<gpu-host>:11434/v1/models` — в списке должна быть `gemma3-recap`.

Вместо Ollama можно указать любой OpenAI-совместимый провайдер (§2.2, переменные `LLM_*`).

## 2. NAS

### 2.1 Каталоги

| Что | Переменная | Пример |
|---|---|---|
| данные n8n: внутри появятся `n8n/` (home, SQLite) и `state/` (реестр, outbox) | `N8N_DATA_DIR` | `/volume1/docker/n8n` |
| записи; подкаталог = тип: `CallRecords`, `Meetings`, `Jitsi`, `Voice` | `RECORDINGS_DIR` | `/volume1/homes/user/Recordings` |
| корень Obsidian vault; заметки попадут в `Notes/Recordings/<год>/` | `VAULT_DIR` | `/volume1/homes/user/Obsidian` |

Контейнер должен писать в vault и читать записи, поэтому запускается от uid:gid владельца этих каталогов
(`id <user>` в ssh на NAS) — иначе запись упрётся в ACL. Создать `N8N_DATA_DIR/n8n` и `N8N_DATA_DIR/state`
заранее и отдать тому же владельцу.

### 2.2 Параметры стека

Скопировать `stack/.env.example` в `stack/.env` и заполнить. Обязательные: `N8N_UID`, `N8N_GID`,
`N8N_DATA_DIR`, `RECORDINGS_DIR`, `VAULT_DIR`, `RECAP_ASR_URL`, `LLM_BASE_URL`, `TZ`.

- `RECAP_TZ_OFFSET_HOURS` — смещение локального времени от UTC в часах (без DST), например `3` для Москвы.
  Нужно для дат из имён файлов Jitsi и из mtime: task runner n8n не наследует `TZ` контейнера.
- `RECAP_OWNER_NAME` — как называть владельца в summary («Иван попросил…»).
- `TELEGRAM_*` можно оставить пустыми до §3 — без токена пайплайн работает, просто не шлёт сообщения.
- `RECAP_TG_MAX_AGE_DAYS` — записи старше в Telegram не отправляются. При первом прогоне архива это
  спасает от сотни сообщений.

### 2.3 Запуск стека

Docker compose на NAS:

```bash
cd stack
docker compose --env-file .env up -d
```

Portainer: Stacks → Add stack → содержимое `stack/docker-compose.yml`, переменные из `.env` — в раздел
Environment variables (compose читает их оттуда). Стек называется `n8n`: движок общий, recap — его первый workflow.

Проверка: `curl http://<nas>:<N8N_PORT>/healthz` → `{"status":"ok"}`, UI открывается в браузере.
Владельца n8n пока не создавать — это сделает скрипт в §4.

### 2.4 Доставка заметок в vault (Synology DSM)

Клиент синхронизации NAS не видит файлы, которые контейнер пишет в vault напрямую. Поэтому workflow
складывает заметки в `N8N_DATA_DIR/state/outbox/`, а переносит их в vault скрипт через штатный API NAS.

1. Скопировать `stack/deliver-outbox.sh` в `N8N_DATA_DIR`.
2. Рядом создать `deliver-outbox.env` по `stack/deliver-outbox.env.example`:
   `OUTBOX_DIR` — путь outbox на ФС, `OUTBOX_SHARE_PATH` — он же в нотации общих папок DSM (без `/volume1`),
   `DEST_BASE` — каталог `Notes/Recordings` внутри vault в той же нотации.
3. DSM: Панель управления → Планировщик задач → Создать → Запланированная задача → Пользовательский скрипт.
   Пользователь `root`, расписание каждые 5 минут, скрипт:

```bash
bash /volume1/docker/n8n/deliver-outbox.sh
```

Лог последнего прогона — `deliver-outbox.log` рядом со скриптом (путь меняется переменной `LOG`).

### 2.5 Если NAS не Synology

Outbox остаётся, меняется только скрипт переноса. Если клиент синхронизации видит обычные операции
файловой системы — достаточно cron с `mv "$OUTBOX_DIR"/*/*.md` в соответствующий `Notes/Recordings/<год>/`.
Если нет — использовать API этого NAS по аналогии с `deliver-outbox.sh`.

## 3. Telegram

1. В BotFather: `/newbot` → токен.
2. Написать боту любое сообщение (например `/start`) со своего аккаунта.
3. Узнать `chat_id`:

```bash
curl -s "https://api.telegram.org/bot<токен>/getUpdates"
```

В ответе `message.chat.id`. Если `result` пустой — запустить `getUpdates` с параметром `timeout=50`
и отправить сообщение боту, пока запрос висит: обновления не хранятся долго.

4. `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` в `.env`, перезапустить стек (`docker compose up -d`
   или Update the stack в Portainer).

Бот только отправляет: сообщение с summary и следом файл с транскриптом. Ошибки пайплайна приходят им же.

## 4. Workflow в n8n

Скрипт деплоя запускается с любой машины, где есть Python 3 и доступ к n8n по сети (в том числе с самого NAS).

1. `workflow/.n8n.env` по `workflow/.n8n.env.example`: адрес n8n, email владельца, часовой пояс workflow (IANA).
2. Запуск:

```bash
cd workflow
python3 deploy_workflows.py
```

Что делает:

- создаёт владельца n8n с email из `.n8n.env` и паролем, который сам генерирует и кладёт в
  `workflow/.n8n-owner-password` (gitignored). Если владелец уже создан через UI — записать его пароль
  в этот файл до запуска;
- создаёт API-ключ;
- создаёт или обновляет два workflow: `recap-errors` (перехват ошибок → Telegram) и `recap-pipeline`
  (сам конвейер, тик каждые 5 минут); `recap-errors` прописывается как error workflow пайплайна;
- активирует `recap-pipeline`.

Скрипт идемпотентный: повторный запуск обновляет workflow на месте. После правок в `workflow/*.js`
раскатывать так же.

## 5. Проверка

1. В UI n8n оба workflow видны, `recap-pipeline` активен.
2. Положить тестовую запись в `RECORDINGS_DIR/CallRecords/`. Файлы моложе 3 минут пропускаются
   (защита от недокачанных), так что первый тик пройдёт мимо — это нормально.
3. Через 5–10 минут:
   - в `N8N_DATA_DIR/state/registry.json` у файла статус `done` и `note_path`;
   - в `N8N_DATA_DIR/state/outbox/<год>/` заметка (появляется и исчезает после прогона планировщика);
   - в vault `Notes/Recordings/<год>/` заметка, клиент синхронизации её подхватил;
   - в Telegram сообщение и вложение.
4. Если что-то не так — `workflow/diag_exec.py` покажет последние экзекьюшены и ошибку последнего упавшего,
   `workflow/dump_exec.py <id>` выгрузит экзекьюшен целиком.

## 6. Эксплуатация

**Реестр** `state/registry.json`: статус на каждый файл. `done` / `failed` / `skipped` — терминальные,
`processing` — в работе (claim протухает через 2 часа), `noted` — заметка записана, Telegram ещё не отправлен.
Повторить файл — удалить его запись из реестра. Перегнать весь архив заново — удалить реестр целиком
и заметки из vault; с `RECAP_TG_MAX_AGE_DAYS` старое в Telegram не пойдёт.

**Выключенный GPU-хост** — не ошибка: файлы ждут в очереди, попытки не сгорают.
**Битый файл** (ASR отвечает 4xx) — `failed` сразу, с текстом ошибки в реестре.
**Прочие ошибки** — 3 попытки, затем `failed` и сообщение в Telegram.

**Новый тип записи**: подкаталог сразу обрабатывается как `generic`. Свой промпт — добавить в `typeMap`
в `workflow/scan.js` и в `prompts` в `workflow/build_prompt.js`, раскатать `deploy_workflows.py`.

**Смена LLM**: переменные `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` (и `LLM_PROXY`, если провайдер
недоступен напрямую), перезапуск стека. Формат — OpenAI `chat/completions`, workflow не меняется.

**Обновление**: GPU-хост — заменить `server.py`/`enroll.py`, `recap-asr-service.exe restart`;
workflow — `deploy_workflows.py`; стек — `docker compose up -d` с новым compose.

**Бэкап**: `N8N_DATA_DIR` (SQLite n8n, реестр) и `owner_ref.npy`. Модели качаются заново.

## 7. Известные грабли

- **Дата/время в заметках сдвинуты** — task runner n8n не наследует `TZ`; выставить `RECAP_TZ_OFFSET_HOURS`.
- **«Access to the file is not allowed»** в ноде чтения файла — пути должны быть в `N8N_RESTRICT_FILE_ACCESS_TO`
  (в compose уже перечислены `/data/recordings;/data/state;/data/vault`).
- **Ошибка записи в vault или state** — uid:gid контейнера не совпадает с владельцем каталогов.
- **Заметки лежат в outbox и не переезжают** — смотреть `deliver-outbox.log`; чаще всего неверная нотация
  путей в `deliver-outbox.env` (для DSM без `/volume1`).
- **LLM отдаёт невалидный JSON** — workflow делает вторую попытку, затем пишет заметку с сырым ответом.
  Если систематически — проверить, что модель `gemma3-recap` создана с `num_ctx 16384`.
- **pyannote не импортируется** — версии из §1.1 (`torchaudio` 2.8.0, `huggingface_hub<1.0`, `matplotlib`).
- **403 от Hugging Face** — не приняты условия одной из двух gated-моделей.
- **OSError 448 «недоверенная точка подключения»** на GPU-хосте — reparse-каталог WinGet Links в PATH;
  `server.py` вычищает его сам, но при ручных запусках из другого окружения может всплыть.
- **Служба стартует, `/health` нет** — смотреть `<base>\logs\recap-asr.err.log`; типично: нет `bin\ffmpeg.exe`
  или токен HF не подставлен в xml.
- **Ollama отвечает 404 на `/v1/chat/completions`** — в `LLM_BASE_URL` должен быть суффикс `/v1`
  (`http://<gpu-host>:11434/v1`), workflow добавляет только `/chat/completions`.

## 8. Метрики

Три эндпойнта в формате Prometheus. Аутентификации нет ни у одного — открывать только в LAN.

| Источник | Адрес | Что даёт |
|---|---|---|
| n8n | `http://<nas>:<N8N_PORT>/metrics` | экзекьюшены по workflow (`n8n_workflow_*`), процесс node.js (`n8n_process_*`, `n8n_nodejs_*`), `n8n_version_info` |
| recap-exporter | `http://<nas>:<RECAP_EXPORTER_PORT>/metrics` (дефолт 9819) | реестр и outbox: файлы по типу и статусу, секунды аудио / ASR / LLM, когда последний файл получил статус, возраст последнего тика, заметки в очереди на доставку |
| recap-asr | `http://<gpu-host>:8000/metrics` | запросы по результату (`ok`/`skipped`/`undecodable`/`error`), секунды аудио, время по стадиям (`decode`/`diarization`/`asr`), гистограмма длительности запроса, найден ли владелец, загруженные в VRAM модели |

Полный список метрик экспортера — в шапке `stack/recap-exporter.py`.

**Включение.** В стеке метрики n8n включены по умолчанию (`N8N_METRICS=true`); экспортер — сервис
`recap-exporter` того же compose, его скрипт `stack/recap-exporter.py` нужно положить в `N8N_DATA_DIR`
(монтируется в контейнер файлом, образ `python:alpine` без сборки). У сервиса ASR ничего включать не нужно.

**Скрейп** (Prometheus / vmagent):

```yaml
  - job_name: n8n
    static_configs:
      - targets: ['nas.local:5678']
  - job_name: recap-exporter
    static_configs:
      - targets: ['nas.local:9819']
  - job_name: recap-asr
    static_configs:
      - targets: ['gpu-host.local:8000']
```

Выключенный GPU-хост даёт `up{job="recap-asr"} = 0` — это норма, алерт на него не нужен.

**Дашборды** — Dashboards → New → Import, при импорте выбрать Prometheus-совместимый datasource:

- `grafana/recap.json` — конвейер: реестр, тайминги, outbox, последний тик, сервис ASR, сводка по n8n;
- `grafana/n8n.json` — движок n8n как сервис: экзекьюшены по workflow и статусу, длительности, процесс
  node.js (память, CPU, event loop, GC), контейнер по cAdvisor (ряд пуст, если cAdvisor не скрейпится).
  Переменные `job` (скрейп-job n8n) и `container` (имя контейнера, дефолт `n8n`).

**Алерты.** Переход файла в `failed` (битый файл или исчерпанные попытки) workflow сообщает в Telegram сам,
без внешнего алертинга. По метрикам имеет смысл завести три правила в Grafana:

| Условие | Смысл |
|---|---|
| `time() - recap_registry_updated_timestamp_seconds > 900` | тики workflow остановились: n8n лежит или workflow деактивирован |
| `recap_outbox_oldest_age_seconds > 1800` | заметки не доезжают до vault: планировщик NAS не отработал |
| `increase(recap_registry_files{status="failed"}[1h]) > 0` | файл помечен `failed` (дублирует сообщение из Telegram на случай, если бот не настроен) |

**Если тайминги пустые.** `recap_registry_{audio,asr,llm}_seconds_*` считаются только по файлам, у которых в
реестре есть поля `duration` / `asr_seconds` / `llm_seconds` — их пишут ноды `Save note` с этой версии workflow.
Файлы, обработанные раньше, в эти суммы не входят; в остальные метрики входят все.
