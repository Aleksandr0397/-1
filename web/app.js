'use strict';

const byId = (id) => document.getElementById(id);
const state = { config: null, catalog: { catalog_version: null, products: [] }, products: new Map(), indexed: [], cart: new Map(), shown: 60, filtered: [], busy: false, sentSignature: null };
const storageKey = 'okunev-order-cart-v1';
const money = new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB', minimumFractionDigits: 0, maximumFractionDigits: 2 });
const wholeNumbers = new Intl.NumberFormat('ru-RU');
let toastTimer;
let searchTimer;

function normalize(value) { return String(value || '').toLocaleLowerCase('ru-RU').replace(/ё/g, 'е').normalize('NFKC'); }
function plural(count) {
  const last = count % 10, lastTwo = count % 100;
  return count + ' ' + (last === 1 && lastTwo !== 11 ? 'позиция' : last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14) ? 'позиции' : 'позиций');
}
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function notify(message) {
  clearTimeout(toastTimer);
  byId('toast').textContent = message;
  byId('toast').hidden = false;
  toastTimer = setTimeout(() => { byId('toast').hidden = true; }, 3200);
}
function feedback(id, message, error = false) {
  const target = byId(id);
  target.textContent = message;
  target.classList.toggle('error', error);
  target.hidden = !message;
}
async function errorFor(response) {
  let body;
  try { body = await response.json(); } catch (_) { /* An upstream server may return an HTML error. */ }
  const message = typeof body?.detail === 'string' ? body.detail : 'Не удалось выполнить действие. Проверьте данные и попробуйте ещё раз.';
  const error = new Error(message);
  error.status = response.status;
  return error;
}
async function jsonApi(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw await errorFor(response);
  return response.json();
}
function saveCart() {
  try { localStorage.setItem(storageKey, JSON.stringify({ catalog_version: state.catalog.catalog_version, items: Array.from(state.cart, ([product_id, quantity]) => ({ product_id, quantity })) })); } catch (_) { /* The basket still works when browser storage is unavailable. */ }
}
function restoreCart() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
    if (!saved) return;
    if (saved.catalog_version !== state.catalog.catalog_version) {
      localStorage.removeItem(storageKey);
      if (saved.items?.length) notify('Прайс обновился. Соберите заказ по новым ценам.');
      return;
    }
    for (const item of saved.items || []) {
      if (state.products.has(item.product_id) && validQuantity(item.quantity)) state.cart.set(item.product_id, Number(item.quantity));
    }
  } catch (_) { /* Ignore damaged saved baskets. */ }
}
function validQuantity(value) { const number = Number(value); return Number.isFinite(number) && number > 0 && number <= 999999 && Math.abs(number * 1000 - Math.round(number * 1000)) < 0.000001; }
function quantityInput(product, value, className) {
  const input = node('input', className);
  input.type = 'number'; input.min = '0.001'; input.max = '999999'; input.step = '0.001'; input.value = String(value);
  input.inputMode = 'decimal'; input.setAttribute('aria-label', 'Количество: ' + product.name);
  return input;
}
function setCatalog(catalog, restore = false) {
  const previous = state.catalog.catalog_version;
  state.catalog = catalog;
  state.products = new Map(catalog.products.map((product) => [product.id, product]));
  state.indexed = catalog.products.map((product) => ({ product, search: normalize(product.name + ' ' + (product.sku || '')) }));
  if (previous && previous !== catalog.catalog_version) {
    state.cart.clear(); state.sentSignature = null; saveCart();
    feedback('order-feedback', 'Прайс обновлён. Добавьте товары из нового каталога.');
  }
  if (restore) restoreCart();
  byId('catalog-count').textContent = plural(catalog.products.length);
  byId('filename').textContent = catalog.filename || 'Прайс пока не загружен';
  filterProducts(); renderCart();
}
function filterProducts() {
  const terms = normalize(byId('search').value).trim().split(/\s+/).filter(Boolean);
  state.filtered = terms.length ? state.indexed.filter((entry) => terms.every((term) => entry.search.includes(term))).map((entry) => entry.product) : state.catalog.products;
  state.shown = 60;
  renderProducts();
}
function renderProducts() {
  const fragment = document.createDocumentFragment();
  for (const product of state.filtered.slice(0, state.shown)) {
    const row = node('tr');
    const description = node('td');
    description.append(node('div', 'product-name', product.name));
    description.append(node('span', 'product-meta', [product.sku ? 'Артикул ' + product.sku : '', product.unit || 'шт'].filter(Boolean).join(' · ')));
    const price = node('td', 'product-price', money.format(Number(product.price)));
    const quantityCell = node('td');
    const input = quantityInput(product, 1, 'product-quantity');
    quantityCell.append(input);
    const action = node('td');
    const button = node('button', 'button product-add', 'В заказ');
    button.type = 'button'; button.setAttribute('aria-label', 'Добавить в заказ: ' + product.name);
    function add() {
      const quantity = Number(input.value);
      if (!validQuantity(quantity)) { input.setCustomValidity('Введите количество от 0,001 до 999 999, не больше трёх знаков после запятой.'); input.reportValidity(); return; }
      const next = Math.round(((state.cart.get(product.id) || 0) + quantity) * 1000) / 1000;
      if (!validQuantity(next)) { notify('В заказе слишком большое количество этого товара.'); return; }
      if (!state.cart.has(product.id) && state.cart.size >= 500) { notify('В один заказ можно добавить не больше 500 разных товаров.'); return; }
      input.setCustomValidity(''); state.cart.set(product.id, next); basketChanged(); renderCart();
      button.textContent = 'Добавлено'; setTimeout(() => { button.textContent = 'В заказ'; }, 1100);
    }
    input.addEventListener('input', () => input.setCustomValidity(''));
    input.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); add(); } });
    button.addEventListener('click', add);
    action.append(button); row.append(description, price, quantityCell, action); fragment.append(row);
  }
  byId('products').replaceChildren(fragment);
  const hasResults = state.filtered.length > 0;
  byId('table-wrap').hidden = !hasResults;
  byId('catalog-empty').hidden = hasResults;
  byId('load-more').hidden = state.filtered.length <= state.shown;
  byId('results-status').textContent = state.catalog.products.length ? 'Найдено: ' + state.filtered.length.toLocaleString('ru-RU') : '';
  if (!hasResults) {
    const emptyCatalog = !state.catalog.products.length;
    byId('empty-title').textContent = emptyCatalog ? 'Добавьте первый прайс-лист' : 'Товары не найдены';
    byId('empty-description').textContent = emptyCatalog ? 'Загрузите Excel-файл, чтобы покупатели могли выбрать товары и собрать заказ.' : 'Попробуйте несколько букв из названия или другой артикул.';
    byId('empty-upload').hidden = !emptyCatalog;
  }
}
function basketChanged() { state.sentSignature = null; feedback('order-feedback', ''); saveCart(); updateActions(); }
function lineTotalCents(product, quantity) {
  const parts = String(product.price).split('.');
  const price = BigInt(parts[0]) * 100n + BigInt((parts[1] || '').padEnd(2, '0').slice(0, 2));
  return (price * BigInt(Math.round(quantity * 1000)) + 500n) / 1000n;
}
function formatCents(cents) {
  const remainder = cents % 100n;
  return wholeNumbers.format(cents / 100n) + (remainder ? ',' + String(remainder).padStart(2, '0') : '') + ' ₽';
}
function renderCart() {
  const fragment = document.createDocumentFragment();
  let totalCents = 0n;
  for (const [id, quantity] of state.cart) {
    const product = state.products.get(id);
    if (!product) { state.cart.delete(id); continue; }
    const item = node('li', 'cart-item');
    const top = node('div', 'cart-top');
    const remove = node('button', 'icon-button', '×');
    remove.type = 'button'; remove.setAttribute('aria-label', 'Убрать из заказа: ' + product.name);
    remove.addEventListener('click', () => { state.cart.delete(id); basketChanged(); renderCart(); });
    top.append(node('span', 'cart-name', product.name), remove);
    const controls = node('div', 'cart-controls');
    const wrap = node('div', 'cart-quantity-wrap');
    const input = quantityInput(product, quantity, 'cart-quantity');
    input.addEventListener('change', () => {
      if (!validQuantity(input.value)) { input.value = String(state.cart.get(id)); notify('Укажите положительное количество, до трёх знаков после запятой.'); return; }
      state.cart.set(id, Number(input.value)); basketChanged(); renderCart();
    });
    wrap.append(input, node('span', 'cart-unit', product.unit || 'шт'));
    const amount = lineTotalCents(product, quantity); totalCents += amount;
    controls.append(wrap, node('span', 'cart-line-total', formatCents(amount)));
    item.append(top, controls); fragment.append(item);
  }
  byId('cart-items').replaceChildren(fragment);
  byId('cart-empty').hidden = state.cart.size > 0;
  byId('clear-cart').hidden = !state.cart.size;
  byId('order-count').textContent = state.cart.size;
  byId('total').textContent = formatCents(totalCents);
  byId('mobile-total').textContent = formatCents(totalCents);
  byId('mobile-count').textContent = plural(state.cart.size);
  byId('mobile-order-bar').hidden = !state.cart.size;
  document.body.classList.toggle('has-cart', state.cart.size > 0);
  updateActions();
}
function orderPayload() {
  return { catalog_version: state.catalog.catalog_version, customer: { name: byId('customer-name').value.trim(), contact: byId('customer-contact').value.trim(), comment: byId('customer-comment').value.trim() }, items: Array.from(state.cart, ([product_id, quantity]) => ({ product_id, quantity: String(quantity) })) };
}
function updateActions() {
  const sent = state.sentSignature && state.sentSignature === JSON.stringify(orderPayload());
  byId('download-order').disabled = state.busy || !state.cart.size;
  byId('send-order').disabled = state.busy || !state.cart.size || !state.config?.mail_ready || Boolean(sent);
  byId('send-order').textContent = state.busy ? 'Подождите…' : sent ? 'Заказ отправлен' : 'Отправить заказ';
}
async function checkout(send) {
  if (state.busy || !state.cart.size) return;
  const payload = orderPayload(); state.busy = true; updateActions(); feedback('order-feedback', '');
  try {
    const response = await fetch('/api/orders/' + (send ? 'send' : 'export'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (!response.ok) throw await errorFor(response);
    if (send) {
      await response.json(); state.sentSignature = JSON.stringify(payload);
      feedback('order-feedback', 'Заказ отправлен. Excel-файл с выбранными товарами приложен к письму.');
    } else {
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      const disposition = response.headers.get('Content-Disposition') || '';
      const filename = disposition.match(/filename="?([^";]+)"?/i);
      link.href = url; link.download = filename ? filename[1] : 'Заказ.xlsx';
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 10000);
      notify('Excel-заказ подготовлен для скачивания.');
    }
  } catch (error) {
    feedback('order-feedback', error.message || 'Нет связи с сайтом. Попробуйте ещё раз.', true);
    if (error.status === 409) await loadCatalog(false);
  } finally { state.busy = false; updateActions(); }
}
function openUpload() {
  feedback('upload-feedback', '');
  if (state.config && !state.config.upload_enabled) feedback('upload-feedback', 'Загрузка прайса пока не подключена. Обратитесь к владельцу сайта.', true);
  byId('upload-submit').disabled = !state.config?.upload_enabled;
  byId('upload-dialog').showModal();
}
function closeUpload() { byId('upload-dialog').close(); byId('admin-password').value = ''; }
async function upload(event) {
  event.preventDefault();
  const file = byId('price-file').files[0];
  if (!file || !state.config?.upload_enabled) return;
  if (!/\.(xlsx|xls)$/i.test(file.name)) { feedback('upload-feedback', 'Выберите Excel-файл в формате XLSX или XLS.', true); return; }
  if (file.size > (state.config.max_upload_mb || 10) * 1024 * 1024) { feedback('upload-feedback', 'Файл слишком большой. Максимальный размер — 10 МБ.', true); return; }
  if (state.cart.size && !window.confirm('Новый прайс заменит текущий каталог и очистит ваш заказ. Продолжить?')) return;
  const data = new FormData(); data.append('file', file);
  const button = byId('upload-submit'); button.disabled = true; button.textContent = 'Загружаем прайс…'; feedback('upload-feedback', '');
  try {
    const catalog = await jsonApi('/api/catalog/import', { method: 'POST', headers: { 'X-Admin-Token': byId('admin-password').value }, body: data });
    byId('search').value = ''; setCatalog(catalog); closeUpload();
    notify('Прайс загружен: ' + plural(catalog.products.length) + '.');
    byId('search').focus();
  } catch (error) { feedback('upload-feedback', error.message || 'Загрузка не удалась. Проверьте соединение и повторите.', true); }
  finally { button.disabled = false; button.textContent = 'Загрузить прайс'; }
}
async function loadCatalog(restore) {
  byId('load-error').hidden = true;
  try { setCatalog(await jsonApi('/api/catalog'), restore); }
  catch (error) {
    byId('load-error').querySelector('span').textContent = 'Не удалось загрузить каталог. Проверьте соединение и повторите.';
    byId('load-error').hidden = false;
    if (!state.catalog.products.length) { byId('empty-title').textContent = 'Каталог временно недоступен'; byId('empty-description').textContent = 'Попробуйте загрузить его ещё раз.'; byId('empty-upload').hidden = true; }
  }
}
async function start() {
  const configTask = jsonApi('/api/config').then((config) => {
    state.config = config;
    byId('mail-note').textContent = config.mail_ready ? 'Заказ отправится поставщику по e-mail с Excel-вложением.' : 'Отправка почты пока не подключена. Готовый заказ можно скачать в Excel.';
    byId('admin-password').required = config.upload_requires_password;
    updateActions();
  }).catch(() => { byId('mail-note').textContent = 'Отправка временно недоступна. Готовый заказ можно скачать в Excel.'; });
  await Promise.allSettled([configTask, loadCatalog(true)]);
}

byId('search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(filterProducts, 60); });
byId('load-more').addEventListener('click', () => { state.shown += 60; renderProducts(); });
byId('clear-cart').addEventListener('click', () => { state.cart.clear(); basketChanged(); renderCart(); });
byId('customer-form').addEventListener('input', () => { state.sentSignature = null; updateActions(); });
byId('customer-form').addEventListener('submit', (event) => event.preventDefault());
byId('send-order').addEventListener('click', () => checkout(true));
byId('download-order').addEventListener('click', () => checkout(false));
byId('open-upload').addEventListener('click', openUpload);
byId('empty-upload').addEventListener('click', openUpload);
byId('close-upload').addEventListener('click', closeUpload);
byId('upload-dialog').addEventListener('cancel', () => { byId('admin-password').value = ''; });
byId('upload-form').addEventListener('submit', upload);
byId('price-file').addEventListener('change', () => { byId('selected-file').textContent = byId('price-file').files[0]?.name || 'Файл не выбран'; });
byId('retry-load').addEventListener('click', () => loadCatalog(false));
byId('jump-to-order').addEventListener('click', () => { byId('order-panel').scrollIntoView({ behavior: 'auto', block: 'start' }); byId('order-heading').focus({ preventScroll: true }); });
start();
