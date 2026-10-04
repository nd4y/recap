// Runs in the same serialized workflow as Scan. No public webhook required.
const fs = require('fs');
const path = require('path');
const token = $env.TELEGRAM_BOT_TOKEN;
const chat = String($env.TELEGRAM_CHAT_ID || '');
if (!token || !chat) return [{ json: {} }];
const state = '/data/state/telegram-offset.json';
const failures = '/data/state/telegram-failures.json';
const dir = '/data/recordings/Telegram';
const atomic = (file, data) => {
  fs.writeFileSync(file + '.tmp', data);
  fs.renameSync(file + '.tmp', file);
};
const api = async (method, body) => {
  const r = await this.helpers.httpRequest({ method: 'POST',
    url: `https://api.telegram.org/bot${token}/${method}`,
    body, json: true, timeout: 30000 });
  if (!r.ok) throw new Error('Telegram API rejected request');
  return r.result;
};
let offset = 0;
let failed = {};
if (fs.existsSync(failures)) failed = JSON.parse(fs.readFileSync(failures, 'utf8'));
if (fs.existsSync(state)) offset = JSON.parse(fs.readFileSync(state, 'utf8')).offset;
fs.mkdirSync('/data/state', { recursive: true });
fs.mkdirSync(dir, { recursive: true });
try {
  const updates = await api('getUpdates', { offset, limit: 5, timeout: 0,
    allowed_updates: ['message'] });
  for (const u of updates) {
    const m = u.message;
    // Forward origin can be hidden. Authorization is based on the receiving chat.
    if (m && m.chat.type === 'private' && String(m.chat.id) === chat) {
      const media = m.voice || m.video_note;
      const reply = async (text) => api('sendMessage', { chat_id: chat, text,
        reply_parameters: { message_id: m.message_id, allow_sending_without_reply: true } });
      if (media) {
        const ext = m.voice ? '.ogg' : '.mp4';
        const name = `${m.chat.id}_${m.message_id}${ext}`;
        const dest = path.join(dir, name);
        const sidecar = dest + '.json';
        if ((failed[u.update_id] || 0) >= 3) {
          await reply('Не удалось скачать запись после 3 попыток. Перешлите её ещё раз или положите файл в каталог записей Recap на NAS.');
          delete failed[u.update_id];
          atomic(failures, JSON.stringify(failed));
          offset = u.update_id + 1;
          atomic(state, JSON.stringify({ offset }));
          continue;
        }
        if (!fs.existsSync(dest)) {
          if (media.file_size > 20 * 1024 * 1024) {
            await reply('Файл больше 20 МБ. Положите его в каталог записей Recap на NAS.');
          } else {
            try {
              const f = await api('getFile', { file_id: media.file_id });
              if (!/^[a-zA-Z0-9_./-]+$/.test(f.file_path || '') || f.file_path.includes('..'))
                throw new Error('Invalid Telegram file path');
              const bytes = await this.helpers.httpRequest({ method: 'GET',
                url: `https://api.telegram.org/file/bot${token}/${f.file_path}`,
                encoding: 'arraybuffer', json: false, timeout: 60000 });
              const data = Buffer.from(bytes);
              if (!data.length || data.length > 20 * 1024 * 1024)
                throw new Error('Invalid Telegram download size');
              const origin = m.forward_origin;
              const sender = origin && origin.sender_user;
              const author = sender ? [sender.first_name, sender.last_name].filter(Boolean).join(' ')
                : (origin && origin.sender_user_name) || null;
              // Sidecar first, media last: Scan never observes incomplete downloads.
              atomic(sidecar, JSON.stringify({ telegram: { chat_id: m.chat.id,
                message_id: m.message_id, kind: m.voice ? 'voice' : 'video_note',
                author, forwarded: !!origin, original_date: origin && origin.date,
                received_date: m.date } }));
              atomic(dest, data);
              delete failed[u.update_id];
              atomic(failures, JSON.stringify(failed));
            } catch (e) {
              failed[u.update_id] = (failed[u.update_id] || 0) + 1;
              atomic(failures, JSON.stringify(failed));
              throw new Error('Telegram download failed');
            }
          }
        }
      } else if (m.text === '/start' || m.text === '/help') {
        await reply('Пришлите или перешлите голосовое сообщение либо видеокружочек. Recap пришлёт выжимку и полный транскрипт вложением. Обработка запускается каждые 5 минут; время зависит от очереди.');
      }
    }
    // Confirm only after a durable download. A crash replays this update safely.
    offset = u.update_id + 1;
    atomic(state, JSON.stringify({ offset }));
  }
} catch (e) {
  // Never include HTTP error text: it can contain the bot token in the URL.
  // Leave offset in place for a retry, and keep the NAS recording pipeline running.
  console.warn('Recap: Telegram intake failed; pending updates will be retried.');
}
return [{ json: {} }];
