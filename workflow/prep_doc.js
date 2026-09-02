// Prep doc: транскрипт как .md-вложение + reply на сообщение с summary.
const src = $('Save note').item.json;
const it = $input.item;
const name = (src.title || 'transcript').replace(/[^\wа-яА-ЯёЁ .\-]/g, '').slice(0, 60) + '.md';
it.json = {
  message_id: ($json.result && $json.result.message_id) || '',
  rel: src.rel,
};
it.binary = { document: await this.helpers.prepareBinaryData(Buffer.from(src.transcriptDoc, 'utf8'), name, 'text/markdown') };
return it;
