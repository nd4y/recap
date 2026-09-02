#!/bin/sh
# recap: доставка заметок из outbox в Obsidian vault через штатный API NAS
# (Synology DSM, File Station). Смысл: прямые записи контейнера в ФС клиент
# синхронизации NAS не замечает, а операции File Station генерят события синка.
# Запускается планировщиком задач NAS от root каждые 5 минут:
#   bash /path/to/deliver-outbox.sh
# Настройки — в deliver-outbox.env рядом со скриптом (см. deliver-outbox.env.example).

HERE=$(cd "$(dirname "$0")" && pwd)
CONF="$HERE/deliver-outbox.env"
[ -f "$CONF" ] || { echo "missing $CONF"; exit 1; }
. "$CONF"
: "${OUTBOX_DIR:?}" "${OUTBOX_SHARE_PATH:?}" "${DEST_BASE:?}"
API=${API:-/usr/syno/bin/synowebapi}
LOG=${LOG:-$HERE/deliver-outbox.log}

exec >"$LOG" 2>&1   # лог последнего прогона
date
[ -d "$OUTBOX_DIR" ] || { echo "no outbox"; exit 0; }

find "$OUTBOX_DIR" -type f -name '*.md' | while IFS= read -r f; do
  rel="${f#$OUTBOX_DIR/}"
  sub=$(dirname "$rel")   # подкаталог года: 2026
  dest="$DEST_BASE/$sub"
  src="$OUTBOX_SHARE_PATH/$rel"
  echo "deliver: $rel -> $dest"
  # каталог года может отсутствовать — создать (ошибка «уже существует» не важна)
  "$API" --exec api=SYNO.FileStation.CreateFolder method=create version=2 \
    folder_path="\"$DEST_BASE\"" name="\"$sub\""
  "$API" --exec api=SYNO.FileStation.CopyMove method=start version=3 \
    path="[\"$src\"]" dest_folder_path="\"$dest\"" remove_src=true
done
echo "run finished"
# перенесённые файлы CopyMove удаляет сам (remove_src);
# не перенёсшиеся останутся в outbox и попадут в следующий прогон
