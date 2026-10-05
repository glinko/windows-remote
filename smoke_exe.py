"""Check the shipped binary against a real loopback command server."""
import json
from pathlib import Path
import subprocess
import tempfile
import threading

import remote_agent as agent


def main():
    exe = Path(__file__).parent / "dist" / "WindowsRemote.exe"
    with tempfile.TemporaryDirectory() as folder:
        runner = agent.CommandRunner(folder)
        server = agent.RemoteServer(("127.0.0.1", 0), "x" * 43, runner)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            path = Path(folder) / "bridge.json"
            agent.save_config(path, {"url": f"http://127.0.0.1:{server.server_port}", "token": "x" * 43})
            check = subprocess.run([str(exe), "--check", "--config", str(path)], capture_output=True, encoding="utf-8", timeout=30)
            assert check.returncode == 0, check.stderr
            assert json.loads(check.stdout)["platform"] == "win32"
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "remote_powershell", "arguments": {"command": "Write-Output 'EXE проверен'"}}},
            ]
            result = subprocess.run([str(exe), "--mcp", "--config", str(path)],
                                    input="".join(json.dumps(m) + "\n" for m in messages), capture_output=True, encoding="utf-8", timeout=30)
            assert result.returncode == 0, result.stderr
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            assert len(replies) == 3
            assert len(replies[1]["result"]["tools"]) == 2
            assert not replies[-1]["result"]["isError"], replies[-1]
            assert "EXE проверен" in replies[-1]["result"]["content"][0]["text"]
            print("EXE smoke: authenticated health + MCP initialization/list/PowerShell Unicode OK")
        finally:
            server.stop()
            worker.join(2)


if __name__ == "__main__":
    main()
