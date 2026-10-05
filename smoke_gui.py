"""Exercise the GUI with temporary configuration and a loopback-only listener."""
from pathlib import Path
import socket
import tempfile
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch

import remote_agent as agent


def all_widgets(widget):
    yield widget
    for child in widget.winfo_children():
        yield from all_widgets(child)


def main():
    original_loop = tk.Tk.mainloop
    failures = []
    with tempfile.TemporaryDirectory() as folder:
        agent.DATA = Path(folder)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        agent.save_config(agent.DATA / "server.json", {
            "host": "127.0.0.1", "port": port, "cwd": folder,
            "token": "x" * 43})

        def smoke_loop(root, *args, **kwargs):
            widgets = list(all_widgets(root))
            buttons = {w.cget("text"): w for w in widgets if isinstance(w, ttk.Button)}
            root.report_callback_exception = lambda kind, error, tb: (failures.append(str(error)), root.destroy())

            def begin():
                try:
                    buttons["Запустить"].invoke()
                    assert str(buttons["Запустить"].cget("state")) == "disabled"
                    result = agent.remote_request({"url": f"http://127.0.0.1:{port}", "token": "x" * 43}, "/health")
                    assert result["cwd"] == str(Path(folder).resolve())
                    entries = [w for w in widgets if isinstance(w, ttk.Entry)]
                    entries[-2].delete(0, "end")
                    entries[-2].insert(0, f"http://127.0.0.1:{port}")
                    entries[-1].delete(0, "end")
                    entries[-1].insert(0, "x" * 43)
                    buttons["Сохранить и показать настройки Codex"].invoke()
                    assert (agent.DATA / "bridge.json").exists()
                    assert "[mcp_servers.windows_remote]" in (agent.DATA / "codex-snippet.toml").read_text()
                    buttons["Остановить"].invoke()
                    root.after(1500, finish)
                except Exception as error:
                    failures.append(str(error))
                    root.destroy()

            def finish():
                try:
                    assert str(buttons["Запустить"].cget("state")) == "normal"
                    assert str(buttons["Остановить"].cget("state")) == "disabled"
                except Exception as error:
                    failures.append(str(error))
                root.destroy()

            root.after(100, begin)
            root.after(8000, root.destroy)
            return original_loop(root, *args, **kwargs)

        with patch.object(tk.Tk, "mainloop", smoke_loop):
            agent.gui()
    if failures:
        raise AssertionError(failures)
    print("GUI smoke: start, health, bridge config, stop OK")


if __name__ == "__main__":
    main()
