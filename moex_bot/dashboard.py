"""Public, read-only dashboard for a virtual T-Invest sandbox portfolio."""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="theme-color" content="#f5f6f2">
  <title>Мой виртуальный портфель · SBER</title>
  <style>
    :root{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#202922;background:#f5f6f2;font-synthesis:none}
    *{box-sizing:border-box}body{margin:0}button{font:inherit}button:focus-visible{outline:3px solid #48836a;outline-offset:4px}[hidden]{display:none!important}
    .page{max-width:1120px;margin:0 auto;padding:36px 28px 28px}.header{display:flex;align-items:center;justify-content:space-between;gap:24px;margin-bottom:30px}
    .brand{display:flex;align-items:center;gap:13px;min-width:0}.mark{display:grid;place-items:center;width:42px;height:42px;border-radius:14px;background:#245c45;color:#fff;font-size:22px;font-weight:700;flex-shrink:0}
    .brand-name{margin:0;font-size:16px;font-weight:650}.brand-note{margin:4px 0 0;color:#697269;font-size:13px}.header-actions{display:flex;align-items:center;gap:16px}
    .connection{display:flex;align-items:center;gap:8px;font-size:13px;color:#697269}.dot{width:7px;height:7px;border-radius:50%;background:#849084;flex-shrink:0}
    .connection[data-state="connected"]{color:#245c45}.connection[data-state="connected"] .dot{background:#378460}.connection[data-state="stale"] .dot{background:#b67b20}.connection[data-state="error"] .dot{background:#bf5043}
    .refresh{min-height:42px;padding:9px 14px;border:1px solid #d9ded4;border-radius:11px;background:#fff;color:#334b3b;cursor:pointer;display:flex;gap:8px;align-items:center;justify-content:center}
    .refresh:hover{background:#edf1e8}.refresh:disabled{cursor:wait;opacity:.6}.refresh svg{width:15px;height:15px;flex-shrink:0}
    .intro{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;margin-bottom:24px}h1{font-size:clamp(27px,4vw,36px);line-height:1.18;letter-spacing:-1.1px;margin:13px 0 8px;font-weight:650}
    .sandbox{display:inline-flex;align-items:center;gap:7px;background:#e8edbd;border:1px solid #dde3ac;border-radius:7px;padding:6px 9px;color:#485227;font-size:12px;font-weight:600}
    .subtitle{margin:0;color:#697269;font-size:14px;line-height:1.6}.updated{color:#697269;font-size:12px;text-align:right;line-height:1.6;max-width:225px}
    .notice{border-radius:12px;background:#fff2d7;border:1px solid #e8d6ab;color:#735318;padding:13px 16px;font-size:13px;line-height:1.6;margin-bottom:18px}
    .notice[data-tone="error"]{background:#fff0eb;border-color:#edcfc4;color:#88453c}.grid{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(280px,1fr);gap:18px}
    .card{background:#fff;border:1px solid #e1e5dd;border-radius:20px;padding:25px;min-width:0}.card-heading{font-size:15px;font-weight:650;letter-spacing:-.15px;margin:0}.eyebrow{font-size:12px;font-weight:550;color:#697269;margin:0 0 11px}
    .portfolio{background:#245c45;color:#fff;border-color:#245c45}.portfolio .eyebrow{color:#d0dfd2}.balance{font-size:clamp(32px,5vw,48px);font-weight:550;letter-spacing:-1.6px;line-height:1.18;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;margin:0}
    .balance-note{margin:9px 0 0;font-size:12px;line-height:1.6;color:#d0dfd2}.portfolio-details{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:24px;padding-top:21px;border-top:1px solid #ffffff29}
    .metric-label{display:block;font-size:12px;color:#d0dfd2;margin-bottom:8px}.metric-value{font-size:20px;font-weight:550;letter-spacing:-.4px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.metric-note{display:block;margin-top:5px;font-size:12px;color:#d0dfd2}
    .signal-top{display:flex;justify-content:space-between;gap:12px;align-items:center}.tag{font-size:11px;background:#f0f3ec;border-radius:6px;color:#536350;padding:5px 7px;white-space:nowrap}
    .action{margin:22px 0 8px;font-size:26px;font-weight:600;letter-spacing:-.6px}.reason{color:#697269;font-size:13px;line-height:1.6;margin:0;min-height:42px}.signal-meta{display:flex;flex-wrap:wrap;gap:8px 18px;margin:18px 0 0;font-size:12px;color:#697269;line-height:1.6}
    .mode{font-size:12px;color:#52644f;background:#f3f5ef;border-radius:10px;padding:12px 13px;margin-top:18px;line-height:1.65}.mode strong{display:block;color:#334b3b;font-weight:600;margin-bottom:3px}
    .chart-top{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}.chart-subtitle{font-size:12px;color:#697269;margin:7px 0 0;line-height:1.5}.price{text-align:right;font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.5px;white-space:nowrap}
    .price-label{display:block;font-size:11px;font-weight:400;letter-spacing:0;color:#697269;margin-top:4px}.chart-body{margin-top:24px}.chart-scale{display:flex;justify-content:space-between;color:#697269;font-size:11px;font-variant-numeric:tabular-nums;margin-bottom:6px}.chart-svg{width:100%;height:160px;display:block;overflow:visible}.chart-dates{display:flex;justify-content:space-between;font-size:11px;color:#697269;margin-top:12px;gap:12px}
    .empty{display:flex;align-items:center;justify-content:center;min-height:140px;text-align:center;color:#697269;font-size:13px;line-height:1.65;margin:14px 0 0;padding:12px}.events{list-style:none;margin:20px 0 0;padding:0}.event{display:flex;gap:12px;padding:13px 0;border-bottom:1px solid #edf0e8}.event:first-child{padding-top:0}.event:last-child{padding-bottom:0;border:0}.event-dot{width:7px;height:7px;border-radius:50%;background:#9eb69a;flex-shrink:0;margin-top:6px}.event-label{font-size:13px;line-height:1.5;color:#344735;overflow-wrap:anywhere}.event-time{display:block;font-size:11px;color:#697269;margin-top:5px;line-height:1.5}
    .footer{display:flex;justify-content:space-between;gap:20px;margin-top:24px;color:#697269;font-size:11px;line-height:1.7}.footer p{margin:0}.footer-right{text-align:right}
    @media(max-width:760px){.page{padding:22px 18px}.header{gap:14px;margin-bottom:28px}.header-actions{gap:10px;flex-shrink:0}.connection{font-size:12px}.connection span:last-child{max-width:86px}.refresh{padding:10px}.refresh-label{display:none}.intro{display:block}.updated{text-align:left;max-width:none;margin-top:12px}.grid{grid-template-columns:1fr}.card{padding:23px;border-radius:18px}.reason{min-height:0}.balance{font-size:42px}.footer{display:block}.footer-right{text-align:left;margin-top:8px!important}}
    @media(max-width:380px){.page{padding:20px 14px}.brand-note{font-size:11px}.connection{font-size:11px}.header-actions{gap:8px}.mark{width:35px;height:35px;border-radius:11px;font-size:19px}.brand{gap:9px}.brand-name{font-size:14px}.card{padding:20px}.balance{font-size:36px}.metric-value{font-size:18px}.price{font-size:20px}.portfolio-details{gap:15px}}
    @media(prefers-reduced-motion:no-preference){.refresh{transition:background .15s ease}}
  </style>
</head>
<body>
  <main class="page">
    <header class="header">
      <div class="brand"><span class="mark" aria-hidden="true">М</span><div><p class="brand-name">Мой портфель</p><p class="brand-note">Т‑Инвестиции · SBER</p></div></div>
      <div class="header-actions">
        <div id="connection" class="connection" data-state="loading" role="status"><span class="dot" aria-hidden="true"></span><span id="connectionText">Подключаемся</span></div>
        <button id="refresh" class="refresh" type="button" aria-label="Обновить данные">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 7a7 7 0 0 1 11.5-1L20 9M4 15l2.4 3a7 7 0 0 0 11.5-1"/></svg><span class="refresh-label">Обновить</span>
        </button>
      </div>
    </header>
    <div class="intro">
      <div><span class="sandbox">Песочница · виртуальные деньги</span><h1>Портфель под рукой</h1><p class="subtitle">Баланс, цена SBER и последний сигнал бота.</p></div>
      <div id="updatedAt" class="updated">Данные ещё не получены</div>
    </div>
    <div id="notice" class="notice" role="status">Загружаем данные песочницы. Первое подключение может занять немного времени.</div>
    <div class="grid">
      <section class="card portfolio" aria-labelledby="balanceHeading">
        <h2 id="balanceHeading" class="eyebrow">Стоимость виртуального портфеля</h2>
        <p id="equity" class="balance">—</p>
        <p class="balance-note">Денежный остаток и стоимость акций</p>
        <div class="portfolio-details">
          <div><span class="metric-label">Свободные деньги</span><span id="cash" class="metric-value">—</span><span class="metric-note">Виртуальные рубли</span></div>
          <div><span class="metric-label">Акции SBER</span><span id="shares" class="metric-value">—</span><span class="metric-note">В портфеле</span></div>
        </div>
      </section>
      <section class="card" aria-labelledby="signalHeading">
        <div class="signal-top"><h2 id="signalHeading" class="card-heading">Последний сигнал</h2><span class="tag">SMA 20 / 60</span></div>
        <p id="action" class="action">Ожидаем данные</p><p id="reason" class="reason">Сигнал появится после проверки завершённых дневных свечей.</p>
        <div class="signal-meta"><span id="signalTime">Время сигнала: —</span><span id="plannedLots" hidden></span></div>
        <div class="mode"><strong>Режим наблюдения</strong>Проверка по запросу, без фоновой торговли. Отправка заявок с этой страницы не включена.</div>
      </section>
      <section class="card" aria-labelledby="chartHeading">
        <div class="chart-top"><div><h2 id="chartHeading" class="card-heading">Цена SBER</h2><p id="chartSummary" class="chart-subtitle">Завершённые дневные свечи</p></div><div class="price"><span id="price">—</span><span class="price-label">Последняя цена</span></div></div>
        <div id="chartEmpty" class="empty">История цен ещё не получена.</div>
        <div id="chartBody" class="chart-body" hidden>
          <div class="chart-scale"><span id="chartLow"></span><span id="chartHigh"></span></div>
          <svg id="chart" class="chart-svg" viewBox="0 0 600 160" preserveAspectRatio="none" role="img" aria-label="История цены SBER">
            <defs><linearGradient id="chartFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#80a88a" stop-opacity=".24"/><stop offset="100%" stop-color="#80a88a" stop-opacity=".02"/></linearGradient></defs>
            <path d="M0 148H600M0 80H600M0 12H600" fill="none" stroke="#edf0e8" stroke-width="1"/>
            <path id="chartArea" fill="url(#chartFill)"/>
            <path id="chartLine" fill="none" stroke="#367052" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
            <circle id="chartEnd" r="4" fill="#367052" vector-effect="non-scaling-stroke"/>
          </svg>
          <div class="chart-dates"><span id="chartStartDate"></span><span id="chartEndDate"></span></div>
        </div>
      </section>
      <section class="card" aria-labelledby="eventsHeading">
        <h2 id="eventsHeading" class="card-heading">События песочницы</h2>
        <p id="eventsEmpty" class="empty">Пока нет событий для отображения.</p><ul id="events" class="events" hidden></ul>
      </section>
    </div>
    <footer class="footer"><p>Только виртуальные средства песочницы Т‑Инвестиций.<br>Результаты не отражают доходность на реальном счёте.</p><p class="footer-right">Время указано по Москве.<br>Автообновление каждые 30 секунд, пока страница открыта.</p></footer>
  </main>
  <script>
    'use strict';
    const byId = (id) => document.getElementById(id);
    const moneyFormat = new Intl.NumberFormat('ru-RU', {style:'currency', currency:'RUB', minimumFractionDigits:2, maximumFractionDigits:2});
    const countFormat = new Intl.NumberFormat('ru-RU');
    const timeFormat = new Intl.DateTimeFormat('ru-RU', {timeZone:'Europe/Moscow', day:'2-digit', month:'short', year:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit'});
    const dateFormat = new Intl.DateTimeFormat('ru-RU', {timeZone:'Europe/Moscow', day:'2-digit', month:'short', year:'numeric'});
    const actions = {hold:'Удерживать', buy:'План: купить', sell:'План: продать', wait:'Ожидать'};
    const reasons = {
      at_target:'Текущая позиция соответствует сигналу стратегии.',
      rebalance_buy:'Сигнал стратегии предусматривает покупку SBER.',
      rebalance_sell:'Сигнал стратегии предусматривает продажу SBER.',
      sma_buy_proposal:'Быстрая средняя выше медленной. Стратегия предусматривает покупку SBER.',
      sma_sell_proposal:'Быстрая средняя не выше медленной. Стратегия предусматривает продажу SBER.',
      sma_hold_position:'Быстрая средняя выше медленной. Стратегия предусматривает удержание акций.',
      sma_hold_cash:'Сигнал на покупку отсутствует. Стратегия предусматривает сохранение денежных средств.',
      drawdown_exit:'Достигнут порог просадки. Стратегия предусматривает выход из позиции.',
      risk_halt:'Покупки остановлены из-за ограничения по просадке.',
      no_completed_candle:'Пока нет завершённой дневной свечи для расчёта.',
      stale_signal_candle:'Последняя свеча устарела. Нужны новые рыночные данные.',
      signal_already_handled:'Этот сигнал уже обработан.',
      signal_order_failed:'Предыдущая заявка по этому сигналу не исполнилась.',
      insufficient_history:'Недостаточно завершённых свечей для расчёта средних.',
      insufficient_funds_for_lot:'Свободных виртуальных средств недостаточно для одного лота.',
      insufficient_allocation_for_lot:'Размер позиции по правилам стратегии меньше одного лота.',
      weekend_calendar_guard:'План рассчитан. Отправка приостановлена проверкой торгового дня.',
      pending_order_uncertain:'Результат предыдущей виртуальной заявки требует проверки.',
      pending_order_open:'Предыдущая виртуальная заявка ещё открыта.',
      active_orders:'В песочнице есть открытая виртуальная заявка. Ожидаем её завершения.',
      active_sandbox_orders:'В песочнице есть открытая виртуальная заявка. Ожидаем её завершения.',
      buying_unavailable:'Покупка SBER сейчас недоступна в песочнице.',
      selling_unavailable:'Продажа SBER сейчас недоступна в песочнице.',
      pending_order_executed:'Предыдущая виртуальная заявка исполнена.',
      pending_order_failed:'Предыдущая виртуальная заявка завершилась без исполнения.',
      submission_uncertain:'Результат отправки виртуальной заявки требует проверки.',
      order_rejected:'Виртуальная заявка отклонена песочницей.',
      order_cancelled:'Виртуальная заявка отменена.'
    };
    let snapshot = null;
    let refreshing = false;
    let refreshFailed = false;
    let polling = null;

    function decimal(value) {
      if (typeof value !== 'string' || !/^-?\d+(?:\.\d+)?$/.test(value)) return null;
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    }
    function money(value) {
      const number = decimal(value);
      return number === null ? '—' : moneyFormat.format(number);
    }
    function date(value) {
      if (typeof value !== 'string' || !value) return null;
      const parsed = new Date(value);
      return Number.isFinite(parsed.getTime()) ? parsed : null;
    }
    function timestamp(value) {
      const parsed = date(value);
      return parsed ? timeFormat.format(parsed) + ' мск' : '—';
    }
    function notice(text, tone = 'stale') {
      byId('notice').textContent = text;
      byId('notice').dataset.tone = tone;
      byId('notice').hidden = !text;
    }
    function setConnection(state, label) {
      byId('connection').dataset.state = state;
      byId('connectionText').textContent = label;
    }
    function updateFreshness() {
      if (refreshing) {
        setConnection('loading', snapshot ? 'Обновляем' : 'Подключаемся');
        return;
      }
      if (refreshFailed || (snapshot && snapshot.status === 'error')) {
        const previousData = snapshot && date(snapshot.updated_at);
        setConnection(previousData ? 'stale' : 'error', previousData ? 'Данные устарели' : 'Нет подключения');
        notice(previousData ? 'Не удалось обновить данные. Показан последний полученный результат; попробуйте обновить ещё раз.' : 'Не удалось получить данные песочницы. Попробуйте обновить страницу немного позже.', 'error');
        return;
      }
      if (!snapshot || snapshot.status === 'loading') {
        setConnection('loading', 'Ожидаем данные');
        notice('Данные песочницы пока не готовы. Страница проверит их при следующем обновлении.');
        return;
      }
      const updated = date(snapshot.updated_at);
      if (!updated || Date.now() - updated.getTime() > 120000) {
        setConnection('stale', 'Данные устарели');
        notice(updated ? 'Последнее обновление было более двух минут назад. Значения могут быть устаревшими.' : 'Время обновления не получено. Актуальность значений пока не подтверждена.');
        return;
      }
      setConnection('connected', 'Подключено');
      notice('');
    }
    function renderChart(values) {
      const points = (Array.isArray(values) ? values : []).filter((point) => point && date(point.time) && decimal(point.price) !== null).slice(-200);
      byId('chartBody').hidden = points.length === 0;
      byId('chartEmpty').hidden = points.length !== 0;
      byId('chartSummary').textContent = points.length ? 'Дневные свечи · точек: ' + countFormat.format(points.length) : 'Завершённые дневные свечи';
      if (!points.length) return;
      const prices = points.map((point) => decimal(point.price));
      const low = Math.min(...prices);
      const high = Math.max(...prices);
      const range = high - low;
      const coordinates = prices.map((price, index) => ({x:points.length === 1 ? 300 : index * 592 / (points.length - 1) + 4, y:range ? 148 - (price - low) / range * 136 : 80}));
      const path = coordinates.map((point, index) => (index ? 'L' : 'M') + point.x.toFixed(2) + ',' + point.y.toFixed(2)).join(' ');
      const first = coordinates[0];
      const last = coordinates[coordinates.length - 1];
      byId('chartLine').setAttribute('d', path);
      byId('chartArea').setAttribute('d', path + ' L' + last.x.toFixed(2) + ',160 L' + first.x.toFixed(2) + ',160 Z');
      byId('chartEnd').setAttribute('cx', last.x.toFixed(2));
      byId('chartEnd').setAttribute('cy', last.y.toFixed(2));
      byId('chartLow').textContent = 'Мин. ' + moneyFormat.format(low);
      byId('chartHigh').textContent = 'Макс. ' + moneyFormat.format(high);
      byId('chartStartDate').textContent = dateFormat.format(date(points[0].time));
      byId('chartEndDate').textContent = dateFormat.format(date(points[points.length - 1].time));
      byId('chart').setAttribute('aria-label', 'История цены SBER: ' + points.length + ' точек. Минимум ' + moneyFormat.format(low) + ', максимум ' + moneyFormat.format(high) + '.');
    }
    function renderEvents(values) {
      const events = (Array.isArray(values) ? values : []).filter((event) => event && typeof event.label === 'string' && date(event.time)).slice(-12).reverse();
      const list = byId('events');
      list.replaceChildren();
      list.hidden = !events.length;
      byId('eventsEmpty').hidden = !!events.length;
      for (const event of events) {
        const item = document.createElement('li');
        item.className = 'event';
        const dot = document.createElement('span');
        dot.className = 'event-dot';
        dot.setAttribute('aria-hidden', 'true');
        const body = document.createElement('div');
        const label = document.createElement('span');
        label.className = 'event-label';
        label.textContent = event.label;
        const time = document.createElement('time');
        time.className = 'event-time';
        time.dateTime = event.time;
        time.textContent = timestamp(event.time);
        body.append(label, time);
        item.append(dot, body);
        list.append(item);
      }
    }
    function render(value) {
      byId('equity').textContent = money(value.equity);
      byId('cash').textContent = money(value.cash);
      byId('price').textContent = money(value.price);
      byId('shares').textContent = Number.isSafeInteger(value.shares) && value.shares >= 0 ? countFormat.format(value.shares) + ' шт.' : '—';
      byId('updatedAt').textContent = date(value.updated_at) ? 'Обновлено ' + timestamp(value.updated_at) : 'Время обновления не получено';
      byId('action').textContent = Object.hasOwn(actions, value.last_action) ? actions[value.last_action] : 'Сигнала пока нет';
      byId('reason').textContent = Object.hasOwn(reasons, value.action_reason) ? reasons[value.action_reason] : 'Сигнал появится после проверки завершённых дневных свечей.';
      byId('signalTime').textContent = 'Время сигнала: ' + timestamp(value.signal_time);
      const lots = Number.isSafeInteger(value.planned_lots) && value.planned_lots > 0 ? value.planned_lots : null;
      byId('plannedLots').hidden = lots === null;
      byId('plannedLots').textContent = lots === null ? '' : 'План, лотов: ' + countFormat.format(lots);
      renderChart(value.chart);
      renderEvents(value.events);
    }
    async function refresh() {
      if (refreshing || document.hidden) return;
      refreshing = true;
      byId('refresh').disabled = true;
      updateFreshness();
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 20000);
      try {
        const response = await fetch('/api/status', {method:'GET', cache:'no-store', credentials:'omit', signal:controller.signal});
        if (!response.ok) throw new Error('status_unavailable');
        const value = await response.json();
        if (!value || !['connected', 'loading', 'error'].includes(value.status) || value.execution_mode !== 'observe_on_demand') throw new Error('invalid_status');
        refreshFailed = value.status === 'error';
        if (value.status !== 'error' || date(value.updated_at) || !snapshot) {
          snapshot = value;
          render(value);
        }
      } catch (_) {
        refreshFailed = true;
      } finally {
        clearTimeout(timeout);
        refreshing = false;
        byId('refresh').disabled = false;
        updateFreshness();
        scheduleRefresh();
      }
    }
    function scheduleRefresh() {
      if (polling !== null) clearTimeout(polling);
      polling = null;
      if (!document.hidden) {
        polling = setTimeout(refresh, !refreshFailed && snapshot && snapshot.status === 'loading' ? 5000 : 30000);
      }
    }
    function startPolling() {
      if (polling !== null) clearTimeout(polling);
      polling = null;
      if (!document.hidden) refresh();
    }
    byId('refresh').addEventListener('click', refresh);
    document.addEventListener('visibilitychange', startPolling);
    startPolling();
  </script>
</body>
</html>
""".encode("utf-8")
