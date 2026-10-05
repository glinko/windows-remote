import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge import StudioBridge


class SimulatedTransport(StudioBridge):
    def __init__(self, response):
        super().__init__({}, r"C:\Users\Test\WindowsRemote\roblox")
        self.data = gzip.compress(json.dumps(response).encode())
        self.upload = bytearray()
        self.commands = []
        self.deleted = False
        self.corrupt = False

    def powershell(self, command, timeout=115):
        self.commands.append(command)
        if "FromBase64String" in command:
            encoded = re.search(r"FromBase64String\('([^']+)'\)", command).group(1)
            self.upload.extend(base64.b64decode(encoded))
            return ""
        if command.startswith("& node"):
            return json.dumps({"id": "a" * 32, "bytes": len(self.data), "sha256": "0" * 64 if self.corrupt else hashlib.sha256(self.data).hexdigest()})
        if "OpenRead" in command:
            offset = int(re.search(r"Seek\((\d+)", command).group(1))
            length = int(re.search(r"byte\[\] (\d+)", command).group(1))
            return base64.b64encode(self.data[offset:offset+length]).decode()
        if "Delete" in command:
            self.deleted = True
            return ""
        raise AssertionError(command)


class BridgeTests(unittest.TestCase):
    def test_large_response_round_trip_and_cleanup(self):
        result = {"jsonrpc": "2.0", "id": "bridge-call", "result": {"content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(os.urandom(150000)).decode()}]}}
        bridge = SimulatedTransport(result)
        response = bridge.call({"jsonrpc": "2.0", "id": 123, "method": "tools/call"})
        self.assertEqual(response["id"], 123)
        self.assertEqual(response["result"], result["result"])
        self.assertGreater(sum("OpenRead" in c for c in bridge.commands), 1)
        self.assertTrue(bridge.deleted)

    def test_large_request_chunked_without_shell_interpolation(self):
        request = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"code": base64.b64encode(os.urandom(40000)).decode() + "'`$(Write-Output test)"}}
        bridge = SimulatedTransport({"jsonrpc": "2.0", "id": 4, "result": {}})
        bridge.call(request)
        self.assertEqual(json.loads(gzip.decompress(bridge.upload)), request)
        self.assertTrue(all(len(c) < 7900 for c in bridge.commands))
        self.assertTrue(any("--file" in c for c in bridge.commands))
        self.assertFalse(any("Write-Output test" in c for c in bridge.commands))

    def test_integrity_failure_still_cleans_up(self):
        bridge = SimulatedTransport({"result": {}})
        bridge.corrupt = True
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            bridge.call({"jsonrpc": "2.0", "id": 5, "method": "ping"})
        self.assertTrue(bridge.deleted)

    def test_error_response_preserves_request_id(self):
        bridge = SimulatedTransport({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "Studio closed"}})
        response = bridge.call({"jsonrpc": "2.0", "id": "test", "method": "tools/list"})
        self.assertEqual(response["id"], "test")
        self.assertEqual(response["error"]["message"], "Studio closed")


if __name__ == "__main__":
    unittest.main()
