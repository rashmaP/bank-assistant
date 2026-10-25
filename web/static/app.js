const log = document.getElementById('log'), input = document.getElementById('in'), go = document.getElementById('go');
let token = null, sessionId = null;

// All server text is inserted with textContent: transaction narrations are third-party data.
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function add(node) { log.appendChild(node); log.scrollTop = log.scrollHeight; return node; }
function row(dl, k, v) { dl.appendChild(el('dt', null, k)); dl.appendChild(el('dd', null, v)); }

function renderCard(c) {
  const box = el('section', 'card'), d = c.data;
  if (c.type === 'balance') {
    box.appendChild(el('h2', null, `${d.account_type} account ${d.account_number_masked}`));
    const dl = el('dl'); row(dl, 'Available balance', d.available_balance_display); row(dl, 'Ledger balance', d.ledger_balance_display);
    row(dl, 'Pending holds', d.pending_holds_display); row(dl, 'As of', d.as_of); box.appendChild(dl);
  } else if (c.type === 'transactions') {
    box.appendChild(el('h2', null, `Matching transactions (${d.total_matches})`));
    const ul = el('ul');
    d.items.forEach(t => { const li = el('li'); const left = el('div', null, t.merchant);
      left.appendChild(el('small', null, `${t.date} · ${t.channel}${t.status === 'pending' ? ' · pending' : ''}`));
      li.appendChild(left); li.appendChild(el('div', null, `${t.direction === 'debit' ? '−' : '+'}${t.amount_display}`)); ul.appendChild(li); });
    box.appendChild(ul);
  } else if (c.type === 'service_request') {
    box.appendChild(el('h2', null, 'Cheque book request raised'));
    const dl = el('dl'); row(dl, 'Request ID', d.request_id); row(dl, 'Account', `${d.account_type} ${d.account_number_masked}`);
    row(dl, 'Leaves', String(d.leaves)); row(dl, 'Delivery', d.delivery); row(dl, 'Expected by', d.expected_delivery_date); box.appendChild(dl);
  } else return null;
  return box;
}

async function login() {
  const r = await fetch('/auth/demo-login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ customer_id: 'CUST-1001' }) });
  if (!r.ok) throw new Error('Demo login failed');
  const j = await r.json(); token = j.token;
  document.getElementById('who').textContent = `Signed in as ${j.customer_name} (demo)`;
  add(el('div', 'msg bot', `Hello ${j.customer_name.split(' ')[0]}. I can show your balance, explain a debit, or request a cheque book. What would you like to do?`));
  go.disabled = false; input.focus();
}

async function send(text) {
  add(el('div', 'msg me', text)); go.disabled = true; input.value = '';
  const wait = add(el('div', 'msg bot dots', 'Thinking'));
  try {
    const r = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ message: text, session_id: sessionId }) });
    wait.remove();
    if (!r.ok) { const e = await r.json().catch(() => ({})); add(el('div', 'err', e.detail || 'Something went wrong. Please try again.')); return; }
    const j = await r.json(); sessionId = j.session_id;
    (j.cards || []).forEach(c => { const n = renderCard(c); if (n) add(n); });
    add(el('div', 'msg bot', j.reply));
  } catch (e) { wait.remove(); add(el('div', 'err', 'Could not reach the assistant. Check your connection and try again.')); }
  finally { go.disabled = false; input.focus(); }
}

document.getElementById('f').addEventListener('submit', e => { e.preventDefault(); const t = input.value.trim(); if (t && token) send(t); });
const chips = document.getElementById('chips');
['What\u2019s my balance?', 'What was the debit of 649 rupees?', 'I need a cheque book'].forEach(t => {
  const b = el('button', null, t); b.type = 'button'; b.addEventListener('click', () => token && !go.disabled && send(t)); chips.appendChild(b);
});
login().catch(() => add(el('div', 'err', 'Could not sign in to the demo. Is the assistant running with DEMO_AUTH_ENABLED=true?')));
