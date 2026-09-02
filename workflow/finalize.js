// Finalize: реестр -> done (сходятся обе ветки: с Telegram и без).
const fs = require('fs');
const src = $('Save note').item.json;
const rel = src.rel;
const STATE = '/data/state/registry.json';
const reg = JSON.parse(fs.readFileSync(STATE, 'utf8'));
reg[rel] = Object.assign(reg[rel] || {}, {
  status: src.skipped ? 'skipped' : 'done', ts: Date.now(),
});
fs.writeFileSync(STATE, JSON.stringify(reg, null, 1));
return { json: { rel, done: true } };
