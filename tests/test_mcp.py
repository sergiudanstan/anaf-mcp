import os
import sqlite3
import tempfile
import unittest

import anaf_mcp
import remote_mcp


class MCPProtocolTests(unittest.TestCase):
    def test_initialize_negotiates_current_protocol(self):
        response = anaf_mcp.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        })
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("read-only", response["result"]["instructions"])

    def test_tools_are_marked_read_only(self):
        response = anaf_mcp.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
        })
        tools = response["result"]["tools"]
        self.assertEqual(len(tools), 8)
        self.assertTrue(all(tool["annotations"]["readOnlyHint"] for tool in tools))
        self.assertTrue(all(not tool["annotations"]["destructiveHint"] for tool in tools))

    def test_notification_has_no_jsonrpc_response(self):
        self.assertIsNone(anaf_mcp.handle({
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {}
        }))

    def test_remote_index_detection(self):
        self.assertTrue(remote_mcp._needs_remote_index({
            "method": "tools/call", "params": {"name": "search_company"}
        }))
        self.assertFalse(remote_mcp._needs_remote_index({
            "method": "tools/call", "params": {"name": "anaf_firma"}
        }))


class CompactIndexTests(unittest.TestCase):
    def test_company_search_works_with_compact_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "compact.sqlite")
            con = sqlite3.connect(path)
            con.execute(
                "CREATE TABLE firme (nume_norm TEXT NOT NULL, cui INTEGER NOT NULL, "
                "PRIMARY KEY(nume_norm, cui)) WITHOUT ROWID"
            )
            con.execute("INSERT INTO firme VALUES ('arobs transilvania software s a', 11291045)")
            con.execute("CREATE TABLE meta (cheie TEXT, valoare TEXT)")
            con.execute("INSERT INTO meta VALUES ('firme', '1')")
            con.commit()
            con.close()

            previous = os.environ.get("ANAF_MCP_INDEX_DB")
            os.environ["ANAF_MCP_INDEX_DB"] = path
            try:
                result = anaf_mcp.tool_search_company({"nume": "AROBS", "limita": 5})
            finally:
                if previous is None:
                    os.environ.pop("ANAF_MCP_INDEX_DB", None)
                else:
                    os.environ["ANAF_MCP_INDEX_DB"] = previous

        self.assertEqual(result["gasite"], 1)
        self.assertEqual(result["rezultate"][0]["cui"], 11291045)
        self.assertEqual(result["rezultate"][0]["denumire"], "AROBS TRANSILVANIA SOFTWARE S A")


if __name__ == "__main__":
    unittest.main()
