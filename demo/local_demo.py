"""Docker-free demo: run the monitor against a local HTTP server you stop and start.

    python demo/local_demo.py

Everything is local (127.0.0.1), dry-run, stdout only.
"""

from __future__ import annotations

import http.server
import pathlib
import tempfile
import threading

from homelab_monitor import cli

PORT = 18089
CONFIG = """
state_dir: {state}
flap_threshold: 2
notifiers: [{{type: stdout}}]
hosts:
  - name: demo-target
    label: Demo target
    checks:
      - {{type: http, id: web, name: target web service, url: "http://127.0.0.1:{port}/", timeout: 2, urgent: true}}
"""


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def start() -> http.server.HTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = pathlib.Path(tmp) / "c.yaml"
        cfg.write_text(CONFIG.format(state=pathlib.Path(tmp) / "state", port=PORT))

        def run(*argv: str) -> int:
            return cli.main(["-c", str(cfg), *argv])

        srv = start()
        print("== 1. service up: baseline (silent)")
        run("check", "all", "--dry-run")
        run("gate")
        srv.shutdown()
        srv.server_close()
        print("== 2. service stopped: first sighting (anti-flap, silent)")
        run("check", "all", "--dry-run")
        print("== 3. second sighting: notify")
        run("check", "all", "--dry-run")
        print("== 4. gate: issue set changed -> agent would wake with this diff")
        run("gate")
        print("== 5. gate again: unchanged -> empty, no LLM call")
        run("gate")
        print("== 6. agent claims the fix (at most once)")
        print("   exit", run("claim", "demo-target:web", "restart service"))
        print("   exit", run("claim", "demo-target:web", "restart service"))
        srv = start()
        print("== 7. service back: recovered")
        run("check", "all", "--dry-run")
        run("gate")
        print("== 8. agent report")
        run("report", "demo-target", "Cause: service stopped / Action: restarted once / Result: healthy", "--dry-run")
        srv.shutdown()


if __name__ == "__main__":
    main()
