'use strict';
const el = (id) => document.getElementById(id);
const money = new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB', maximumFractionDigits: 2 });
const dates = new Intl.DateTimeFormat('ru-RU', { dateStyle: 'long', timeStyle: 'short' });
let mode = 'login';
let nextOffset = null;
function prepareGuestCart(user) {
  try {
    const legacyKey = 'okunev-order-cart-v1';
    const guestKey = 'okunev-order-cart-v2:guest';
    const legacy = localStorage.getItem(legacyKey);
    if (!legacy) return;
    if (!user && !localStorage.getItem(guestKey)) {
      const draft = JSON.parse(legacy);
      if (draft && Array.isArray(draft.items)) localStorage.setItem(guestKey, legacy);
    }
    localStorage.removeItem(legacyKey);
  } catch (_) { /* Accounts still work when browser storage is unavailable. */ }
}
function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}
function message(text, error = false) {
  el('history-feedback').textContent = text;
  el('history-feedback').hidden = !text;
  el('history-feedback').classList.toggle('error', error);
}
async function jsonApi(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } });
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Не удалось выполнить действие. Проверьте данные.');
  return body;
}
function accountView(user) {
  el('account-link').textContent = user ? 'Мой аккаунт' : 'Войти';
  el('account-card').hidden = Boolean(user);
  el('account-bar').hidden = !user;
  el('account-user').textContent = user?.email || '';
  if (!user) { el('history-list').replaceChildren(); el('history-empty').hidden = true; el('history-more').hidden = true; }
}
function setMode(value) {
  mode = value;
  for (const kind of ['login', 'register']) {
    const button = el('auth-' + kind);
    button.setAttribute('aria-pressed', String(kind === value));
    button.className = 'button ' + (kind === value ? 'button-primary' : 'button-quiet');
  }
  el('account-heading').textContent = value === 'login' ? 'Войдите в свой аккаунт' : 'Создайте свой аккаунт';
  el('account-submit').textContent = value === 'login' ? 'Войти' : 'Создать аккаунт';
  el('account-password').autocomplete = value === 'login' ? 'current-password' : 'new-password';
}
async function download(order) {
  try {
    const response = await fetch('/api/orders/history/' + encodeURIComponent(order.id) + '/file');
    if (!response.ok) {
      const body = await response.json();
      throw new Error(typeof body.detail === 'string' ? body.detail : 'Не удалось скачать заказ.');
    }
    const url = URL.createObjectURL(await response.blob());
    const link = node('a'); link.href = url; link.download = order.filename;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (error) { message(error.message || 'Нет связи с сайтом.', true); }
}
function orderCard(order) {
  const card = node('article', 'history-card');
  const heading = node('div', 'history-card-heading');
  const created = new Date(order.created_at);
  heading.append(node('h2', '', 'Заказ от ' + (Number.isNaN(created.getTime()) ? order.created_at : dates.format(created))), node('strong', '', money.format(Number(order.total))));
  card.append(heading, node('p', 'product-meta', '№ ' + order.id));
  const status = { exported: 'Excel сформирован', sent: 'Отправлен на почту', pending: 'Отправляется', failed: 'Отправка не удалась' };
  card.append(node('p', 'history-status', status[order.status] || order.status));
  const details = node('details');
  details.append(node('summary', '', 'Позиции заказа: ' + order.positions.length));
  const list = node('ul', 'history-positions');
  for (const position of order.positions) {
    const row = node('li');
    row.append(node('span', '', position.name), node('span', '', String(position.quantity) + ' ' + (position.unit || 'шт') + ' · ' + money.format(Number(position.line_total))));
    list.append(row);
  }
  details.append(list); card.append(details);
  const button = node('button', 'button button-outline', 'Скачать Excel'); button.type = 'button';
  button.addEventListener('click', () => download(order)); card.append(button);
  return card;
}
async function loadOrders(append = false) {
  const result = await jsonApi('/api/orders/history?offset=' + (append ? nextOffset : 0));
  if (!append) el('history-list').replaceChildren();
  for (const order of result.orders) el('history-list').append(orderCard(order));
  nextOffset = result.next_offset;
  el('history-more').hidden = nextOffset === null || nextOffset === undefined;
  el('history-empty').hidden = result.total > 0;
}
async function start() {
  try {
    const result = await jsonApi('/api/accounts/me');
    prepareGuestCart(result.user);
    if (!result.available) {
      el('account-card').hidden = true; el('account-bar').hidden = true;
      message('Хранилище заказов ещё не подключено. Пока можно собрать корзину и скачать Excel.');
      return;
    }
    accountView(result.user); message('');
    if (result.user) await loadOrders();
  } catch (error) { message(error.message || 'Не удалось загрузить историю.', true); }
}
el('auth-login').addEventListener('click', () => setMode('login'));
el('auth-register').addEventListener('click', () => setMode('register'));
el('account-form').addEventListener('submit', async (event) => {
  event.preventDefault(); el('account-submit').disabled = true; message('');
  try {
    const result = await jsonApi('/api/accounts/' + mode, { method: 'POST', body: JSON.stringify({ email: el('account-email').value.trim(), password: el('account-password').value }) });
    accountView(result.user); await loadOrders();
  } catch (error) { message(error.message || 'Вход не удался.', true); }
  finally { el('account-password').value = ''; el('account-submit').disabled = false; }
});
el('account-logout').addEventListener('click', async () => {
  try { await jsonApi('/api/accounts/logout', { method: 'POST' }); accountView(null); message('Вы вышли из аккаунта.'); }
  catch (error) { message(error.message, true); }
});
el('history-more').addEventListener('click', async () => {
  el('history-more').disabled = true;
  try { await loadOrders(true); } catch (error) { message(error.message, true); }
  finally { el('history-more').disabled = false; }
});
window.addEventListener('pageshow', (event) => { if (event.persisted) window.location.reload(); });
start();
