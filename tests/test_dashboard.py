"""Run the dashboard script against its read-only public boundary, without a browser."""

from html.parser import HTMLParser
import json
import shutil
import subprocess
import unittest

from moex_bot.dashboard import DASHBOARD_HTML


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.scripts = []
        self.in_script = False

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if "id" in attributes:
            self.ids.append(attributes["id"])
        if tag == "script":
            self.in_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.scripts.append(data)


_HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
class Element {
  constructor() {this.textContent = ''; this.dataset = {}; this.attributes = {}; this.children = []; this.listeners = {};}
  set innerHTML(_) {throw new Error('Unsafe HTML insertion');}
  setAttribute(key, value) {this.attributes[key] = value;}
  append(...items) {this.children.push(...items);}
  replaceChildren(...items) {this.children = items;}
  addEventListener(event, callback) {this.listeners[event] = callback;}
}
const elements = Object.fromEntries(input.ids.map((id) => [id, new Element()]));
const timers = new Map();
const replies = [];
const requests = [];
let timerId = 0;
const document = {
  hidden:false, listeners:{},
  getElementById(id) {assert.ok(elements[id], 'Unknown DOM id: ' + id); return elements[id];},
  createElement() {return new Element();},
  addEventListener(event, callback) {this.listeners[event] = callback;}
};
class Clock extends Date {static now() {return Date.parse('2026-10-06T12:00:00Z');}}
const context = vm.createContext({document, Intl, Date:Clock, AbortController,
  setTimeout(callback, delay) {const id = ++timerId; timers.set(id, {callback, delay}); return id;},
  clearTimeout(id) {timers.delete(id);},
  async fetch(url, options) {
    requests.push({url, options});
    assert.ok(replies.length, 'Unexpected request');
    const value = replies.shift();
    if (value === 'network_error') throw new Error('SECRET: inaccessible broker details');
    return {ok:true, json:async () => value};
  }
});
const flush = () => new Promise(setImmediate);
const h = {
  elements, timers, requests,
  fixture(overrides = {}) {
    return {status:'connected', execution_mode:'observe_on_demand', ticker:'SBER',
      updated_at:'2026-10-06T12:00:00Z', signal_time:'2026-10-05T07:00:00Z',
      equity:'100000.00', cash:'100000.00', price:'293.47', shares:0,
      last_action:'hold', action_reason:'at_target', planned_lots:0,
      chart:[{time:'2026-10-02T07:00:00Z', price:'289.11'}, {time:'2026-10-05T07:00:00Z', price:'293.47'}],
      events:[{time:'2026-10-06T12:00:00Z', label:'Проверка завершена'}], error:null, ...overrides};
  },
  async start(reply) {replies.push(reply); vm.runInContext(input.script, context); await flush();},
  async refresh(reply) {replies.push(reply); await elements.refresh.listeners.click();},
  async poll(delay, reply) {
    const entry = [...timers.entries()].find(([, value]) => value.delay === delay);
    assert.ok(entry, 'Expected poll at ' + delay + 'ms');
    timers.delete(entry[0]);
    replies.push(reply);
    await entry[1].callback();
  },
  async visibility(hidden, reply) {
    document.hidden = hidden;
    if (reply) replies.push(reply);
    document.listeners.visibilitychange();
    await flush();
  },
  pollDelays() {return [...timers.values()].map((timer) => timer.delay);},
  text() {return Object.values(elements).map((element) => element.textContent).join(' ');},
  normalized(id) {return elements[id].textContent.replace(/[\u00a0\u202f]/g, ' ');}
};
(async () => {
  await new Function('h', 'assert', 'return (async () => {' + input.scenario + '})();')(h, assert);
  for (const request of requests) {
    assert.equal(request.url, '/api/status');
    assert.equal(request.options.method, 'GET');
    assert.equal(request.options.credentials, 'omit');
    assert.equal(request.options.cache, 'no-store');
    assert.equal(request.options.body, undefined);
    assert.equal(request.options.headers, undefined);
  }
})().catch((error) => {console.error(error); process.exitCode = 1;});
"""


@unittest.skipUnless(shutil.which("node"), "Node is needed to verify browser JavaScript")
class DashboardTests(unittest.TestCase):
    def run_script(self, scenario):
        page = _Page()
        page.feed(DASHBOARD_HTML.decode("utf-8"))
        result = subprocess.run(
            [shutil.which("node"), "-e", _HARNESS],
            input=json.dumps({"ids": page.ids, "script": "".join(page.scripts), "scenario": scenario}),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_values_chart_and_events_are_rendered_as_text(self):
        self.run_script(r"""
          const label = '<img src=x onerror=alert(1)>';
          await h.start(h.fixture({events:[{time:'2026-10-06T12:00:00Z', label}],
            account_id:'SECRET_ACCOUNT', token:'SECRET_TOKEN'}));
          assert.equal(h.normalized('equity'), '100 000,00 ₽');
          assert.equal(h.normalized('cash'), '100 000,00 ₽');
          assert.equal(h.normalized('price'), '293,47 ₽');
          assert.equal(h.elements.shares.textContent, '0 шт.');
          assert.equal(h.elements.connection.dataset.state, 'connected');
          assert.equal(h.elements.notice.hidden, true);
          assert.equal(h.elements.chartBody.hidden, false);
          assert.match(h.elements.chartLine.attributes.d, /^M4.00,148.00 L596.00,12.00$/);
          assert.match(h.normalized('chartLow'), /289,11/);
          assert.match(h.normalized('chartHigh'), /293,47/);
          assert.equal(h.elements.events.children[0].children[1].children[0].textContent, label);
          assert.match(h.elements.updatedAt.textContent, /2026/);
          assert.match(h.elements.updatedAt.textContent, /15:00:00/);
          assert.equal(h.text().includes('SECRET'), false);
          assert.equal(h.elements.plannedLots.hidden, true);
          assert.deepEqual(h.pollDelays(), [30000]);
        """)

    def test_loading_retry_visibility_and_manual_refresh(self):
        self.run_script(r"""
          await h.start(h.fixture({status:'loading', updated_at:null, equity:null, cash:null,
            price:null, shares:null, last_action:null, signal_time:null, chart:[], events:[]}));
          assert.equal(h.elements.connection.dataset.state, 'loading');
          assert.equal(h.elements.equity.textContent, '—');
          assert.equal(h.elements.chartEmpty.hidden, false);
          assert.equal(h.elements.eventsEmpty.hidden, false);
          assert.deepEqual(h.pollDelays(), [5000]);
          await h.poll(5000, h.fixture());
          assert.equal(h.requests.length, 2);
          assert.deepEqual(h.pollDelays(), [30000]);
          await h.visibility(true);
          assert.deepEqual(h.pollDelays(), []);
          assert.equal(h.requests.length, 2);
          await h.visibility(false, h.fixture());
          assert.equal(h.requests.length, 3);
          await h.refresh(h.fixture({price:'301.20'}));
          assert.equal(h.normalized('price'), '301,20 ₽');
          assert.equal(h.requests.length, 4);
          assert.deepEqual(h.pollDelays(), [30000]);
        """)

    def test_errors_and_old_data_are_visible_without_leaking_details(self):
        self.run_script(r"""
          await h.start('network_error');
          assert.equal(h.elements.connection.dataset.state, 'error');
          assert.equal(h.elements.notice.hidden, false);
          assert.equal(h.text().includes('SECRET'), false);
          await h.refresh(h.fixture({updated_at:'2026-10-06T11:55:00Z'}));
          assert.equal(h.elements.connection.dataset.state, 'stale');
          assert.equal(h.elements.notice.hidden, false);
          const balance = h.elements.equity.textContent;
          await h.refresh(h.fixture({status:'error', error:'SECRET_TOKEN'}));
          assert.equal(h.elements.connection.dataset.state, 'stale');
          assert.equal(h.elements.equity.textContent, balance);
          assert.equal(h.text().includes('SECRET'), false);
          await h.refresh(h.fixture({chart:[{time:'2026-10-05T07:00:00Z', price:'293.47'}],
            last_action:'wait', action_reason:'active_sandbox_orders'}));
          assert.equal(h.elements.connection.dataset.state, 'connected');
          assert.equal(h.elements.notice.hidden, true);
          assert.equal(h.elements.action.textContent, 'Ожидать');
          assert.match(h.elements.reason.textContent, /открытая виртуальная заявка/);
          assert.equal(h.elements.chartLine.attributes.d, 'M300.00,80.00');
          assert.equal(h.elements.chartEnd.attributes.cy, '80.00');
        """)

    def test_initial_error_still_shows_previous_confirmed_server_snapshot(self):
        self.run_script(r"""
          await h.start(h.fixture({status:'error', error:'SECRET_BROKER_DETAILS',
            updated_at:'2026-10-06T11:55:00Z', cash:'98500.00', equity:'100500.00',
            last_action:'hold', action_reason:'sma_hold_cash'}));
          assert.equal(h.normalized('equity'), '100 500,00 ₽');
          assert.equal(h.normalized('cash'), '98 500,00 ₽');
          assert.equal(h.elements.connection.dataset.state, 'stale');
          assert.equal(h.elements.notice.hidden, false);
          assert.match(h.elements.reason.textContent, /сохранение денежных средств/);
          assert.equal(h.text().includes('SECRET'), false);
          assert.deepEqual(h.pollDelays(), [30000]);
        """)

    def test_automatic_mode_displays_actual_check_times_and_last_result(self):
        self.run_script(r"""
          const automation = {enabled:true, status:'waiting', interval_seconds:300,
            last_checked_at:'2026-10-06T12:00:00Z', next_check_at:'2026-10-06T12:05:00Z',
            last_result:{action:'wait', reason:'pending_order_uncertain', lots:2,
              order_status:null, token:'SECRET_ORDER_TOKEN'}, error:null};
          await h.start(h.fixture({execution_mode:'sandbox_auto', automation,
            last_action:'buy', action_reason:'sma_buy_proposal', planned_lots:99}));
          assert.match(h.elements.modeTitle.textContent, /Автоторговля в песочнице.*каждые 5 мин/);
          assert.match(h.elements.modeDescription.textContent, /Закрытие страницы.*не останавливает/);
          assert.equal(h.elements.hostingNotice.hidden, false);
          assert.equal(h.elements.automationNotice.hidden, true);
          assert.equal(h.elements.automationTimes.hidden, false);
          assert.match(h.elements.lastChecked.textContent, /15:00:00/);
          assert.match(h.elements.nextCheck.textContent, /15:05:00/);
          assert.equal(h.elements.signalHeading.textContent, 'Последний шаг бота');
          assert.equal(h.elements.action.textContent, 'Ожидать');
          assert.match(h.elements.reason.textContent, /требует проверки/);
          assert.equal(h.elements.plannedLots.textContent, 'План, лотов: 2');
          assert.equal(h.text().includes('SECRET'), false);
          assert.deepEqual(h.pollDelays(), [30000]);
          await h.visibility(true);
          assert.deepEqual(h.pollDelays(), []);
          assert.equal(h.requests.length, 1);
        """)

    def test_automation_stops_are_prominent_even_with_connected_or_retained_data(self):
        self.run_script(r"""
          const automation = {enabled:true, status:'error', interval_seconds:300,
            last_checked_at:'2026-10-06T12:00:00Z', next_check_at:null,
            last_result:null, error:'SECRET_STATE_DETAIL'};
          await h.start(h.fixture({execution_mode:'sandbox_auto', automation}));
          assert.equal(h.elements.connection.dataset.state, 'connected');
          assert.equal(h.elements.automationNotice.hidden, false);
          assert.equal(h.elements.modeTitle.textContent, 'Автоторговля остановлена');
          assert.match(h.elements.modeDescription.textContent, /Новые виртуальные заявки не отправляются/);
          assert.equal(h.elements.nextCheck.textContent, 'Следующая проверка: —');
          assert.equal(h.text().includes('SECRET'), false);
          const balance = h.elements.equity.textContent;
          await h.refresh(h.fixture({status:'error', updated_at:null, equity:null,
            execution_mode:'sandbox_auto', automation:{...automation, status:'stopped'}}));
          assert.equal(h.elements.equity.textContent, balance);
          assert.equal(h.elements.automationNotice.hidden, false);
          assert.equal(h.elements.connection.dataset.state, 'stale');
          await h.refresh(h.fixture({automation:{...automation, enabled:false, status:'stopped'}}));
          assert.equal(h.elements.hostingNotice.hidden, true);
          assert.equal(h.elements.automationNotice.hidden, true);
          assert.equal(h.elements.automationTimes.hidden, true);
          assert.equal(h.elements.modeTitle.textContent, 'Режим наблюдения');
        """)

    def test_order_execution_is_claimed_only_for_confirmed_fill(self):
        self.run_script(r"""
          const automation = {enabled:true, status:'running', interval_seconds:300,
            last_checked_at:'2026-10-06T12:00:00Z', next_check_at:'2026-10-06T12:05:00Z',
            last_result:{action:'buy', reason:'rebalance_buy', lots:2,
              order_status:'EXECUTION_REPORT_STATUS_NEW'}, error:null};
          await h.start(h.fixture({execution_mode:'sandbox_auto', automation}));
          assert.equal(h.elements.action.textContent, 'Покупка в песочнице');
          assert.equal(h.elements.orderResult.textContent, 'Заявка открыта');
          assert.equal(h.elements.plannedLots.textContent, 'Заявка, лотов: 2');
          await h.refresh(h.fixture({execution_mode:'sandbox_auto', automation:{...automation,
            last_result:{...automation.last_result, order_status:'EXECUTION_REPORT_STATUS_PARTIALLYFILL'}}}));
          assert.equal(h.elements.action.textContent, 'Покупка в песочнице');
          assert.equal(h.elements.orderResult.textContent, 'Заявка исполнена частично');
          await h.refresh(h.fixture({execution_mode:'sandbox_auto', automation:{...automation,
            last_result:{...automation.last_result, order_status:'EXECUTION_REPORT_STATUS_FILL'}}}));
          assert.equal(h.elements.action.textContent, 'Куплено в песочнице');
          assert.equal(h.elements.orderResult.textContent, 'Заявка полностью исполнена');
          await h.refresh(h.fixture({execution_mode:'sandbox_auto', automation:{...automation,
            last_result:{...automation.last_result, reason:'weekend_calendar_guard', order_status:null}}}));
          assert.equal(h.elements.action.textContent, 'План: купить');
          assert.equal(h.elements.orderResult.hidden, true);
          assert.equal(h.elements.reason.textContent, 'Сегодня выходной. Стратегия ожидает торгового дня.');
        """)
