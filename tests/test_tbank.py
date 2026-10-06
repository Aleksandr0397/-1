import io
import json
import os
import ssl
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

from moex_bot.tbank import ApiError, SANDBOX_REST_URL, TInvestClient, _http_transport, money, quotation


UTC = timezone.utc
ORDER_ID = "abf0bd9e-8f01-4c4d-9f25-e0f1fd1f06df"
SECRET = "private-test-token"


class RecordingTransport:
    """The HTTP seam receives real Requests and returns broker JSON bytes."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError("Unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, bytes) else json.dumps(response).encode()


def bar(time, *, complete=True, close="105", volume="10"):
    return {"time": time, "open": {"units": "100"}, "high": {"units": "110"},
            "low": {"units": "90"}, "close": {"units": close}, "volume": volume,
            "isComplete": complete}


class MoneyTests(unittest.TestCase):
    def test_exact_decoding_including_protobuf_zero_defaults(self):
        self.assertEqual(money({"units": "123", "nano": 456789123}), Decimal("123.456789123"))
        self.assertEqual(money({"nano": -500000000}), Decimal("-0.5"))
        self.assertEqual(money({"units": "-12", "nano": -9}), Decimal("-12.000000009"))
        self.assertEqual(money({}), Decimal(0))

    def test_exact_at_int64_boundary_regardless_of_decimal_context(self):
        value = Decimal("9223372036854775807.999999999")
        with localcontext() as ctx:
            ctx.prec = 6
            self.assertEqual(money({"units": "9223372036854775807", "nano": 999999999}), value)
            self.assertEqual(quotation(value), {"units": "9223372036854775807", "nano": 999999999})

    def test_quotation_preserves_signed_fraction(self):
        self.assertEqual(quotation(Decimal("-1.5")), {"units": "-1", "nano": -500000000})
        self.assertEqual(quotation(Decimal("-0.000000001")), {"units": "0", "nano": -1})
        self.assertEqual(quotation(Decimal("1.0000000000")), {"units": "1", "nano": 0})

    def test_rejects_malformed_or_lossy_values(self):
        for value in [{"nano": 1000000000}, {"units": "1", "nano": -1}, {"units": 1.0},
                      {"units": True}, {"units": "1.5"}, {"units": " 1"}, [],
                      {"units": "9223372036854775808"}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                money(value)
        for value in [Decimal("NaN"), Decimal("Infinity"), Decimal("1.0000000001"),
                      Decimal("1e10000"), Decimal("1e-1000000"), Decimal("-9223372036854775809"), 1.5]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                quotation(value)


class HttpContractTests(unittest.TestCase):
    def assert_request(self, transport, index, service, method, body):
        request, timeout = transport.requests[index]
        self.assertIsInstance(request, urllib.request.Request)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, f"{SANDBOX_REST_URL}/tinkoff.public.invest.api.contract.v1.{service}/{method}")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + SECRET)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), body)
        self.assertEqual(timeout, 7)

    def test_shareby_exact_ticker_class_and_protobuf_fields(self):
        transport = RecordingTransport({"instrument": {
            "uid": "share-uid", "ticker": "SBER", "classCode": "TQBR", "name": "Сбербанк",
            "currency": "rub", "lot": 10, "exchange": "MOEX", "apiTradeAvailableFlag": True,
            "buyAvailableFlag": True, "sellAvailableFlag": True,
        }})
        share = TInvestClient(SECRET, timeout=7, transport=transport).resolve_share("sber")
        self.assertEqual((share.uid, share.lot, share.currency, share.ticker), ("share-uid", 10, "rub", "SBER"))
        self.assertTrue(share.api_trade_available)
        self.assert_request(transport, 0, "InstrumentsService", "ShareBy", {
            "idType": "INSTRUMENT_ID_TYPE_TICKER", "classCode": "TQBR", "id": "SBER"})

    def test_shareby_rejects_wrong_instrument_and_invalid_metadata(self):
        base = {"uid": "share-uid", "ticker": "SBER", "classCode": "TQBR", "name": "Сбербанк",
                "currency": "rub", "lot": 10, "exchange": "MOEX"}
        for update in [{"ticker": "GAZP"}, {"lot": 0}, {"lot": True}, {"apiTradeAvailableFlag": "true"}, {"uid": ""}]:
            with self.subTest(update=update), self.assertRaises(ApiError):
                TInvestClient(SECRET, transport=RecordingTransport({"instrument": {**base, **update}})).resolve_share("SBER")

    def test_sandbox_methods_use_only_sandboxservice_with_correct_bodies(self):
        transport = RecordingTransport({"accounts": [{"id": "account"}]}, {"accountId": "new-account"},
            {"balance": {"units": "1000"}}, {"positions": []}, {"money": []},
            {"orders": [{"orderId": "broker-order"}]}, {"orderId": "broker-order"}, {"orderId": "broker-order"})
        client = TInvestClient(SECRET, timeout=7, transport=transport)
        self.assertEqual(client.list_sandbox_accounts(), ["account"])
        self.assertEqual(client.open_sandbox_account(), "new-account")
        self.assertEqual(client.sandbox_pay_in("account", Decimal("1000.25")), {"balance": {"units": "1000"}})
        self.assertEqual(client.get_sandbox_portfolio("account"), {"positions": []})
        self.assertEqual(client.get_sandbox_positions("account"), {"money": []})
        self.assertEqual(client.get_sandbox_orders("account"), [{"orderId": "broker-order"}])
        self.assertEqual(client.get_sandbox_order_state("account", ORDER_ID), {"orderId": "broker-order"})
        self.assertEqual(client.post_sandbox_order("account", "share-uid", 3, "buy", ORDER_ID), {"orderId": "broker-order"})
        expected = [
            ("GetSandboxAccounts", {}), ("OpenSandboxAccount", {}),
            ("SandboxPayIn", {"accountId": "account", "amount": {"currency": "rub", "units": "1000", "nano": 250000000}}),
            ("GetSandboxPortfolio", {"accountId": "account", "currency": "RUB"}),
            ("GetSandboxPositions", {"accountId": "account"}), ("GetSandboxOrders", {"accountId": "account"}),
            ("GetSandboxOrderState", {"accountId": "account", "orderId": ORDER_ID, "orderIdType": "ORDER_ID_TYPE_REQUEST"}),
            ("PostSandboxOrder", {"accountId": "account", "instrumentId": "share-uid", "quantity": "3", "direction": "ORDER_DIRECTION_BUY",
                                  "orderType": "ORDER_TYPE_MARKET", "orderId": ORDER_ID, "confirmMarginTrade": False}),
        ]
        for index, (method, body) in enumerate(expected):
            self.assert_request(transport, index, "SandboxService", method, body)

    def test_latest_price_requires_same_instrument_and_positive_exact_price(self):
        transport = RecordingTransport({"lastPrices": [{"instrumentUid": "share-uid", "price": {"units": "101", "nano": 1}}]})
        self.assertEqual(TInvestClient(SECRET, timeout=7, transport=transport).get_last_price("share-uid"), Decimal("101.000000001"))
        self.assert_request(transport, 0, "MarketDataService", "GetLastPrices", {"instrumentId": ["share-uid"]})
        for response in [{"lastPrices": []}, {"lastPrices": [{"instrumentUid": "other", "price": {"units": "1"}}]},
                         {"lastPrices": [{"instrumentUid": "share-uid", "price": {}}]}]:
            with self.subTest(response=response), self.assertRaises(ApiError):
                TInvestClient(SECRET, transport=RecordingTransport(response)).get_last_price("share-uid")

    def test_client_inputs_are_validated_before_network(self):
        for kwargs in [{"token": ""}, {"token": "Bearer token"}, {"token": "token\n"},
                       {"token": SECRET, "timeout": 0}, {"token": SECRET, "timeout": float("inf")}, {"token": SECRET, "timeout": True}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                TInvestClient(**kwargs)
        transport = RecordingTransport()
        client = TInvestClient(SECRET, transport=transport)
        for lots, direction, order_id in [(True, "buy", ORDER_ID), (0, "buy", ORDER_ID), (1.0, "buy", ORDER_ID),
                                          (1, "hold", ORDER_ID), (1, "buy", "invalid-uuid")]:
            with self.subTest(lots=lots, direction=direction, order_id=order_id), self.assertRaises(ValueError):
                client.post_sandbox_order("account", "share-uid", lots, direction, order_id)
        for amount in [Decimal(0), Decimal(-1), Decimal("0.0000000001")]:
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                client.sandbox_pay_in("account", amount)
        self.assertEqual(transport.requests, [])

    def test_omitted_repeated_fields_are_empty_protobuf_lists(self):
        client = TInvestClient(SECRET, transport=RecordingTransport({}, {}))
        self.assertEqual(client.list_sandbox_accounts(), [])
        self.assertEqual(client.get_sandbox_orders("account"), [])

    def test_malformed_collection_response_rejected(self):
        for response in [{"accounts": [{}]}, {"accounts": "bad"}, {"accounts": ["bad"]}]:
            with self.subTest(response=response), self.assertRaises(ApiError):
                TInvestClient(SECRET, transport=RecordingTransport(response)).list_sandbox_accounts()


class CandleTests(unittest.TestCase):
    def test_daily_request_works_with_gateway_without_optional_limit(self):
        requests = []

        def gateway(request, timeout):
            body = json.loads(request.data)
            requests.append(body)
            if 'limit' in body:
                raise urllib.error.HTTPError(request.full_url, 400, 'Invalid argument', {},
                    io.BytesIO(b'{"code":3,"message":"Unknown field limit"}'))
            return b'{"candles":[]}'

        client = TInvestClient(SECRET, transport=gateway)
        self.assertEqual(client.get_daily_candles('share-uid', datetime(2026, 1, 1, tzinfo=UTC),
                                                datetime(2026, 2, 1, tzinfo=UTC)), [])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['candleSourceType'], 'CANDLE_SOURCE_EXCHANGE')

    def test_pages_are_bounded_sorted_deduplicated_and_complete_utc(self):
        start = datetime(2020, 1, 1, tzinfo=UTC)
        boundary = start + timedelta(days=365)
        end = boundary + timedelta(days=3)
        transport = RecordingTransport({"candles": [
            bar(boundary.isoformat()), bar("2020-01-03T00:00:00Z"), bar("2020-01-02T03:00:00+03:00"),
            bar("2020-01-04T00:00:00Z", complete=False), bar("2019-12-31T00:00:00Z"),
        ]}, {"candles": [bar(boundary.isoformat()), bar((boundary + timedelta(days=1)).isoformat()),
                          bar(end.isoformat()), bar((boundary + timedelta(days=2)).isoformat(), complete=False)]})
        candles = TInvestClient(SECRET, transport=transport).get_daily_candles("share-uid", start, end)
        self.assertEqual([c.time for c in candles], [datetime(2020, 1, 2, tzinfo=UTC), datetime(2020, 1, 3, tzinfo=UTC),
                                                   boundary, boundary + timedelta(days=1)])
        self.assertTrue(all(c.time.tzinfo == UTC for c in candles))
        self.assertEqual(len(transport.requests), 2)
        bodies = [json.loads(req.data) for req, _ in transport.requests]
        self.assertEqual(bodies[0]["from"], "2020-01-01T00:00:00Z")
        self.assertEqual(bodies[0]["to"], bodies[1]["from"])
        self.assertEqual(bodies[1]["to"], end.isoformat().replace("+00:00", "Z"))
        self.assertEqual(bodies[0]["interval"], "CANDLE_INTERVAL_DAY")
        self.assertEqual(bodies[0]["candleSourceType"], "CANDLE_SOURCE_EXCHANGE")
        self.assertEqual(bodies[0]["instrumentId"], "share-uid")

    def test_missing_complete_flag_is_not_accepted(self):
        item = bar("2020-01-01T00:00:00Z")
        del item["isComplete"]
        client = TInvestClient(SECRET, transport=RecordingTransport({"candles": [item]}))
        self.assertEqual(client.get_daily_candles("share-uid", datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 1, 2, tzinfo=UTC)), [])

    def test_malformed_complete_candle_and_conflicting_duplicates_fail(self):
        bad_bars = [bar("2020-01-01"), bar("2020-01-01T00:00:00Z", close="0"),
                    bar("2020-01-01T00:00:00Z", volume="-1"), bar("2020-01-01T00:00:00Z", complete="true")]
        for item in bad_bars:
            with self.subTest(item=item), self.assertRaises(ApiError):
                TInvestClient(SECRET, transport=RecordingTransport({"candles": [item]})).get_daily_candles(
                    "share-uid", datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 1, 2, tzinfo=UTC))
        with self.assertRaises(ApiError):
            TInvestClient(SECRET, transport=RecordingTransport({"candles": [bar("2020-01-01T00:00:00Z"),
                bar("2020-01-01T00:00:00Z", close="106")]})).get_daily_candles(
                "share-uid", datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 1, 2, tzinfo=UTC))

    def test_range_requires_aware_datetimes_in_increasing_order(self):
        transport = RecordingTransport()
        client = TInvestClient(SECRET, transport=transport)
        now = datetime(2020, 1, 1, tzinfo=UTC)
        for start, end in [(datetime(2020, 1, 1), now), (now, now), (now + timedelta(days=1), now)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                client.get_daily_candles("share-uid", start, end)
        self.assertEqual(transport.requests, [])


class ErrorTests(unittest.TestCase):
    def test_only_numeric_broker_error_code_is_exposed(self):
        for code, expected in [('40003', 40003), (SECRET, None), (True, None)]:
            with self.subTest(code=code):
                body = json.dumps({'code': code, 'message': SECRET}).encode()
                error = urllib.error.HTTPError('https://example.test', 400, SECRET, {}, io.BytesIO(body))
                with self.assertRaises(ApiError) as caught:
                    TInvestClient(SECRET, transport=RecordingTransport(error)).list_sandbox_accounts()
                self.assertEqual(caught.exception.broker_code, expected)
                self.assertNotIn(SECRET, str(caught.exception))

    def test_direct_certificate_failure_is_safe_and_distinct(self):
        error = urllib.error.URLError(ssl.SSLCertVerificationError(1, SECRET))
        with self.assertRaises(ApiError) as caught:
            TInvestClient(SECRET, transport=RecordingTransport(error)).list_sandbox_accounts()
        self.assertEqual(caught.exception.reason, 'broker_tls_certificate')
        self.assertNotIn(SECRET, str(caught.exception))

    def test_extra_root_supplements_verified_context_and_keeps_redirect_protection(self):
        contexts = []
        actual_factory = ssl.create_default_context

        def record_context():
            context = actual_factory()
            contexts.append(context)
            return context

        observed = []

        def inspect_opener(*handlers):
            observed.extend(handlers)
            raise OSError('stop before network')

        system_bundle = ssl.get_default_verify_paths().cafile
        self.assertIsNotNone(system_bundle)
        with patch.dict(os.environ, {'TINVEST_CA_FILE': system_bundle}), \
             patch('moex_bot.tbank.ssl.create_default_context', side_effect=record_context), \
             patch('moex_bot.tbank.urllib.request.build_opener', side_effect=inspect_opener):
            with self.assertRaises(OSError):
                _http_transport(urllib.request.Request('https://example.test'), 2)
        self.assertEqual(contexts[0].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(contexts[0].check_hostname)
        self.assertTrue(any(isinstance(handler, urllib.request.HTTPSHandler) for handler in observed))
        self.assertTrue(any(isinstance(handler, urllib.request.HTTPRedirectHandler) and
                            handler.redirect_request(None, None, 302, '', {}, 'https://elsewhere.test') is None
                            for handler in observed))

    def test_proxy_tls_error_is_distinguished_without_leaking_payload(self):
        body = b'upstream connect error CERTIFICATE_VERIFY_FAILED ' + SECRET.encode()
        error = urllib.error.HTTPError('https://example.test', 503, 'Unavailable', {}, io.BytesIO(body))
        transport = RecordingTransport(error)
        with self.assertRaises(ApiError) as caught:
            TInvestClient(SECRET, transport=transport).list_sandbox_accounts()
        self.assertEqual(caught.exception.reason, 'proxy_tls_certificate')
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(len(transport.requests), 1)

    def test_default_http_transport_does_not_forward_token_on_redirect(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "/received")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/redirect", data=b"{}",
                                             headers={"Authorization": "Bearer " + SECRET}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                _http_transport(request, 2)
            self.assertEqual(caught.exception.code, 302)
            self.assertEqual(received, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_errors_do_not_expose_token_or_payload_and_are_not_retried(self):
        error = urllib.error.HTTPError("https://example.test", 503, SECRET, {}, io.BytesIO(SECRET.encode()))
        transport = RecordingTransport(error)
        with self.assertRaises(ApiError) as caught:
            TInvestClient(SECRET, transport=transport).post_sandbox_order("account", "share-uid", 1, "SELL", ORDER_ID)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(len(transport.requests), 1)

    def test_timeout_is_an_uncertain_outcome_with_no_retry(self):
        transport = RecordingTransport(TimeoutError(SECRET))
        with self.assertRaises(ApiError) as caught:
            TInvestClient(SECRET, transport=transport).post_sandbox_order("account", "share-uid", 1, "BUY", ORDER_ID)
        self.assertIn("unknown", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(len(transport.requests), 1)

    def test_malformed_json_and_grpc_error_payloads_are_safe(self):
        for payload in [b"not JSON " + SECRET.encode(), b"[]", b"null", b'{"accounts":[],"accounts":[{}]}',
                        b'{"code":NaN}', {"code": 7, "message": SECRET}]:
            with self.subTest(payload=payload), self.assertRaises(ApiError) as caught:
                TInvestClient(SECRET, transport=RecordingTransport(payload)).list_sandbox_accounts()
            self.assertNotIn(SECRET, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
