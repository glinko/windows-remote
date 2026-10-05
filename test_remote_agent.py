import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import remote_agent as agent


class RemoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.runner = agent.CommandRunner(cls.temp.name)
        cls.token = secrets.token_urlsafe(32)
        cls.server = agent.RemoteServer(("127.0.0.1", 0), cls.token, cls.runner)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.config = {"url": f"http://127.0.0.1:{cls.server.server_port}", "token": cls.token}

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.thread.join(2)
        cls.temp.cleanup()

    def test_unauthorized_and_browser_requests(self):
        for headers, expected in [({}, 401), ({"Authorization": "Bearer wrong"}, 401),
                                  ({"Authorization": "Bearer " + self.token, "Origin": "http://localhost"}, 403)]:
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(self.config["url"] + "/health", headers=headers), timeout=3)
            self.assertEqual(caught.exception.code, expected)
            caught.exception.close()

    def test_info(self):
        result = agent.remote_request(self.config, "/health")
        self.assertEqual(result["platform"], "win32")
        self.assertEqual(result["cwd"], str(Path(self.temp.name).resolve()))

    def test_command_unicode_and_exit_code(self):
        result = agent.remote_request(self.config, "/execute", {"command": "Write-Output 'Привет'; [Console]::Error.WriteLine('ошибка'); exit 7"})
        self.assertIn("Привет", result["stdout"])
        self.assertIn("ошибка", result["stderr"])
        self.assertEqual(result["exit_code"], 7)

    def test_file_read_write(self):
        result = agent.remote_request(self.config, "/execute", {"command": "Set-Content sample.txt 'hello'; Get-Content sample.txt"})
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("hello", result["stdout"])
        self.assertTrue((Path(self.temp.name) / "sample.txt").exists())

    def test_validation(self):
        for args in ({"command": ""}, {"command": "pwd", "timeout_seconds": 0},
                     {"command": "pwd", "timeout_seconds": True},
                     {"command": "pwd", "timeout_seconds": float("nan")},
                     {"command": "pwd", "cwd": "relative"}, {"command": "a" * 8001}):
            with self.assertRaises(ValueError):
                self.runner.execute(args)

    def test_timeout(self):
        result = agent.remote_request(self.config, "/execute", {"command": "Start-Sleep 20", "timeout_seconds": 1})
        self.assertTrue(result["timed_out"])
        self.assertLess(result["duration_seconds"], 6)

    def test_output_limit(self):
        result = agent.remote_request(self.config, "/execute", {"command": "[Console]::Write('x' * 200000)"})
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["stdout"]), agent.LIMIT)

    def test_concurrency_rejected(self):
        self.runner.lock.acquire()
        try:
            with self.assertRaises(RuntimeError):
                self.runner.execute({"command": "pwd"})
        finally:
            self.runner.lock.release()

    def test_mcp_error_marks_failed_command(self):
        response = agent.mcp_dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "remote_powershell", "arguments": {"command": "exit 9"}}}, self.config)
        self.assertTrue(response["result"]["isError"])

    def test_stdio_end_to_end(self):
        config_path = Path(self.temp.name) / "bridge.json"
        agent.save_config(config_path, self.config)
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "remote_info"}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "remote_powershell", "arguments": {"command": "Write-Output 'MCP OK'"}}},
        ]
        result = subprocess.run([sys.executable, str(Path(agent.__file__).resolve()), "--mcp", "--config", str(config_path)],
                                input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(responses), 4)
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(len(responses[1]["result"]["tools"]), 2)
        self.assertFalse(responses[3]["result"]["isError"])
        self.assertIn("MCP OK", responses[3]["result"]["content"][0]["text"])

    def test_bad_mcp_messages(self):
        self.assertEqual(agent.mcp_dispatch([], self.config)["error"]["code"], -32600)
        self.assertIsNone(agent.mcp_dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.config))
        self.assertEqual(agent.mcp_dispatch({"jsonrpc": "2.0", "id": 1, "method": "nope"}, self.config)["error"]["code"], -32601)

    def test_url_validation(self):
        for url in ("ftp://localhost", "http://user:pass@localhost", "http://localhost/path", "http://localhost?token=foo"):
            with self.assertRaises(ValueError):
                agent.remote_request({"url": url, "token": self.token}, "/health")

    def test_stop_cancels_command_and_closes_port(self):
        runner = agent.CommandRunner(self.temp.name)
        server = agent.RemoteServer(("127.0.0.1", 0), self.token, runner)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        results = []
        command = threading.Thread(target=lambda: results.append(runner.execute({"command": "Start-Sleep 30"})))
        command.start()
        deadline = time.monotonic() + 3
        while not runner.lock.locked() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)
        server.stop()
        command.join(5)
        thread.join(2)
        self.assertFalse(command.is_alive())
        self.assertTrue(results[0]["cancelled"])


if __name__ == "__main__":
    unittest.main()
