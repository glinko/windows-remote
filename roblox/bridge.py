"""Expose the remote official Roblox Studio tools as a local stdio MCP server."""
import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import remote_agent


def quote(value):
    return "'" + value.replace("'", "''") + "'"


class StudioBridge:
    def __init__(self, config, remote_folder):
        self.config = config
        self.folder = remote_folder.rstrip("\\/")

    def powershell(self, command, timeout=115):
        result = remote_agent.remote_request(self.config, "/execute", {
            "command": "$ProgressPreference='SilentlyContinue'; " + command,
            "timeout_seconds": timeout})
        if result.get("timed_out") or result.get("cancelled") or result.get("exit_code") != 0 or result.get("truncated"):
            raise RuntimeError("Remote bridge command failed; it was not retried: " + json.dumps(result, ensure_ascii=False))
        return result["stdout"].strip()

    def call(self, message):
        packed = gzip.compress(json.dumps(message, ensure_ascii=False).encode("utf-8"))
        encoded = base64.b64encode(packed).decode("ascii")
        script = quote(self.folder + "\\studio_rpc.cjs")
        if len(encoded) <= 6000:
            command = "& node " + script + " " + quote(encoded)
        else:
            request_id = uuid.uuid4().hex
            request_path = quote(self.folder + "\\" + request_id + ".request.gz")
            for index in range(0, len(encoded), 6000):
                block = encoded[index:index+6000]
                mode = "Create" if index == 0 else "Append"
                self.powershell("$b=[Convert]::FromBase64String(" + quote(block) + "); $f=[IO.File]::Open(" + request_path + ",[IO.FileMode]::" + mode + "); try{$f.Write($b,0,$b.Length)}finally{$f.Dispose()}", 15)
            command = "& node " + script + " --file " + request_id
        manifest = json.loads(self.powershell(command))
        reply_id = manifest.get("id", "")
        if len(reply_id) != 32 or any(c not in "0123456789abcdef" for c in reply_id):
            raise RuntimeError("Invalid remote response ID")
        size = manifest["bytes"]
        if not isinstance(size, int) or not 0 < size <= 32 * 1024 * 1024:
            raise RuntimeError("Remote response exceeds 32 MiB")
        reply_path = quote(self.folder + "\\" + reply_id + ".reply.gz")
        chunks = []
        try:
            for offset in range(0, size, 32768):
                code = "$f=[IO.File]::OpenRead(" + reply_path + "); try{$null=$f.Seek(" + str(offset) + ",[IO.SeekOrigin]::Begin); $b=New-Object byte[] " + str(min(32768, size-offset)) + "; $n=$f.Read($b,0,$b.Length); [Convert]::ToBase64String($b,0,$n)}finally{$f.Dispose()}"
                chunks.append(base64.b64decode(self.powershell(code, 15), validate=True))
            compressed = b"".join(chunks)
            if len(compressed) != size or hashlib.sha256(compressed).hexdigest() != manifest["sha256"]:
                raise RuntimeError("Response checksum mismatch")
            payload = gzip.decompress(compressed)
            if len(payload) > 32 * 1024 * 1024:
                raise RuntimeError("Decompressed response exceeds 32 MiB")
            response = json.loads(payload)
            if "id" in message:
                response["id"] = message["id"]
            return response
        finally:
            try:
                self.powershell("[IO.File]::Delete(" + reply_path + ")", 15)
            except Exception:
                pass  # Abandoned response files expire on the next remote call.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(remote_agent.DATA / "bridge.json"))
    parser.add_argument("--remote-folder", required=True)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    bridge = StudioBridge(remote_agent.load_config(args.config), args.remote_folder)
    if args.probe:
        response = bridge.call({"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"list_roblox_studios", "arguments":{}}})
        print(json.dumps(response, ensure_ascii=False))
        return
    for line in sys.stdin.buffer:
        message = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
                raise ValueError("Invalid JSON-RPC request")
            if "id" not in message:
                # Notifications do not carry state between per-request sessions.
                continue
            response = bridge.call(message)
        except Exception as error:
            response = {"jsonrpc":"2.0", "id":message.get("id") if isinstance(message,dict) else None,
                        "error":{"code":-32000,"message":str(error)}}
        sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False)+"\n").encode("utf-8"))
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
