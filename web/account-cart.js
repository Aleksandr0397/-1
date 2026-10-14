'use strict';

(() => {
  const clone = (value) => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === 'object') {
      const result = {};
      for (const key of Object.keys(value).sort()) {
        if (value[key] !== undefined && value[key] !== null) result[key] = canonical(value[key]);
      }
      return result;
    }
    return value;
  }
  function comparable(document) {
    if (document === null) return null;
    const result = clone(document);
    if (result && Array.isArray(result.items)) {
      result.customer = { name: '', contact: '', comment: '', ...result.customer };
      result.items = result.items.map(item => ({ ...item, product: {
        available: true, restocked: false, unresolved: false, ...item.product,
      } }));
    }
    return canonical(result);
  }
  const identical = (left, right) => JSON.stringify(comparable(left)) === JSON.stringify(comparable(right));
  function validState(value) {
    return value && Number.isSafeInteger(value.revision) && value.revision >= 0
      && (value.cart === null || (value.cart && typeof value.cart === 'object'
        && !Array.isArray(value.cart) && Array.isArray(value.cart.items)
        && value.cart.customer && typeof value.cart.customer === 'object'
        && !Array.isArray(value.cart.customer)));
  }
  function failure(message, properties = {}) {
    return Object.assign(new Error(message), properties);
  }

  class AccountCartSync {
    constructor({ ownerId, getDocument, applyDocument, onStatus, onAcknowledged, onConflict, onAuthError }) {
      if (!ownerId || typeof getDocument !== 'function' || typeof applyDocument !== 'function') {
        throw new TypeError('AccountCartSync requires an owner and document callbacks.');
      }
      this._ownerId = String(ownerId);
      this._callbacks = { getDocument, applyDocument, onStatus, onAcknowledged, onConflict, onAuthError };
      this._revision = 0;
      this._serverCart = null;
      this._ready = false;
      this._pending = false;
      this._saving = false;
      this._disposed = false;
      this._sequence = 0;
      this._timer = null;
      this._flight = null;
      this._loadFlight = null;
      this._refreshFlight = null;
    }

    get revision() { return this._revision; }
    get ready() { return this._ready && !this._disposed; }
    get pending() { return this._pending; }
    get saving() { return this._saving; }
    get lastServerDocument() { return clone(this._serverCart); }
    matches(document) { return identical(this._serverCart, document); }

    _assertActive() {
      if (this._disposed) throw failure('Синхронизация этого аккаунта завершена.', { disposed: true });
    }
    _status(state, message) {
      this._lastStatus = [state, message];
      if (this._disposed || typeof this._callbacks.onStatus !== 'function') return;
      try {
        const result = this._callbacks.onStatus(state, message);
        if (result && typeof result.catch === 'function') result.catch(() => {});
      } catch (_) { /* A status display must not interrupt a confirmed save. */ }
    }
    async _callback(name, ...args) {
      this._assertActive();
      const callback = this._callbacks[name];
      if (typeof callback === 'function') await callback(...args);
      this._assertActive();
    }
    _document() {
      this._assertActive();
      return clone(this._callbacks.getDocument());
    }
    _state() { return { revision: this._revision, cart: clone(this._serverCart) }; }
    async _request(method, document, keepalive = false) {
      this._assertActive();
      const options = {
        method, cache: 'no-store', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Account-Id': this._ownerId },
      };
      if (document !== undefined) {
        options.body = JSON.stringify(document);
        // Keepalive has a shared browser quota; large carts use an ordinary request.
        const small = options.body.length <= 20000 || (typeof TextEncoder !== 'undefined'
          && new TextEncoder().encode(options.body).byteLength <= 60000);
        if (keepalive && small) options.keepalive = true;
      }
      const response = await fetch('/api/accounts/cart', options);
      this._assertActive();
      let body;
      try { body = await response.json(); } catch (_) { /* A hosting proxy may return HTML. */ }
      this._assertActive();
      if (!response.ok) {
        throw failure(typeof body?.detail === 'string' ? body.detail : 'Не удалось сохранить корзину. Повторите попытку.', {
          status: response.status,
          cartConflict: response.status === 409 && body?.code === 'cart_conflict' && validState(body),
          state: body,
        });
      }
      if (!validState(body)) throw failure('Сайт вернул неполные данные корзины. Повторите попытку.');
      return body;
    }
    async _error(error) {
      if (this._disposed || error.disposed) return;
      if (error.status === 401 || (error.status === 409 && !error.cartConflict)) {
        this._ready = false;
        this._status('error', 'Аккаунт изменился. Войдите снова, чтобы сохранить корзину.');
        await this._callback('onAuthError', error);
      } else {
        this._status('error', 'Корзина пока не сохранена на сайте. Проверьте связь и повторите сохранение.');
      }
    }
    async _acknowledge(result) {
      this._assertActive();
      this._revision = result.revision;
      this._serverCart = clone(result.cart);
      await this._callback('onAcknowledged', this._state());
    }
    async _adopt(result) {
      this._assertActive();
      this._revision = result.revision;
      this._serverCart = clone(result.cart);
      await this._callback('applyDocument', clone(result.cart));
      this._ready = true;
      await this._callback('onAcknowledged', this._state());
    }
    async _conflict(result, local) {
      this._assertActive();
      // The integration keeps this document as a private recovery copy before replacement.
      await this._callback('onConflict', clone(local), clone(result));
      this._pending = false;
      clearTimeout(this._timer); this._timer = null;
      await this._adopt(result);
      this._status('conflict', 'Корзина обновлена на другом устройстве. Загружена её последняя версия. Повторите последнее изменение.');
      return failure('Корзина изменилась на другом устройстве. Проверьте заказ и повторите действие.', { cartConflict: true, handled: true });
    }

    load({ migration = null, pending = null } = {}) {
      if (this._loadFlight) return this._loadFlight;
      const promise = this._load(migration, pending);
      this._loadFlight = promise;
      const clear = () => { if (this._loadFlight === promise) this._loadFlight = null; };
      promise.then(clear, clear);
      return promise;
    }
    async _load(migration, pending) {
      this._assertActive();
      this._status('loading', 'Загружаем вашу корзину…');
      try {
        const result = await this._request('GET');
        this._assertActive();
        if (pending && validState(pending) && pending.cart !== null) {
          if (identical(result.cart, pending.cart)) {
            this._pending = false;
            await this._adopt(result);
          } else if (result.revision === pending.revision) {
            this._revision = result.revision;
            this._serverCart = clone(result.cart);
            await this._callback('applyDocument', clone(pending.cart));
            this._ready = true;
            this._pending = true;
            this._sequence += 1;
            await this.flush();
          } else {
            await this._conflict(result, pending.cart);
            return this._state();
          }
        } else if (result.cart === null && migration && Array.isArray(migration.items) && migration.items.length) {
          this._revision = result.revision;
          this._serverCart = null;
          await this._callback('applyDocument', clone(migration));
          this._ready = true;
          this._pending = true;
          this._sequence += 1;
          await this.flush();
        } else {
          // An initialized empty server cart wins over old local data, just like a full one.
          this._pending = false;
          await this._adopt(result);
        }
        if (!this._pending) this._status('saved', 'Корзина сохранена в вашем аккаунте.');
        return this._state();
      } catch (error) {
        if (error.handled) return this._state();
        await this._error(error);
        throw error;
      }
    }

    schedule() {
      if (this._disposed) return;
      this._sequence += 1;
      this._pending = true;
      clearTimeout(this._timer);
      if (!this._ready) return;
      this._status('saving', 'Сохраняем корзину…');
      this._timer = setTimeout(() => {
        this._timer = null;
        this.flush().catch(() => {});
      }, 250);
    }
    flush({ keepalive = false } = {}) {
      if (this._flight) return this._flight;
      const promise = this._flush(keepalive);
      this._flight = promise;
      const clear = () => { if (this._flight === promise) this._flight = null; };
      promise.then(clear, clear);
      return promise;
    }
    async _flush(keepalive) {
      this._assertActive();
      if (!this._ready) throw failure('Сначала загрузите корзину аккаунта.');
      clearTimeout(this._timer); this._timer = null;
      if (!this._pending) return this._state();
      this._saving = true;
      this._status('saving', 'Сохраняем корзину…');
      try {
        while (this._pending) {
          this._assertActive();
          const sequence = this._sequence;
          const document = this._document();
          const revision = this._revision;
          let result;
          try {
            result = await this._request('PUT', { revision, cart: document }, keepalive);
          } catch (error) {
            if (!error.cartConflict) throw error;
            if (identical(error.state.cart, document)) result = error.state;
            else throw await this._conflict(error.state, this._document());
          }
          this._assertActive();
          if (result.revision <= revision) throw failure('Не удалось подтвердить сохранение корзины. Повторите попытку.');
          // Acknowledging a save never applies its old document over newer local edits.
          this._pending = this._sequence !== sequence;
          await this._acknowledge(result);
          this._pending = this._sequence !== sequence;
        }
        this._status('saved', 'Корзина сохранена в вашем аккаунте.');
        return this._state();
      } catch (error) {
        if (!error.handled && !this._disposed) {
          this._pending = true;
          await this._error(error);
        }
        throw error;
      } finally {
        this._saving = false;
        if (this._lastStatus) this._status(...this._lastStatus);
      }
    }

    refresh() {
      if (this._refreshFlight) return this._refreshFlight;
      if (!this.ready || this._pending || this._saving || this._loadFlight) return Promise.resolve(null);
      const promise = this._refresh();
      this._refreshFlight = promise;
      const clear = () => { if (this._refreshFlight === promise) this._refreshFlight = null; };
      promise.then(clear, clear);
      return promise;
    }
    async _refresh() {
      const sequence = this._sequence;
      const revision = this._revision;
      try {
        const result = await this._request('GET');
        this._assertActive();
        // A GET begun before a local edit must never replace that edit or advance its CAS base.
        if (this._sequence !== sequence || this._pending || this._saving || this._revision !== revision) return null;
        if (result.revision !== revision) {
          await this._adopt(result);
          this._status('saved', 'Корзина обновлена из вашего аккаунта.');
        }
        return this._state();
      } catch (error) {
        await this._error(error);
        throw error;
      }
    }
    dispose() {
      this._disposed = true;
      this._ready = false;
      clearTimeout(this._timer); this._timer = null;
    }
  }

  window.AccountCartSync = AccountCartSync;
})();
