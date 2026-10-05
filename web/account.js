'use strict';

const el = (id) => document.getElementById(id);
const profileFields = { name: 'profile-name', phone: 'profile-phone', company: 'profile-company', delivery_address: 'profile-delivery-address' };
let mode = 'login';
let currentUser = null;
let busy = false;

function message(id, text, error = false) {
  const target = el(id);
  target.textContent = text;
  target.hidden = !text;
  target.classList.toggle('error', error);
}
async function jsonApi(url, options = {}) {
  const response = await fetch(url, { ...options, cache: 'no-store', headers: { 'Content-Type': 'application/json', ...options.headers } });
  let body;
  try { body = await response.json(); } catch (_) { /* The hosting proxy can return an HTML error. */ }
  if (!response.ok) {
    const error = new Error(typeof body?.detail === 'string' ? body.detail : 'Не удалось выполнить действие. Проверьте данные и попробуйте ещё раз.');
    error.status = response.status;
    throw error;
  }
  if (!body) throw new Error('Не удалось получить данные аккаунта. Попробуйте ещё раз.');
  return body;
}
function setBusy(value) {
  busy = value;
  for (const id of ['account-submit', 'auth-login', 'auth-register', 'account-logout', 'profile-save', 'account-retry']) el(id).disabled = value;
  for (const id of Object.values(profileFields)) el(id).disabled = value;
}
function prepareGuestCart(user) {
  try {
    const legacy = localStorage.getItem('okunev-order-cart-v1');
    if (!legacy) return;
    if (!user && !localStorage.getItem('okunev-order-cart-v2:guest')) {
      const draft = JSON.parse(legacy);
      if (draft && Array.isArray(draft.items)) localStorage.setItem('okunev-order-cart-v2:guest', legacy);
    }
    localStorage.removeItem('okunev-order-cart-v1');
  } catch (_) { /* Login works even when browser storage is unavailable. */ }
}
function accountView(user) {
  currentUser = user;
  el('account-link').textContent = user ? 'Мой аккаунт' : 'Войти';
  el('profile-heading').textContent = user ? 'Мой аккаунт' : 'Вход в аккаунт';
  el('account-card').hidden = Boolean(user);
  el('account-bar').hidden = !user;
  el('account-user').textContent = user?.email || '';
  el('profile-card').hidden = true;
  for (const id of [...Object.values(profileFields), 'profile-email']) el(id).value = '';
  el('account-password').value = '';
  message('profile-feedback', '');
}
function setMode(value) {
  mode = value;
  for (const kind of ['login', 'register']) {
    const active = kind === value;
    el('auth-' + kind).setAttribute('aria-pressed', String(active));
    el('auth-' + kind).className = 'button ' + (active ? 'button-primary' : 'button-quiet');
  }
  el('account-heading').textContent = value === 'login' ? 'Войдите в свой аккаунт' : 'Создайте свой аккаунт';
  el('account-submit').textContent = value === 'login' ? 'Войти' : 'Создать аккаунт';
  el('account-password').autocomplete = value === 'login' ? 'current-password' : 'new-password';
  el('account-password').value = '';
}
function fillProfile(profile) {
  for (const [field, id] of Object.entries(profileFields)) el(id).value = profile[field];
  el('profile-email').value = profile.email;
}
async function loadProfile() {
  const result = await jsonApi('/api/accounts/profile', { headers: { 'X-Account-Id': currentUser.id } });
  fillProfile(result.profile);
  el('profile-card').hidden = false;
}
function accountError(error) {
  if (error.status === 401 || error.status === 409) accountView(null);
  message('account-feedback', error.message || 'Нет связи с сайтом. Попробуйте ещё раз.', true);
  el('account-retry').hidden = false;
}
async function start() {
  if (busy) return;
  setBusy(true);
  accountView(null);
  el('account-card').hidden = true;
  el('account-retry').hidden = true;
  el('profile-card').hidden = true;
  message('account-feedback', 'Проверяем аккаунт…');
  try {
    const result = await jsonApi('/api/accounts/me');
    prepareGuestCart(result.user);
    accountView(result.user);
    if (!result.available) {
      el('account-card').hidden = true;
      message('account-feedback', 'Личный кабинет пока недоступен. Попробуйте позже.', true);
      el('account-retry').hidden = false;
      return;
    }
    if (result.user) await loadProfile();
    message('account-feedback', '');
  } catch (error) { accountError(error); }
  finally { setBusy(false); }
}

el('auth-login').addEventListener('click', () => setMode('login'));
el('auth-register').addEventListener('click', () => setMode('register'));
el('account-retry').addEventListener('click', start);
el('account-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  setBusy(true); message('account-feedback', ''); el('account-retry').hidden = true;
  try {
    const result = await jsonApi('/api/accounts/' + mode, { method: 'POST', body: JSON.stringify({ email: el('account-email').value.trim(), password: el('account-password').value }) });
    accountView(result.user);
    await loadProfile();
  } catch (error) {
    if (currentUser && (error.status === 401 || error.status === 409)) accountError(error);
    else {
      message('account-feedback', error.message || 'Вход не удался.', true);
      if (currentUser) el('account-retry').hidden = false;
    }
  } finally { el('account-password').value = ''; setBusy(false); }
});
el('profile-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || !currentUser) return;
  const profile = Object.fromEntries(Object.entries(profileFields).map(([field, id]) => [field, el(id).value.trim()]));
  setBusy(true); message('profile-feedback', 'Сохраняем данные…');
  try {
    const result = await jsonApi('/api/accounts/profile', { method: 'PUT', headers: { 'X-Account-Id': currentUser.id }, body: JSON.stringify(profile) });
    fillProfile(result.profile);
    message('profile-feedback', 'Личные данные сохранены.');
  } catch (error) {
    if (error.status === 401 || error.status === 409) accountError(error);
    else message('profile-feedback', error.message || 'Не удалось сохранить данные. Попробуйте ещё раз.', true);
  } finally { setBusy(false); }
});
el('profile-form').addEventListener('input', () => message('profile-feedback', ''));
el('account-logout').addEventListener('click', async () => {
  if (busy) return;
  setBusy(true);
  try {
    await jsonApi('/api/accounts/logout', { method: 'POST' });
    accountView(null); setMode('login'); el('account-retry').hidden = true;
    message('account-feedback', 'Вы вышли из аккаунта.');
  } catch (error) { message('account-feedback', error.message || 'Не удалось выйти из аккаунта.', true); }
  finally { setBusy(false); }
});
window.addEventListener('pageshow', (event) => { if (event.persisted) window.location.reload(); });
start();
