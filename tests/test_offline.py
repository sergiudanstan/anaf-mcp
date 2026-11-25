"""Offline tests: input handling, CAEN lookup, BNR parsing/conversion, ranking over a synthetic
index, the compact remote index, and the HTTP transport on a loopback port. No network access."""

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import anaf_mcp
import remote_mcp
from tools import build_remote_index


def call(name, arguments):
    response = anaf_mcp.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    result = response["result"]
    return result["isError"], result["content"][0]["text"]


class InputHandlingTests(unittest.TestCase):
    def test_normalize_cui(self):
        self.assertEqual(anaf_mcp.normalize_cui(" RO 14399840 "), 14399840)
        self.assertEqual(anaf_mcp.normalize_cui(14399840), 14399840)
        for bad in ("abc", "", "12;DROP", "-5", None):
            with self.assertRaises(ValueError):
                anaf_mcp.normalize_cui(bad)

    def test_normalize_date(self):
        self.assertEqual(anaf_mcp.normalize_date("15.03.2026"), "2026-03-15")
        self.assertEqual(anaf_mcp.normalize_date("2026-03-15"), "2026-03-15")
        with self.assertRaises(ValueError):
            anaf_mcp.normalize_date("tomorrow")

    def test_limit_is_clamped(self):
        # SQLite treats LIMIT -1 as "no limit"; the helper must never pass that through.
        self.assertEqual(anaf_mcp._limita(-1, 20, 500), 20)
        self.assertEqual(anaf_mcp._limita("x", 20, 500), 20)
        self.assertEqual(anaf_mcp._limita(10 ** 9, 20, 500), 500)

    def test_invalid_arguments_become_tool_errors_without_network(self):
        with mock.patch.object(anaf_mcp, "http_request", side_effect=AssertionError("network used")):
            is_error, text = call("anaf_firma", {"cui": "not-a-cui"})
            self.assertTrue(is_error)
            self.assertIn("CUI invalid", text)
            is_error, text = call("anaf_firme", {"cuiuri": list(range(101))})
            self.assertTrue(is_error)
            self.assertIn("Maximum 100", text)
            is_error, _ = call("bnr_curs", {"data": "not a date"})
            self.assertTrue(is_error)

    def test_unknown_tool_is_a_jsonrpc_error(self):
        response = anaf_mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                    "params": {"name": "rm_rf", "arguments": {}}})
        self.assertEqual(response["error"]["code"], -32601)


class CaenTests(unittest.TestCase):
    def test_lookup_by_code_prefix_and_by_words(self):
        by_code = anaf_mcp.caen_cauta("47")
        self.assertTrue(by_code and all(r["cod"].startswith("47") for r in by_code))
        self.assertTrue(any(r["cod"] == "4754" for r in anaf_mcp.caen_cauta("4754")))
        self.assertTrue(anaf_mcp.caen_cauta("instalatii electrice"))  # diacritics do not matter
        self.assertEqual(anaf_mcp.caen_cauta("zzzzqqqq"), [])


BNR_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<DataSet xmlns="http://www.bnr.ro/xsd">
  <Body><Cube date="2026-06-30">
    <Rate currency="EUR">5.0000</Rate>
    <Rate currency="HUF" multiplier="100">1.2500</Rate>
    <Rate currency="USD">4.0000</Rate>
  </Cube></Body>
</DataSet>"""


class BnrTests(unittest.TestCase):
    def test_parse_applies_multiplier_and_adds_ron(self):
        rates = anaf_mcp._parse_bnr(BNR_XML)["2026-06-30"]
        self.assertEqual(rates["RON"], 1.0)
        self.assertAlmostEqual(rates["HUF"], 0.0125)
        self.assertEqual(rates["EUR"], 5.0)

    def test_conversion_uses_the_rate_table(self):
        rates = anaf_mcp._parse_bnr(BNR_XML)["2026-06-30"]
        with mock.patch.object(anaf_mcp, "curs_la_data", return_value=("2026-06-30", rates)):
            is_error, text = call("bnr_conversie", {"suma": 10, "din": "EUR", "in": "USD", "data": "2026-06-30"})
            self.assertFalse(is_error)
            self.assertEqual(json.loads(text)["rezultat"], 12.5)
            is_error, text = call("bnr_conversie", {"suma": 10, "din": "XXX", "data": "2026-06-30"})
            self.assertTrue(is_error)
            self.assertIn("XXX", text)


def _write_index(path, compact=False):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE firme (cui INTEGER, denumire TEXT, nume_norm TEXT, nr_reg_com TEXT, "
                "data_inmatriculare TEXT, forma_juridica TEXT, judet TEXT, localitate TEXT)")
    con.executemany("INSERT INTO firme (cui, denumire, nume_norm, judet) VALUES (?,?,?,?)", [
        (1, "ALFA SRL", "alfa srl", "CLUJ"),
        (2, "BETA SA", "beta sa", "BRASOV"),
    ])
    con.execute("CREATE TABLE financiar (an INTEGER, cui INTEGER, denumire TEXT, judet TEXT, caen INTEGER, "
                "cifra_afaceri REAL, profit_net REAL, salariati INTEGER)")
    con.executemany("INSERT INTO financiar VALUES (?,?,?,?,?,?,?,?)", [
        (2024, 1, "ALFA SRL", "CLUJ", 4754, 300.0, 30.0, 3),
        (2024, 2, "BETA SA", "BRASOV", 4799, 900.0, -5.0, 9),
        (2024, 3, "GAMA SRL", "CLUJ", 6201, 600.0, 60.0, 6),
        (2023, 4, "OLD SRL", "CLUJ", 4754, 9999.0, 1.0, 1),
    ])
    con.execute("CREATE TABLE meta (cheie TEXT, valoare TEXT)")
    con.executemany("INSERT INTO meta VALUES (?,?)", [("firme", "2"), ("financiar", "4"), ("ani", "2024,2023")])
    con.commit()
    con.close()


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "index.sqlite")
        _write_index(self.db)
        patcher = mock.patch.dict(os.environ, {"ANAF_MCP_INDEX_DB": self.db})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_top_ranks_the_latest_year_by_the_requested_column(self):
        is_error, text = call("top_firme", {"limita": 10})
        out = json.loads(text)
        self.assertFalse(is_error)
        self.assertEqual(out["an"], 2024)
        self.assertEqual([r["denumire"] for r in out["clasament"]], ["BETA SA", "GAMA SRL", "ALFA SRL"])
        out = json.loads(call("top_firme", {"dupa": "profit", "limita": 1})[1])
        self.assertEqual(out["clasament"][0]["denumire"], "GAMA SRL")

    def test_top_filters_by_caen_prefix_county_and_year(self):
        out = json.loads(call("top_firme", {"caen": "47"})[1])
        self.assertEqual([r["denumire"] for r in out["clasament"]], ["BETA SA", "ALFA SRL"])
        out = json.loads(call("top_firme", {"judet": "cluj"})[1])
        self.assertEqual({r["denumire"] for r in out["clasament"]}, {"ALFA SRL", "GAMA SRL"})
        out = json.loads(call("top_firme", {"an": 2023})[1])
        self.assertEqual([r["denumire"] for r in out["clasament"]], ["OLD SRL"])

    def test_search_company_treats_input_as_data_not_sql(self):
        out = json.loads(call("search_company", {"nume": "alfa"})[1])
        self.assertEqual([r["cui"] for r in out["rezultate"]], [1])
        out = json.loads(call("search_company", {"nume": "' OR 1=1 --"})[1])
        self.assertEqual(out["gasite"], 0)
        is_error, _ = call("search_company", {"nume": "a"})
        self.assertTrue(is_error)  # shorter than 2 characters

    def test_compact_remote_index_is_built_unpacked_and_searched(self):
        archive = os.path.join(self.tmp.name, "remote.sqlite.xz")
        build_remote_index.build(self.db, archive, log=lambda *_: None)
        unpacked = os.path.join(self.tmp.name, "unpacked.sqlite")
        env = {k: v for k, v in os.environ.items() if k != "ANAF_MCP_INDEX_DB"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(anaf_mcp, "INDEX_DB", os.path.join(self.tmp.name, "none1")), \
                mock.patch.object(anaf_mcp, "ONRC_DB", os.path.join(self.tmp.name, "none2")), \
                mock.patch.object(remote_mcp, "REMOTE_INDEX_ARCHIVE", archive), \
                mock.patch.object(remote_mcp, "REMOTE_INDEX_DB", unpacked):
            response = remote_mcp._handle_one({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "search_company", "arguments": {"nume": "beta"}},
            })
            out = json.loads(response["result"]["content"][0]["text"])
            self.assertEqual(out["rezultate"][0]["cui"], 2)
            self.assertEqual(out["rezultate"][0]["denumire"], "BETA SA")

    def test_remote_index_decompression_has_a_size_limit(self):
        archive = os.path.join(self.tmp.name, "remote.sqlite.xz")
        build_remote_index.build(self.db, archive, log=lambda *_: None)
        unpacked = os.path.join(self.tmp.name, "unpacked.sqlite")
        env = {k: v for k, v in os.environ.items() if k != "ANAF_MCP_INDEX_DB"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(anaf_mcp, "INDEX_DB", os.path.join(self.tmp.name, "none1")), \
                mock.patch.object(anaf_mcp, "ONRC_DB", os.path.join(self.tmp.name, "none2")), \
                mock.patch.object(remote_mcp, "REMOTE_INDEX_ARCHIVE", archive), \
                mock.patch.object(remote_mcp, "REMOTE_INDEX_DB", unpacked), \
                mock.patch.object(remote_mcp, "REMOTE_INDEX_MAX_BYTES", 1024):
            with self.assertRaises(RuntimeError):
                remote_mcp.ensure_remote_index()
        self.assertFalse(os.path.exists(unpacked))
        self.assertFalse(os.path.exists(unpacked + ".tmp"))


class HttpTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        quiet = mock.patch.object(remote_mcp.RemoteMCPHandler, "log_message", lambda *a, **k: None)
        quiet.start()
        cls.addClassCleanup(quiet.stop)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), remote_mcp.RemoteMCPHandler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, body, path="/mcp", headers=None, raw=False):
        data = body if raw else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as err:
            return err.code, err.read(), err.headers

    def test_health_reports_version_and_no_auth(self):
        with urllib.request.urlopen(self.base + "/health", timeout=10) as resp:
            body = json.load(resp)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["version"], anaf_mcp.SERVER_VERSION)
        self.assertEqual(body["authentication"], "none")

    def test_tools_list_over_http(self):
        status, body, headers = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["result"]["tools"]), 8)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_event_stream_accept_header(self):
        status, body, headers = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                          headers={"Accept": "text/event-stream"})
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))
        self.assertTrue(body.startswith(b"event: message\ndata: "))

    def test_notification_gets_202(self):
        status, _, _ = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(status, 202)

    def test_batch(self):
        status, body, _ = self.post([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                     {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in json.loads(body)], [1, 2])

    def test_malformed_requests_are_rejected(self):
        self.assertEqual(self.post(b"{not json", raw=True)[0], 400)
        self.assertEqual(self.post(b"", raw=True)[0], 400)
        self.assertEqual(self.post([])[0], 400)
        self.assertEqual(self.post(5)[0], 400)
        self.assertEqual(self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, path="/elsewhere")[0], 404)

    def test_oversized_body_is_refused_before_reading(self):
        with mock.patch.object(remote_mcp, "MAX_REQUEST_BYTES", 64):
            status, _, _ = self.post(b"x" * 200, raw=True)
        self.assertEqual(status, 413)

    def test_get_on_the_mcp_endpoint_is_not_allowed(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/mcp", timeout=10)
        self.assertEqual(ctx.exception.code, 405)


class StdioAndReleaseTests(unittest.TestCase):
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_stdio_server_answers_json_rpc_lines(self):
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "search_caen", "arguments": {"q": "4754"}}},
        ]
        proc = subprocess.run(
            [sys.executable, os.path.join(self.ROOT, "anaf_mcp.py")],
            input="".join(json.dumps(m) + "\n" for m in lines),
            capture_output=True, text=True, timeout=30, check=True,
        )
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])  # the notification gets no reply
        self.assertEqual(replies[0]["result"]["serverInfo"]["version"], anaf_mcp.SERVER_VERSION)
        self.assertIn("4754", replies[1]["result"]["content"][0]["text"])

    def test_declared_version_has_a_changelog_section(self):
        with open(os.path.join(self.ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
            changelog = f.read()
        self.assertRegex(anaf_mcp.SERVER_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertIsNotNone(re.search(r"^## \[%s\]" % re.escape(anaf_mcp.SERVER_VERSION), changelog, re.M))


if __name__ == "__main__":
    unittest.main()
