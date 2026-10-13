const log = document.getElementById('log');
const form = document.getElementById('step-form');
const stepInput = document.getElementById('step');
const sendButton = document.getElementById('send');
let previousResponseId = null;

function add(className, text) {
  const div = document.createElement('div');
  div.className = `msg ${className}`;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

function addToolCall(event) {
  const details = document.createElement('details');
  const output = event.detail ?? '';
  details.className = 'tool' + (event.kind === 'error' || output.startsWith('ERROR') ? ' failed' : '');
  const summary = document.createElement('summary');
  summary.textContent = `MCP ${event.title}`;
  const pre = document.createElement('pre');
  pre.textContent = output || '(no output)';
  details.append(summary, pre);
  log.appendChild(details);
}

async function runStep(message) {
  add('user', message);
  const pending = add('pending', 'Agent is working on the green screen...');
  sendButton.disabled = true;
  try {
    const response = await fetch('api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, previousResponseId })
    });
    const body = await response.json();
    pending.remove();
    if (!response.ok) {
      add('error', body.error || `Request failed (${response.status}).`);
      return;
    }
    previousResponseId = body.responseId;
    (body.events || []).forEach(addToolCall);
    add('agent', body.text || '(no response)');
  } catch (error) {
    pending.remove();
    add('error', String(error));
  } finally {
    sendButton.disabled = false;
    stepInput.focus();
    log.scrollTop = log.scrollHeight;
  }
}

form.addEventListener('submit', event => {
  event.preventDefault();
  const message = stepInput.value.trim();
  if (!message || sendButton.disabled) return;
  stepInput.value = '';
  runStep(message);
});

stepInput.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

document.getElementById('new-session').addEventListener('click', () => {
  previousResponseId = null;
  add('pending', 'Started a new conversation. The green screen session itself stays open.');
});

fetch('api/config').then(r => r.json()).then(config => {
  document.getElementById('agent-info').textContent = `${config.agent} v${config.version}`;
  const link = document.getElementById('live-link');
  link.href = config.liveUiUrl;
  document.getElementById('live').src = config.liveUiUrl;
}).catch(() => {});
