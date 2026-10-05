"""Windows Remote: visible command server + dependency-free MCP stdio bridge."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

VERSION = "1.0.0"
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WindowsRemote"
LIMIT = 65536


def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_config(path, config):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def kill_tree(proc):
    if proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if proc.poll() is None:
            proc.kill()


class CommandRunner:
    def __init__(self, cwd, on_event=lambda event: None):
        self.cwd = str(Path(cwd).resolve(strict=True))
        self.on_event = on_event
        self.stopping = threading.Event()
        self.lock = threading.Lock()

    def execute(self, args):
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        command = args.get("command")
        timeout = args.get("timeout_seconds", 30)
        if not isinstance(command, str) or not command.strip() or len(command) > 8000:
            raise ValueError("command must contain 1..8000 characters")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        cwd = args.get("cwd", self.cwd)
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ValueError("cwd must be an existing absolute directory on the remote machine")
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("Another command is running; retry after it finishes")
        try:
            if self.stopping.is_set():
                raise RuntimeError("Server is stopping")
            # Each request uses a fresh PowerShell. Never expose the access key to it.
            env = {k: v for k, v in os.environ.items() if k.upper() != "WINREMOTE_TOKEN"}
            prefix = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); $OutputEncoding = [Console]::OutputEncoding;\n"
            encoded = base64.b64encode((prefix + command).encode("utf-16-le")).decode("ascii")
            if len(encoded) > 30000:
                raise ValueError("Encoded command exceeds Windows command-line limit; use a .ps1 file")
            shell = shutil.which("powershell.exe")
            if not shell:
                raise RuntimeError("Windows PowerShell not found")
            started = time.monotonic()
            proc = subprocess.Popen([shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-EncodedCommand", encoded],
                                    cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            buffers = [bytearray(), bytearray()]
            truncated = [False, False]

            def drain(stream, index):
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    remaining = LIMIT - len(buffers[index])
                    buffers[index].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated[index] = True
                stream.close()

            readers = [threading.Thread(target=drain, args=(stream, i), daemon=True)
                       for i, stream in enumerate((proc.stdout, proc.stderr))]
            for reader in readers:
                reader.start()
            timed_out = cancelled = False
            self.on_event({"event": "command_started", "pid": proc.pid,
                           "command_sha256": hashlib.sha256(command.encode()).hexdigest()})
            try:
                while proc.poll() is None:
                    cancelled = self.stopping.is_set()
                    timed_out = time.monotonic() - started >= timeout
                    if cancelled or timed_out:
                        kill_tree(proc)
                        break
                    time.sleep(0.05)
                proc.wait(timeout=10)
            finally:
                kill_tree(proc)
            for reader in readers:
                reader.join(timeout=1)
            result = {"stdout": bytes(buffers[0]).decode("utf-8", "replace"),
                      "stderr": bytes(buffers[1]).decode("utf-8", "replace"),
                      "exit_code": proc.returncode, "timed_out": timed_out,
                      "cancelled": cancelled, "truncated": any(truncated),
                      "output_incomplete": any(t.is_alive() for t in readers),
                      "duration_seconds": round(time.monotonic() - started, 3), "cwd": cwd}
            self.on_event({"event": "command_finished", "exit_code": proc.returncode,
                           "timed_out": timed_out, "cancelled": cancelled})
            return result
        finally:
            self.lock.release()


class RemoteServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, token, runner):
        if not isinstance(token, str) or len(token) < 32 or not token.isascii():
            raise ValueError("Access key must be at least 32 ASCII characters")
        self.token = token
        self.runner = runner
        super().__init__(address, Handler)

    def stop(self):
        self.runner.stopping.set()
        self.shutdown()
        # Give the active command time to terminate its process tree.
        with self.runner.lock:
            pass
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass  # Do not log request headers, tokens or command text.

    def reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        # Browser-origin requests are never command clients.
        if self.headers.get("Origin") is not None:
            self.reply(403, {"error": "Browser requests are disabled"})
            return False
        received = self.headers.get("Authorization", "").encode("utf-8")
        expected = ("Bearer " + self.server.token).encode("utf-8")
        if not hmac.compare_digest(received, expected):
            self.reply(401, {"error": "Invalid access key"})
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        if self.path != "/health":
            self.reply(404, {"error": "Unknown endpoint"})
            return
        self.reply(200, {"name": socket.gethostname(), "version": VERSION,
                         "platform": sys.platform, "cwd": self.server.runner.cwd})

    def do_POST(self):
        if not self.authorized():
            return
        if self.path != "/execute":
            self.reply(404, {"error": "Unknown endpoint"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 262144 or self.headers.get("Transfer-Encoding"):
                raise ValueError("Invalid request size")
            args = json.loads(self.rfile.read(size))
            self.reply(200, self.server.runner.execute(args))
        except (ValueError, TypeError) as exc:
            self.reply(400, {"error": str(exc)})
        except RuntimeError as exc:
            self.reply(409, {"error": str(exc)})
        except (OSError, subprocess.SubprocessError) as exc:
            self.reply(500, {"error": str(exc)})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Redirect refused: check server URL")


def remote_request(config, path, args=None):
    url = config["url"].rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("URL must be http(s)://hostname:port, without path or credentials")
    body = None if args is None else json.dumps(args).encode("utf-8")
    request = Request(url + path, data=body, headers={
        "Authorization": "Bearer " + config["token"], "Content-Type": "application/json"})
    # Do not leak credentials to a system HTTP proxy or through redirects.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=140) as response:
            return json.loads(response.read(1048576))
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: " + exc.read(4096).decode("utf-8", "replace")) from None


TOOLS = [
    {"name": "remote_info", "description": "Check connectivity and identify the remote Windows computer.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "remote_powershell", "description": "Execute PowerShell on the remote Windows PC as the user running Windows Remote. Each call starts a fresh shell. Use absolute remote paths. Can read/write files and run programs. Maximum 120 seconds, 64 KiB per output stream. Long-running services should be started separately with Start-Process -WindowStyle Hidden.",
     "inputSchema": {"type": "object", "properties": {
         "command": {"type": "string", "minLength": 1, "maxLength": 8000},
         "cwd": {"type": "string", "description": "Existing absolute directory on the remote PC"},
         "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 120, "default": 30}},
         "required": ["command"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}}
]


def mcp_dispatch(message, config):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    if "id" not in message:
        return None
    reply = {"jsonrpc": "2.0", "id": message["id"]}
    method = message["method"]
    if method == "initialize":
        reply["result"] = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "windows-remote", "version": VERSION}}
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        reply["result"] = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            params = message.get("params", {})
            name = params.get("name")
            if name == "remote_info":
                result = remote_request(config, "/health")
            elif name == "remote_powershell":
                result = remote_request(config, "/execute", params.get("arguments", {}))
            else:
                raise ValueError("Unknown tool")
            failed = bool(result.get("timed_out") or result.get("cancelled") or result.get("exit_code", 0) != 0)
            reply["result"] = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": failed}
        except Exception as exc:
            reply["result"] = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    else:
        reply["error"] = {"code": -32601, "message": "Method not found"}
    return reply


def run_mcp(config):
    # MCP stdio is newline-delimited JSON; stdout contains protocol messages only.
    for raw in sys.stdin.buffer:
        try:
            message = json.loads(raw)
            response = mcp_dispatch(message, config)
        except (ValueError, TypeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()


def codex_snippet(config_path):
    if getattr(sys, "frozen", False):
        command, args = sys.executable, ["--mcp", "--config", str(config_path)]
    else:
        command, args = sys.executable, [str(Path(__file__).resolve()), "--mcp", "--config", str(config_path)]
    return "[mcp_servers.windows_remote]\ncommand = " + json.dumps(command) + "\nargs = " + json.dumps(args) + "\ntool_timeout_sec = 150\n"


def gui():
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    import queue
    events = queue.Queue()
    root = tk.Tk()
    root.title("Windows Remote — доступ для Codex")
    root.geometry("760x650")
    root.minsize(700, 600)
    tabs = ttk.Notebook(root)
    tabs.pack(fill="both", expand=True, padx=12, pady=12)
    server_tab, client_tab = ttk.Frame(tabs, padding=14), ttk.Frame(tabs, padding=14)
    tabs.add(server_tab, text="1. Удалённая Windows")
    tabs.add(client_tab, text="2. Компьютер с Codex")
    state = {"server": None, "closing": False, "stopping": False}
    config_path = DATA / "server.json"
    config = load_config(config_path) if config_path.exists() else {"token": secrets.token_urlsafe(32), "host": "0.0.0.0", "port": 8765, "cwd": str(Path.home())}

    def field(parent, label, value, secret=False):
        ttk.Label(parent, text=label).pack(anchor="w", pady=(10, 3))
        variable = tk.StringVar(value=str(value))
        entry = ttk.Entry(parent, textvariable=variable, show="*" if secret else "")
        entry.pack(fill="x")
        return variable, entry

    ttk.Label(server_tab, text="Запустите на машине, которой нужно управлять.", font=("Segoe UI", 12)).pack(anchor="w")
    ttk.Label(server_tab, text="Команды выполняются с правами текущего пользователя.\nHTTP используйте внутри зашифрованного VPN; не открывайте порт в интернет.", wraplength=680).pack(anchor="w", pady=8)
    host, host_entry = field(server_tab, "Слушать адрес (0.0.0.0 — все IPv4 интерфейсы)", config["host"])
    port, port_entry = field(server_tab, "Порт", config["port"])
    cwd, cwd_entry = field(server_tab, "Начальная рабочая папка (не ограничивает доступ к другим папкам)", config["cwd"])
    ttk.Button(server_tab, text="Выбрать папку", command=lambda: cwd.set(filedialog.askdirectory() or cwd.get())).pack(anchor="w", pady=3)
    token, token_entry = field(server_tab, "Ключ доступа", config["token"], True)

    def copy_key():
        root.clipboard_clear()
        root.clipboard_append(token.get())

    ttk.Button(server_tab, text="Копировать ключ", command=copy_key).pack(anchor="w", pady=5)
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)})
    ttk.Label(server_tab, text="IPv4 этой машины: " + ", ".join(addresses)).pack(anchor="w", pady=5)
    status = tk.StringVar(value="Остановлен. Доступ закрыт.")
    ttk.Label(server_tab, textvariable=status).pack(anchor="w", pady=8)
    buttons = ttk.Frame(server_tab)
    buttons.pack(anchor="w")
    editable = (host_entry, port_entry, cwd_entry, token_entry)

    def on_event(event):
        event = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), **event}
        events.put(("log", json.dumps(event, ensure_ascii=False)))
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            log_path = DATA / "audit.jsonl"
            if log_path.exists() and log_path.stat().st_size > 5_000_000:
                log_path.replace(DATA / "audit.previous.jsonl")
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(event) + "\n")
        except OSError:
            events.put(("log", "Не удалось сохранить журнал"))

    def start():
        try:
            values = {"host": host.get(), "port": int(port.get()), "cwd": cwd.get(), "token": token.get()}
            if not 1 <= values["port"] <= 65535:
                raise ValueError("Порт: 1–65535")
            runner = CommandRunner(values["cwd"], on_event)
            server = RemoteServer((values["host"], values["port"]), values["token"], runner)
            try:
                save_config(config_path, values)
            except Exception:
                server.server_close()
                raise
            state["server"] = server
            threading.Thread(target=server.serve_forever, daemon=True).start()
            for entry in editable:
                entry.configure(state="disabled")
            start_button.configure(state="disabled")
            stop_button.configure(state="normal")
            status.set(f"Работает: {values['host']}:{values['port']}. Закройте окно, чтобы отключить доступ.")
            on_event({"event": "server_started"})
        except Exception as exc:
            messagebox.showerror("Запуск не удался", str(exc))

    def stop():
        if state["stopping"]:
            return
        server = state["server"]
        if server is None:
            if state["closing"]:
                root.destroy()
            return
        stop_button.configure(state="disabled")
        state["stopping"] = True
        status.set("Остановка; завершение активной команды…")

        def worker():
            server.stop()
            events.put(("stopped", ""))
        threading.Thread(target=worker, daemon=True).start()

    start_button = ttk.Button(buttons, text="Запустить", command=start)
    start_button.pack(side="left", padx=(0, 10))
    stop_button = ttk.Button(buttons, text="Остановить", command=stop, state="disabled")
    stop_button.pack(side="left")
    log_box = tk.Text(server_tab, height=6, state="disabled", wrap="word")
    log_box.pack(fill="both", expand=True, pady=10)

    bridge_path = DATA / "bridge.json"
    bridge = load_config(bridge_path) if bridge_path.exists() else {"url": "http://192.168.1.100:8765", "token": ""}
    ttk.Label(client_tab, text="Настройте на машине с Codex.", font=("Segoe UI", 12)).pack(anchor="w")
    url, _ = field(client_tab, "Адрес удалённой Windows (IP локальной сети или VPN)", bridge["url"])
    client_token, _ = field(client_tab, "Ключ из окна удалённой машины", bridge["token"], True)
    client_status = tk.StringVar(value="Сначала проверьте соединение, затем сохраните настройки.")
    ttk.Label(client_tab, textvariable=client_status, wraplength=680).pack(anchor="w", pady=12)

    def test_connection():
        values = {"url": url.get(), "token": client_token.get()}
        client_status.set("Проверяю соединение…")

        def worker():
            try:
                result = remote_request(values, "/health")
                events.put(("client", "Подключено: " + result["name"] + " · " + result["cwd"]))
            except Exception as exc:
                events.put(("client", "Ошибка: " + str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    snippet = tk.Text(client_tab, height=9, wrap="word")

    def save_bridge():
        try:
            values = {"url": url.get(), "token": client_token.get()}
            if len(values["token"]) < 32:
                raise ValueError("Скопируйте полный ключ с удалённой машины")
            save_config(bridge_path, values)
            text = codex_snippet(bridge_path)
            snippet.delete("1.0", "end")
            snippet.insert("1.0", text)
            (DATA / "codex-snippet.toml").write_text(text, encoding="utf-8")
            client_status.set("Сохранено. Добавьте блок ниже в конфигурацию MCP Codex и перезапустите Codex.")
        except Exception as exc:
            messagebox.showerror("Не удалось сохранить", str(exc))

    ttk.Button(client_tab, text="Проверить соединение", command=test_connection).pack(anchor="w", pady=5)
    ttk.Button(client_tab, text="Сохранить и показать настройки Codex", command=save_bridge).pack(anchor="w", pady=5)
    ttk.Label(client_tab, text="Блок для %USERPROFILE%\\.codex\\config.toml:").pack(anchor="w", pady=10)
    snippet.pack(fill="both", expand=True)
    ttk.Label(client_tab, text="Ключ хранится локально в %LOCALAPPDATA%\\WindowsRemote.\nПриложение не изменяет конфигурацию Codex автоматически.").pack(anchor="w", pady=10)

    def poll():
        try:
            while True:
                kind, message = events.get_nowait()
                if kind == "log":
                    log_box.configure(state="normal")
                    log_box.insert("end", message + "\n")
                    if int(log_box.index("end-1c").split(".")[0]) > 200:
                        log_box.delete("1.0", "50.0")
                    log_box.see("end")
                    log_box.configure(state="disabled")
                elif kind == "client":
                    client_status.set(message)
                elif kind == "stopped":
                    state["server"] = None
                    state["stopping"] = False
                    on_event({"event": "server_stopped"})
                    if state["closing"]:
                        root.destroy()
                        return
                    for entry in editable:
                        entry.configure(state="normal")
                    start_button.configure(state="normal")
                    status.set("Остановлен. Доступ закрыт.")
        except queue.Empty:
            pass
        root.after(100, poll)

    def close():
        state["closing"] = True
        stop()
    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, poll)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="Windows Remote command server and MCP bridge")
    parser.add_argument("--mcp", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--config", default=str(DATA / "bridge.json"))
    args = parser.parse_args()
    if args.mcp:
        run_mcp(load_config(args.config))
    elif args.check:
        print(json.dumps(remote_request(load_config(args.config), "/health"), ensure_ascii=False))
    else:
        gui()


if __name__ == "__main__":
    main()
