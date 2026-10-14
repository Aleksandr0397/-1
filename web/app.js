'use strict';

const byId = (id) => document.getElementById(id);
const state = { config: null, user: null, catalog: { catalog_version: null, products: [] }, products: new Map(), cartProducts: new Map(), indexed: [], cart: new Map(), shown: 60, filtered: [], sort: null, busy: false, sentSignature: null };
const legacyStorageKey = 'okunev-order-cart-v1';
const guestStorageKey = 'okunev-order-cart-v2:guest';
let storageKey = null;
let accountCart = null;
let initialising = true;
let applyingAccountCart = false;
let catalogVerified = false;
let localCartMetadata = null;
let syncMessageTimer;
const catalogCacheKey = 'okunev-public-catalog-v1';
const fullCart = document.body.dataset.view === 'cart';
const money = new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB', minimumFractionDigits: 0, maximumFractionDigits: 2 });
const wholeNumbers = new Intl.NumberFormat('ru-RU');
const names = new Intl.Collator('ru-RU', { numeric: true, sensitivity: 'base', ignorePunctuation: true });
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
  const response = await fetch(url, { cache: 'no-store', ...options });
  if (!response.ok) throw await errorFor(response);
  return response.json();
}
function cartDocument() {
  return { catalog_version: state.catalog.catalog_version, customer: customerFields(), items: Array.from(state.cart, ([product_id, quantity]) => {
    const source = state.cartProducts.get(product_id) || state.products.get(product_id) || { id: product_id, name: 'Товар из предыдущего прайса', sku: '', unit: '', price: '0.00', unresolved: true };
    const product = {};
    for (const field of ['id', 'name', 'sku', 'unit', 'price', 'available', 'previous_price', 'restocked', 'unresolved', 'source_version']) if (source[field] !== undefined && source[field] !== null) product[field] = source[field];
    return { product_id, quantity, product };
  }) };
}
function readLocalCart() {
  try { return JSON.parse(localStorage.getItem(storageKey) || 'null'); } catch (_) { return null; }
}
function writeLocalCart() {
  if (!storageKey || initialising) return;
  if (accountCart?.ready) localCartMetadata = { revision: accountCart.revision, pending: accountCart.pending };
  try { localStorage.setItem(storageKey, JSON.stringify({ ...cartDocument(), ...(localCartMetadata ? { _shared: localCartMetadata } : {}) })); } catch (_) { /* A server cart also works without browser storage. */ }
}
function saveCart() {
  if (accountCart?.ready && !initialising && !applyingAccountCart && catalogVerified && (accountCart.pending || !accountCart.matches(cartDocument()))) accountCart.schedule();
  writeLocalCart();
}
function cartEditable() {
  return Boolean(storageKey) && !initialising && !applyingAccountCart && catalogVerified && (!state.user || Boolean(accountCart?.ready));
}
function cartContext() {
  const key = storageKey, owner = state.user?.id, controller = accountCart;
  return () => Boolean(key) && storageKey === key && state.user?.id === owner && accountCart === controller;
}
function cartSyncStatus(kind, text) {
  clearTimeout(syncMessageTimer);
  feedback('cart-sync-feedback', text || '', kind === 'error' || kind === 'conflict');
  byId('cart-sync-retry').hidden = kind !== 'error';
  updateActions();
  if (kind === 'saved') writeLocalCart();
  if (kind === 'saved') syncMessageTimer = setTimeout(() => feedback('cart-sync-feedback', ''), 2500);
}
async function applyAccountCart(document) {
  const current = cartContext();
  applyingAccountCart = true;
  updateActions();
  try {
    state.cart.clear(); state.cartProducts.clear();
    for (const field of ['name', 'contact', 'comment']) byId('customer-' + field).value = '';
    await restoreCart(document || { items: [], customer: {} });
    if (!current()) return;
    if (!initialising && document?.catalog_version && document.catalog_version !== state.catalog.catalog_version) await loadCatalog(false);
    if (!current()) return;
    if (state.catalog.catalog_version) reconcileCart();
    renderCart();
  } finally { applyingAccountCart = false; updateActions(); }
}
function accountCartAuthError(ownerId) {
  if (state.user?.id !== ownerId) return;
  accountCart?.dispose(); accountCart = null; storageKey = null;
  state.user = null; state.cart.clear(); state.cartProducts.clear();
  for (const field of ['name', 'contact', 'comment']) byId('customer-' + field).value = '';
  byId('account-link').textContent = 'Войти'; renderCart();
  feedback('order-feedback', '');
  feedback('cart-sync-feedback', 'Аккаунт изменился или вход завершён. Обновите страницу и войдите в свой аккаунт.', true);
  byId('cart-sync-retry').hidden = false;
}
async function initialiseAccountCart(local) {
  const ownerId = state.user.id;
  if (local?.items?.length && !local._shared) {
    try { localStorage.setItem(storageKey + ':before-sync', JSON.stringify(local)); } catch (_) { /* Server state remains authoritative. */ }
  }
  accountCart = new window.AccountCartSync({
    ownerId, getDocument: cartDocument, applyDocument: applyAccountCart,
    onStatus: cartSyncStatus, onAcknowledged: writeLocalCart,
    onConflict: (document) => { try { localStorage.setItem(storageKey + ':conflict', JSON.stringify(document)); } catch (_) { /* Keep server state authoritative. */ } },
    onAuthError: () => accountCartAuthError(ownerId),
  });
  const recovery = local?._shared?.pending && Number.isInteger(local._shared.revision) ? { revision: local._shared.revision, cart: cartDocument() } : null;
  await accountCart.load({ migration: local && !local._shared ? cartDocument() : null, pending: recovery });
}
function cachedCatalog() {
  try {
    const cached = JSON.parse(sessionStorage.getItem(catalogCacheKey) || 'null');
    if (cached && typeof cached.catalog_version === 'string' && Array.isArray(cached.products) && cached.products.length <= 20000 && cached.products.every(product => validSnapshot(product, product.id))) return cached;
  } catch (_) { /* The public cache is optional. */ }
  return null;
}
function cacheCatalog(catalog) {
  try { sessionStorage.setItem(catalogCacheKey, JSON.stringify(catalog)); } catch (_) { /* Continue without a catalog cache. */ }
}
function selectCartStorage() {
  storageKey = state.user ? 'okunev-order-cart-v2:user:' + state.user.id : guestStorageKey;
  try {
    const legacy = localStorage.getItem(legacyStorageKey);
    if (legacy) {
      // The old basket has no owner. Only an anonymous visit can safely migrate it.
      if (!state.user && !localStorage.getItem(guestStorageKey)) localStorage.setItem(guestStorageKey, legacy);
      localStorage.removeItem(legacyStorageKey);
    }
    if (state.user) {
      const guest = localStorage.getItem(guestStorageKey);
      if (guest) {
        const personal = localStorage.getItem(storageKey);
        if (!personal) localStorage.setItem(storageKey, guest);
        localStorage.removeItem(guestStorageKey);
        if (personal && JSON.parse(guest)?.items?.length) notify('Восстановлен ваш личный заказ. Товары гостевой корзины не добавлены.');
      }
    }
  } catch (_) { /* Do not transfer or expose another account's basket if storage fails. */ }
}
function validSnapshot(product, id) {
  return product && product.id === id && typeof product.name === 'string' && product.name.length <= 500 && typeof product.sku === 'string' && product.sku.length <= 100 && typeof product.unit === 'string' && product.unit.length <= 50 && /^\d{1,9}\.\d{2}$/.test(product.price);
}
async function restoreCart(document) {
  if (!storageKey) return;
  const current = cartContext();
  try {
    const saved = document === undefined ? readLocalCart() : document;
    if (!saved) return;
    const recovered = new Map();
    const items = Array.isArray(saved.items) ? saved.items.slice(0, 500) : [];
    const versions = new Set(items.filter(item => !state.products.has(item.product_id) && (!validSnapshot(item.product, item.product_id) || item.product.unresolved)).map(item => item.product?.source_version || saved.catalog_version));
    for (const version of versions) {
      if (typeof version !== 'string' || !version || version.length > 100) continue;
      try {
        const archive = await jsonApi('/api/catalog/versions/' + encodeURIComponent(version));
        if (!current()) return;
        for (const product of archive.products) recovered.set(product.id, { ...product, source_version: version });
      } catch (_) { /* Keep unresolved rows, including their original ID and quantity. */ }
    }
    if (!current()) return;
    for (const field of ['name', 'contact', 'comment']) {
      const input = byId('customer-' + field);
      if (typeof saved.customer?.[field] === 'string') input.value = saved.customer[field].slice(0, input.maxLength);
    }
    let removedFractional = false;
    for (const item of items) {
      if (typeof item.product_id !== 'string' || !item.product_id || item.product_id.length > 100) continue;
      if (validQuantity(item.quantity)) {
        const snapshot = recovered.get(item.product_id) || (validSnapshot(item.product, item.product_id) ? item.product : null) || state.products.get(item.product_id) || { id: item.product_id, name: 'Товар из предыдущего прайса · ' + item.product_id, sku: '', unit: '', price: '0.00', unresolved: true, source_version: saved.catalog_version };
        state.cart.set(item.product_id, Number(item.quantity));
        state.cartProducts.set(item.product_id, snapshot);
      } else if (Number.isFinite(Number(item.quantity)) && !Number.isInteger(Number(item.quantity))) removedFractional = true;
    }
    if (removedFractional) notify('Позиции с дробным количеством удалены из сохранённого заказа. Добавьте их с целым количеством.');
  } catch (_) { /* Ignore damaged saved baskets. */ }
}
function productIdentity(value) { return normalize(value).trim().replace(/\s+/g, ' '); }
function reconcileCart() {
  const skuIndex = new Map(), nameIndex = new Map();
  function add(index, key, product) { if (!index.has(key)) index.set(key, []); index.get(key).push(product); }
  for (const product of state.catalog.products) {
    const unit = productIdentity(product.unit);
    if (product.sku) add(skuIndex, productIdentity(product.sku) + '\u0000' + unit, product);
    add(nameIndex, productIdentity(product.name) + '\u0000' + unit, product);
  }
  const cart = new Map(), snapshots = new Map(), candidates = [], totals = new Map();
  for (const [id, quantity] of state.cart) {
    const old = state.cartProducts.get(id) || state.products.get(id);
    if (!old) continue;
    let current = state.products.get(id);
    if (!current && !old.unresolved) {
      const unit = productIdentity(old.unit);
      const bySku = old.sku ? skuIndex.get(productIdentity(old.sku) + '\u0000' + unit) : null;
      if (bySku?.length === 1) current = bySku[0];
      if (!current) {
        const byName = nameIndex.get(productIdentity(old.name) + '\u0000' + unit)?.filter(product => !old.sku || !product.sku || productIdentity(old.sku) === productIdentity(product.sku));
        if (byName?.length === 1) current = byName[0];
      }
    }
    candidates.push({ id, quantity, old, current });
    if (current) totals.set(current.id, (totals.get(current.id) || 0) + quantity);
  }
  for (const { id, quantity, old, current } of candidates) {
    if (current && (totals.get(current.id) <= 999999 || id === current.id)) {
      cart.set(current.id, (cart.get(current.id) || 0) + quantity);
      snapshots.set(current.id, { ...current, available: true, previous_price: old.price !== current.price ? old.price : old.previous_price, restocked: old.available === false || Boolean(old.restocked) });
    } else {
      cart.set(id, quantity);
      snapshots.set(id, { ...old, available: false, restocked: false });
    }
  }
  state.cart = cart; state.cartProducts = snapshots;
}
function validQuantity(value) { const number = Number(value); return Number.isInteger(number) && number >= 1 && number <= 999999; }
function quantityInput(product, value, className) {
  const input = node('input', className);
  input.type = 'number'; input.min = '1'; input.max = '999999'; input.step = '1'; input.value = String(value);
  input.disabled = state.busy || !cartEditable();
  input.dataset.productId = product.id;
  input.inputMode = 'numeric'; input.setAttribute('aria-label', 'Количество: ' + product.name);
  input.addEventListener('keydown', (event) => { if (['.', ',', 'e', 'E', '-', '+'].includes(event.key)) event.preventDefault(); });
  input.addEventListener('change', () => { if (validQuantity(input.value)) input.value = String(Number(input.value)); });
  return input;
}
function quantityStepper(input) {
  const wrapper = node('div', 'quantity-stepper');
  const arrows = node('div', 'quantity-arrows');
  const up = node('button', 'quantity-increase', '▲');
  const down = node('button', 'quantity-decrease', '▼');
  for (const [button, direction, label] of [[up, 1, 'Увеличить количество'], [down, -1, 'Уменьшить количество']]) {
    button.type = 'button';
    button.setAttribute('aria-label', label + ': ' + input.getAttribute('aria-label').replace(/^Количество: /, ''));
    button.addEventListener('mousedown', (event) => event.preventDefault());
    button.addEventListener('click', () => {
      if (state.busy || !cartEditable()) return;
      if (!validQuantity(input.value)) {
        input.setCustomValidity('Введите целое количество от 1 до 999 999.'); input.reportValidity(); return;
      }
      input.setCustomValidity('');
      input.value = String(Math.min(999999, Math.max(1, Number(input.value) + direction)));
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      const current = input.isConnected ? input : byId('cart-items').querySelector('input[data-product-id="' + CSS.escape(input.dataset.productId) + '"]');
      current?.closest('.quantity-stepper')?.querySelector(direction === 1 ? '.quantity-increase' : '.quantity-decrease')?.focus({ preventScroll: true });
    });
  }
  function updateLimits() { up.disabled = state.busy || !cartEditable() || Number(input.value) >= 999999; down.disabled = state.busy || !cartEditable() || Number(input.value) <= 1; }
  input.addEventListener('input', updateLimits); input.addEventListener('change', updateLimits);
  updateLimits(); arrows.append(up, down); wrapper.append(input, arrows);
  return wrapper;
}
function markCatalogSelection() {
  for (const row of byId('products').children) {
    const quantity = state.cart.get(row.dataset.productId);
    row.classList.toggle('in-cart', Boolean(quantity));
    const badge = row.querySelector('.product-cart-status');
    badge.hidden = !quantity;
    badge.textContent = quantity ? 'В корзине: ' + wholeNumbers.format(quantity) : '';
  }
}
async function setCatalog(catalog, restore = false) {
  const previous = state.catalog.catalog_version;
  for (const [id] of state.cart) if (!state.cartProducts.has(id) && state.products.has(id)) state.cartProducts.set(id, { ...state.products.get(id), source_version: previous });
  state.catalog = catalog;
  state.products = new Map(catalog.products.map((product) => [product.id, product]));
  state.indexed = fullCart ? [] : catalog.products.map((product) => ({ product, search: normalize(product.name + ' ' + (product.sku || '')) }));
  if (previous && previous !== catalog.catalog_version) {
    state.sentSignature = null;
    feedback('order-feedback', 'Прайс обновлён. Товары и количества сохранены; цены и наличие проверены по новому прайсу.');
  }
  if (restore) await restoreCart();
  reconcileCart();
  saveCart();
  byId('catalog-count').textContent = plural(catalog.products.length);
  byId('filename').textContent = catalog.filename || 'Прайс пока не загружен';
  if (!fullCart) filterProducts();
  renderCart();
}
function filterProducts() {
  const terms = normalize(byId('search').value).trim().split(/\s+/).filter(Boolean);
  state.filtered = terms.length ? state.indexed.filter((entry) => terms.every((term) => entry.search.includes(term))).map((entry) => entry.product) : [...state.catalog.products];
  if (state.sort) {
    const { field, direction } = state.sort;
    state.filtered.sort((a, b) => direction * (field === 'price' ? Number(a.price) - Number(b.price) : names.compare(a.name, b.name)));
  }
  state.shown = 60;
  renderProducts();
}
function sortProducts(field) {
  state.sort = { field, direction: state.sort?.field === field ? -state.sort.direction : 1 };
  for (const kind of ['name', 'price']) {
    const button = byId('sort-' + kind);
    if (!button) continue;
    const active = kind === field;
    button.closest('th').setAttribute('aria-sort', active ? (state.sort.direction === 1 ? 'ascending' : 'descending') : 'none');
    byId('sort-' + kind + '-direction').textContent = active ? (state.sort.direction === 1 ? '↑' : '↓') : '↕';
  }
  filterProducts();
}
function renderProducts() {
  const fragment = document.createDocumentFragment();
  for (const product of state.filtered.slice(0, state.shown)) {
    const row = node('tr');
    row.dataset.productId = product.id;
    const description = node('td');
    description.append(node('div', 'product-name', product.name));
    description.append(node('span', 'product-meta', [product.sku ? 'Артикул ' + product.sku : '', product.unit || 'шт'].filter(Boolean).join(' · ')));
    description.append(node('span', 'product-cart-status'));
    const price = node('td', 'product-price', money.format(Number(product.price)));
    const quantityCell = node('td');
    const input = quantityInput(product, 1, 'product-quantity');
    quantityCell.append(quantityStepper(input));
    const action = node('td');
    const button = node('button', 'button product-add', 'В заказ');
    button.disabled = state.busy || !cartEditable();
    button.type = 'button'; button.setAttribute('aria-label', 'Добавить в заказ: ' + product.name);
    function add() {
      if (state.busy || !cartEditable()) return;
      if (!storageKey) { notify('Обновите страницу, чтобы восстановить свой аккаунт и корзину.'); return; }
      const quantity = Number(input.value);
      if (!validQuantity(quantity)) { input.setCustomValidity('Введите целое количество от 1 до 999 999.'); input.reportValidity(); return; }
      const next = (state.cart.get(product.id) || 0) + quantity;
      if (!validQuantity(next)) { notify('В заказе слишком большое количество этого товара.'); return; }
      if (!state.cart.has(product.id) && state.cart.size >= 500) { notify('В один заказ можно добавить не больше 500 разных товаров.'); return; }
      input.setCustomValidity(''); state.cart.set(product.id, next);
      if (!state.cartProducts.has(product.id)) state.cartProducts.set(product.id, { ...product, available: true });
      basketChanged(); renderCart();
      button.textContent = 'Добавлено'; setTimeout(() => { button.textContent = 'В заказ'; }, 1100);
    }
    input.addEventListener('input', () => input.setCustomValidity(''));
    input.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); add(); } });
    button.addEventListener('click', add);
    action.append(button); row.append(description, price, quantityCell, action); fragment.append(row);
  }
  byId('products').replaceChildren(fragment);
  markCatalogSelection();
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
  return price * BigInt(quantity);
}
function formatCents(cents) {
  const remainder = cents % 100n;
  return wholeNumbers.format(cents / 100n) + (remainder ? ',' + String(remainder).padStart(2, '0') : '') + ' ₽';
}
function setCartOpen(open, scroll = false) {
  if (fullCart) return;
  byId('order-panel').hidden = !open;
  document.querySelector('.workspace').classList.toggle('cart-open', open);
  byId('toggle-cart').setAttribute('aria-expanded', String(open));
  byId('jump-to-order').setAttribute('aria-expanded', String(open));
  byId('jump-to-order').textContent = open ? 'Скрыть заказ' : 'Открыть заказ';
  if (open && scroll) {
    byId('order-panel').scrollIntoView({ behavior: 'auto', block: 'start' });
    byId('order-heading').focus({ preventScroll: true });
  } else if (!open) {
    byId('toggle-cart').focus({ preventScroll: true });
  }
}
function renderCart() {
  const fragment = document.createDocumentFragment();
  let totalCents = 0n;
  for (const [id, quantity] of state.cart) {
    const product = state.cartProducts.get(id) || state.products.get(id);
    if (!product) continue;
    const available = state.products.has(id) && product.available !== false;
    const item = node('li', 'cart-item');
    item.classList.toggle('cart-unavailable', !available);
    item.classList.toggle('cart-restocked', available && Boolean(product.restocked));
    const top = node('div', 'cart-top');
    const remove = node('button', 'icon-button', '×');
    remove.type = 'button'; remove.setAttribute('aria-label', 'Убрать из заказа: ' + product.name);
    remove.disabled = state.busy || !cartEditable();
    remove.addEventListener('click', () => { if (state.busy || !cartEditable()) return; state.cart.delete(id); state.cartProducts.delete(id); basketChanged(); renderCart(); });
    top.append(node('span', 'cart-name', product.name), remove);
    const controls = node('div', 'cart-controls');
    const wrap = node('div', 'cart-quantity-wrap');
    const input = quantityInput(product, quantity, 'cart-quantity');
    input.addEventListener('change', () => {
      if (state.busy || !cartEditable()) { input.value = String(state.cart.get(id)); return; }
      if (!validQuantity(input.value)) { input.value = String(state.cart.get(id)); notify('Укажите целое количество от 1 до 999 999.'); return; }
      state.cart.set(id, Number(input.value)); basketChanged(); renderCart();
    });
    wrap.append(quantityStepper(input), node('span', 'cart-unit', product.unit || 'шт'));
    const amount = available ? lineTotalCents(product, quantity) : 0n; totalCents += amount;
    controls.append(wrap, node('span', 'cart-line-total', available ? formatCents(amount) : 'Не включён в заказ'));
    item.append(top, controls);
    if (!available) item.append(node('p', 'cart-availability', 'Нет в новом прайсе · остаётся в корзине'));
    else if (product.restocked) item.append(node('p', 'cart-availability', 'Снова в наличии'));
    if (available && product.previous_price && product.previous_price !== product.price) item.append(node('p', 'cart-price-change', 'Цена изменилась: ' + money.format(Number(product.previous_price)) + ' → ' + money.format(Number(product.price))));
    fragment.append(item);
  }
  byId('cart-items').replaceChildren(fragment);
  byId('cart-empty').hidden = state.cart.size > 0;
  byId('clear-cart').hidden = !state.cart.size;
  byId('order-count').textContent = state.cart.size;
  byId('header-cart-count').textContent = state.cart.size;
  byId('total').textContent = formatCents(totalCents);
  const unavailable = Array.from(state.cart.keys()).filter(id => !state.products.has(id) || state.cartProducts.get(id)?.available === false).length;
  byId('availability-note').hidden = !unavailable;
  byId('availability-note').textContent = 'Нет в наличии: ' + unavailable + '. Эти позиции остаются в корзине; в заказ войдут только доступные товары.';
  byId('mobile-total').textContent = formatCents(totalCents);
  byId('mobile-count').textContent = plural(state.cart.size);
  byId('mobile-order-bar').hidden = !state.cart.size;
  document.body.classList.toggle('has-cart', state.cart.size > 0);
  markCatalogSelection();
  updateActions();
}
function customerFields() {
  return { name: byId('customer-name').value.trim(), contact: byId('customer-contact').value.trim(), comment: byId('customer-comment').value.trim() };
}
function orderPayload() {
  return { catalog_version: state.catalog.catalog_version, customer: customerFields(), items: Array.from(state.cart).filter(([id]) => state.products.has(id) && state.cartProducts.get(id)?.available !== false).map(([product_id, quantity]) => ({ product_id, quantity: String(quantity) })), ...(accountCart?.ready ? { cart_revision: accountCart.revision } : {}) };
}
function updateActions() {
  const sent = state.sentSignature && state.sentSignature === JSON.stringify(orderPayload());
  const needsLogin = state.config?.history_ready && !state.user;
  const available = orderPayload().items.length;
  const locked = state.busy || !cartEditable();
  const saving = accountCart?.pending || accountCart?.saving;
  byId('download-order').disabled = locked || saving || !available || needsLogin;
  byId('send-order').disabled = locked || saving || !available || needsLogin || !state.config?.mail_ready || Boolean(sent);
  byId('send-order').textContent = state.busy ? 'Подождите…' : sent ? 'Заказ отправлен' : 'Отправить заказ';
  byId('clear-cart').disabled = locked;
  for (const id of ['open-upload', 'empty-upload']) byId(id).disabled = state.busy;
  for (const input of document.querySelectorAll('.quantity-stepper input')) {
    input.disabled = locked;
    const wrapper = input.closest('.quantity-stepper');
    wrapper.querySelector('.quantity-increase').disabled = locked || Number(input.value) >= 999999;
    wrapper.querySelector('.quantity-decrease').disabled = locked || Number(input.value) <= 1;
  }
  for (const button of document.querySelectorAll('.product-add')) button.disabled = locked;
  for (const button of document.querySelectorAll('.cart-top .icon-button')) button.disabled = locked;
  for (const input of byId('customer-form').querySelectorAll('input, textarea')) input.disabled = locked;
}
function removeCompletedItems(payload, submittedProducts, submittedStorageKey) {
  if (storageKey !== submittedStorageKey) return;
  for (const item of payload.items) {
    let id = item.product_id;
    if (!state.cart.has(id)) {
      const original = submittedProducts.get(id);
      if (!original) continue;
      const candidates = Array.from(state.cart.keys()).filter(candidateId => {
        const product = state.cartProducts.get(candidateId) || state.products.get(candidateId);
        if (!product || productIdentity(product.unit) !== productIdentity(original.unit)) return false;
        if (original.sku && product.sku) return productIdentity(original.sku) === productIdentity(product.sku);
        return productIdentity(product.name) === productIdentity(original.name);
      });
      if (candidates.length !== 1) continue;
      id = candidates[0];
    }
    const remaining = (state.cart.get(id) || 0) - Number(item.quantity);
    if (remaining > 0) state.cart.set(id, remaining);
    else { state.cart.delete(id); state.cartProducts.delete(id); }
  }
  saveCart(); renderCart();
}
function completedOrderFeedback(send, orderId, saved, cleared = true) {
  if (!saved) {
    feedback('order-feedback', (send ? 'Заказ отправлен.' : 'Excel подготовлен для скачивания.') + ' Сохранение в «Моих заказах» не подтверждено. Корзина сохранена.', true);
    return;
  }
  feedback('order-feedback', (send ? 'Заказ отправлен и сохранён в «Моих заказах».' : 'Excel готов. Заказ сохранён в «Моих заказах».') + (cleared ? ' Заказанные товары убраны из корзины; отсутствующие позиции остаются.' : ' Корзина изменилась на другом устройстве и оставлена без очистки.'));
  const link = node('a', 'button button-outline', 'Открыть заказ в «Моих заказах»');
  link.href = '/orders/?order=' + encodeURIComponent(orderId);
  byId('order-feedback').append(document.createElement('br'), link);
}
async function checkout(send) {
  if (state.busy || !cartEditable() || !orderPayload().items.length) return;
  state.busy = true; updateActions(); feedback('order-feedback', '');
  try {
    if (accountCart) {
      if (!accountCart.matches(cartDocument())) accountCart.schedule();
      await accountCart.flush();
    }
    const payload = orderPayload();
    const current = cartContext();
    const submittedStorageKey = storageKey;
    const submittedProducts = new Map(payload.items.map(item => [item.product_id, { ...(state.cartProducts.get(item.product_id) || state.products.get(item.product_id)) }]));
    const response = await fetch('/api/orders/' + (send ? 'send' : 'export'), { method: 'POST', headers: { 'Content-Type': 'application/json', ...(state.user ? { 'X-Account-Id': state.user.id } : {}) }, body: JSON.stringify(payload) });
    if (!response.ok) throw await errorFor(response);
    if (!current()) return;
    if (send) {
      const result = await response.json();
      if (!current()) return;
      state.sentSignature = JSON.stringify(payload);
      const saved = result.order_saved === true && typeof result.order_id === 'string' && Boolean(result.order_id);
      if (saved && !accountCart) removeCompletedItems(payload, submittedProducts, submittedStorageKey);
      if (saved && accountCart) {
        try { await accountCart.refresh(); }
        catch (_) { cartSyncStatus('error', 'Заказ сохранён. Не удалось обновить общую корзину — повторите загрузку.'); }
      }
      if (!current()) return;
      completedOrderFeedback(true, result.order_id, saved, !accountCart || result.cart_completed === true);
    } else {
      const blob = await response.blob();
      if (!current()) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      const disposition = response.headers.get('Content-Disposition') || '';
      const filename = disposition.match(/filename="?([^";]+)"?/i);
      link.href = url; link.download = filename ? filename[1] : 'Заказ.xlsx';
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 10000);
      const orderId = response.headers.get('X-Order-Id');
      const saved = response.headers.get('X-Order-Saved') === 'true' && Boolean(orderId);
      if (saved && !accountCart) removeCompletedItems(payload, submittedProducts, submittedStorageKey);
      if (saved && accountCart) {
        try { await accountCart.refresh(); }
        catch (_) { cartSyncStatus('error', 'Заказ сохранён. Не удалось обновить общую корзину — повторите загрузку.'); }
      }
      if (!current()) return;
      completedOrderFeedback(false, orderId, saved);
    }
  } catch (error) {
    feedback('order-feedback', error.message || 'Нет связи с сайтом. Попробуйте ещё раз.', true);
    if (error.status === 409) {
      try {
        if (state.config?.history_ready && (await jsonApi('/api/accounts/me')).user?.id !== state.user?.id) window.location.reload();
        else { if (accountCart) await accountCart.refresh(); await loadCatalog(false); }
      } catch (_) { feedback('order-feedback', 'Обновите страницу, чтобы проверить текущий аккаунт и корзину.', true); }
    }
  } finally { state.busy = false; updateActions(); }
}
function openUpload() {
  if (state.busy) return;
  feedback('upload-feedback', '');
  if (state.config && !state.config.upload_enabled) feedback('upload-feedback', 'Загрузка прайса пока не подключена. Обратитесь к владельцу сайта.', true);
  byId('upload-submit').disabled = !state.config?.upload_enabled;
  byId('upload-dialog').showModal();
}
function closeUpload() { byId('upload-dialog').close(); byId('admin-password').value = ''; }
async function upload(event) {
  event.preventDefault();
  if (state.busy) return;
  const file = byId('price-file').files[0];
  if (!file || !state.config?.upload_enabled) return;
  if (!/\.(xlsx|xls)$/i.test(file.name)) { feedback('upload-feedback', 'Выберите Excel-файл в формате XLSX или XLS.', true); return; }
  if (file.size > (state.config.max_upload_mb || 10) * 1024 * 1024) { feedback('upload-feedback', 'Файл слишком большой. Максимальный размер — 10 МБ.', true); return; }
  if (state.cart.size && !window.confirm('Новый прайс заменит текущий каталог. Товары и количества останутся в корзине; цены и наличие обновятся. Продолжить?')) return;
  const data = new FormData(); data.append('file', file);
  const button = byId('upload-submit'); button.disabled = true; button.textContent = 'Загружаем прайс…'; feedback('upload-feedback', '');
  try {
    const catalog = await jsonApi('/api/catalog/import', { method: 'POST', headers: { 'X-Admin-Token': byId('admin-password').value }, body: data });
    byId('search').value = ''; await setCatalog(catalog); closeUpload();
    notify('Прайс загружен: ' + plural(catalog.products.length) + '.');
    byId('search').focus();
  } catch (error) { feedback('upload-feedback', error.message || 'Загрузка не удалась. Проверьте соединение и повторите.', true); }
  finally { button.disabled = false; button.textContent = 'Загрузить прайс'; }
}
async function loadCatalog(restore) {
  byId('load-error').hidden = true;
  try {
    const catalog = await jsonApi('/api/catalog');
    catalogVerified = true; cacheCatalog(catalog); await setCatalog(catalog, restore);
  }
  catch (error) {
    byId('load-error').querySelector('span').textContent = 'Не удалось загрузить каталог. Проверьте соединение и повторите.';
    byId('load-error').hidden = false;
    if (!state.catalog.products.length) { byId('empty-title').textContent = 'Каталог временно недоступен'; byId('empty-description').textContent = 'Попробуйте загрузить его ещё раз.'; byId('empty-upload').hidden = true; }
  }
}
async function start() {
  initialising = true;
  accountCart?.dispose(); accountCart = null;
  const cached = cachedCatalog();
  if (cached) await setCatalog(cached);
  const catalogTask = loadCatalog(false);
  try {
    const [config, identity] = await Promise.all([jsonApi('/api/config'), jsonApi('/api/accounts/me')]);
    state.config = config;
    byId('mail-note').textContent = config.mail_ready ? 'Заказ отправится поставщику по e-mail с Excel-вложением.' : 'Отправка почты пока не подключена. Готовый заказ можно скачать в Excel.';
    byId('admin-password').required = config.upload_requires_password;
    const note = byId('history-note');
    if (config.history_ready) {
      state.user = identity.user;
      note.textContent = state.user ? 'Готовые заказы сохраняются в вашем личном списке.' : 'Войдите, чтобы оформлять и сохранять заказы: ';
      const link = node('a', '', state.user ? 'Мои заказы' : 'Вход и регистрация'); link.href = state.user ? '/orders/' : '/account/';
      note.append(document.createTextNode(' '), link);
    } else note.textContent = 'Сохранение истории на сервере пока не подключено. Excel-заказ можно скачать.';
    selectCartStorage();
    const local = readLocalCart(); localCartMetadata = local?._shared || null;
    await restoreCart(local); if (state.catalog.catalog_version) reconcileCart(); renderCart();
    if (state.user && config.history_ready) await initialiseAccountCart(local);
    if (byId('account-link')) byId('account-link').textContent = state.user ? 'Мой аккаунт' : 'Войти';
    updateActions();
  } catch (_) {
    byId('mail-note').textContent = 'Не удалось проверить настройки и аккаунт. Обновите страницу, чтобы восстановить свой заказ.';
    cartSyncStatus('error', 'Не удалось загрузить корзину аккаунта. Проверьте связь и повторите.');
  }
  await catalogTask;
  initialising = false;
  if (accountCart?.ready) {
    reconcileCart(); renderCart();
    const server = accountCart.lastServerDocument;
    if (state.cart.size && server && !accountCart.matches(cartDocument())) saveCart();
    else writeLocalCart();
  } else if (storageKey && !state.user) writeLocalCart();
  updateActions();
}

byId('search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(filterProducts, 60); });
byId('load-more').addEventListener('click', () => { state.shown += 60; renderProducts(); });
for (const field of ['name', 'price']) byId('sort-' + field)?.addEventListener('click', () => sortProducts(field));
byId('clear-cart').addEventListener('click', () => { if (state.busy || !cartEditable()) return; byId('clear-cart-dialog').showModal(); byId('clear-cart-cancel').focus(); });
for (const id of ['clear-cart-cancel', 'clear-cart-close']) byId(id).addEventListener('click', () => byId('clear-cart-dialog').close());
byId('clear-cart-confirm').addEventListener('click', () => { if (state.busy || !cartEditable()) return; state.cart.clear(); state.cartProducts.clear(); basketChanged(); renderCart(); byId('clear-cart-dialog').close(); });
byId('customer-form').addEventListener('input', () => { if (state.busy || !cartEditable()) return; state.sentSignature = null; saveCart(); updateActions(); });
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
byId('toggle-cart').addEventListener('click', () => setCartOpen(byId('order-panel').hidden, window.matchMedia('(max-width: 900px)').matches));
byId('close-cart').addEventListener('click', () => setCartOpen(false));
byId('jump-to-order').addEventListener('click', () => setCartOpen(byId('order-panel').hidden, true));
byId('cart-sync-retry').addEventListener('click', async () => {
  if (!accountCart?.ready) { window.location.reload(); return; }
  try {
    if (accountCart.pending) await accountCart.flush();
    else await accountCart.refresh();
  } catch (_) { /* Synchronization reports its own error and keeps the local draft. */ }
});
async function refreshAccountCart(checkCatalog = false) {
  if (initialising || state.busy || document.visibilityState === 'hidden') return;
  try {
    if (accountCart?.ready) {
      if (accountCart.pending) await accountCart.flush();
      else await accountCart.refresh();
    }
    if (checkCatalog) await loadCatalog(false);
  } catch (_) { /* Do not replace an unconfirmed draft after an unsuccessful refresh. */ }
}
setInterval(() => refreshAccountCart(), 20000);
window.addEventListener('focus', () => refreshAccountCart(true));
window.addEventListener('online', () => refreshAccountCart(true));
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshAccountCart(true); });
window.addEventListener('pagehide', () => { if (accountCart?.ready && accountCart.pending) accountCart.flush({ keepalive: true }).catch(() => {}); });
document.addEventListener('click', async (event) => {
  if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || !accountCart?.ready || (!accountCart.pending && !accountCart.saving)) return;
  const link = event.target.closest('a[href]');
  if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self')) return;
  const destination = new URL(link.href, window.location.href);
  if (destination.origin !== window.location.origin || (destination.pathname === window.location.pathname && destination.hash)) return;
  event.preventDefault();
  try { await accountCart.flush(); window.location.assign(destination.href); }
  catch (_) { /* Keep the page open so a failed save can be retried. */ }
});
byId('order-panel').addEventListener('keydown', (event) => { if (event.key === 'Escape') { event.preventDefault(); setCartOpen(false); } });
window.addEventListener('pageshow', (event) => { if (event.persisted) window.location.reload(); });
start();
