// Run where n8n-workflow is installed. This uses n8n's parser, not JavaScript eval.
const assert = require('assert').strict;
const fs = require('fs');
const path = require('path');
const { Workflow } = require('n8n-workflow');
const source = fs.readFileSync(path.join(__dirname, 'deploy_workflows.py'), 'utf8');
const expression = source.match(/"jsonBody": "(=\{\{ JSON.stringify\(\{chat_id: \$env.TELEGRAM_CHAT_ID, text: \$json.tgText[^\n]+)"/)[1];
const node = { name: 'TG message', type: 'n8n-nodes-base.httpRequest', typeVersion: 4.2,
  position: [0, 0], parameters: {} };
const workflow = new Workflow({ id: 'expression-test', name: 'expression-test', active: false,
  nodes: [node], connections: {}, nodeTypes: { getByNameAndVersion: () => ({ description: { properties: [] } }) } });
for (const telegram of [{ message_id: 123 }, null]) {
  const value = workflow.expression.getParameterValue(expression, null, 0, 0, node.name,
    [{ json: { tgText: 'summary', telegram } }], 'internal', {});
  const payload = JSON.parse(value);
  assert.equal(payload.text, 'summary');
  if (telegram) assert.deepEqual(payload.reply_parameters, { message_id: 123, allow_sending_without_reply: true });
  else assert(!Object.hasOwn(payload, 'reply_parameters'));
}
console.log('PASS: Telegram and ordinary recording payloads use valid n8n expressions');
