#!/usr/bin/env python3
##################################################################################################################################
# Module: test_proxy_lifecycle.py (unit tests — mavlink_proxy lifecycle)                                                         #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Proxy stop-file and watchdog behavior tests.                                                                      #
##################################################################################################################################

"""Local tests for MAVLink proxy graceful shutdown (no cluster)."""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SITL_LIB = Path(__file__).resolve().parents[2] / "03_sitl" / "lib"
sys.path.insert(0, str(SITL_LIB))

from mavlink_proxy import MavlinkProxy  # noqa: E402


def _dummy_sitl_server(port: int, stop: threading.Event) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    srv.settimeout(0.5)
    while not stop.is_set():
        try:
            client, _ = srv.accept()
        except socket.timeout:
            continue
        client.settimeout(0.5)
        while not stop.is_set():
            try:
                data = client.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            try:
                client.sendall(data)
            except OSError:
                break
        client.close()
    srv.close()


class TestProxyLifecycle(unittest.TestCase):
    def _run_proxy(self, out: Path, watchdog: float, stop_after: float | None) -> dict:
        sitl_port = 15961
        stop = threading.Event()
        t_sitl = threading.Thread(target=_dummy_sitl_server, args=(sitl_port, stop), daemon=True)
        t_sitl.start()
        time.sleep(0.2)
        stop_file = out / "STOP"
        proxy = MavlinkProxy(
            "127.0.0.1", 15760, "127.0.0.1", sitl_port, out,
            watchdog_s=watchdog, stop_file=stop_file,
        )

        def client_send() -> None:
            time.sleep(0.3)
            c = socket.create_connection(("127.0.0.1", 15760), timeout=5)
            c.sendall(b"\xfd\x04\x00\x00\x01\x01\x00\x00\x00")
            time.sleep(0.2)
            c.close()

        t_client = threading.Thread(target=client_send, daemon=True)
        t_client.start()

        def touch_stop() -> None:
            if stop_after is None:
                return
            time.sleep(stop_after)
            stop_file.write_text("", encoding="utf-8")

        if stop_after is not None:
            threading.Thread(target=touch_stop, daemon=True).start()

        t0 = time.monotonic()
        summary = proxy.serve()
        elapsed = time.monotonic() - t0
        stop.set()
        t_sitl.join(timeout=2)
        summary["_elapsed_s"] = elapsed
        return summary

    def test_stop_file_shuts_down_quickly(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            summary = self._run_proxy(out, watchdog=120.0, stop_after=0.8)
            self.assertLess(summary["_elapsed_s"], 10.0)
            self.assertIn(summary["termination_reason"], ("stop_file", "client_closed"))
            self.assertTrue((out / "proxy_summary.json").is_file())
            self.assertTrue((out / "proxy_forward.csv").is_file())
            self.assertTrue((out / "proxy_reverse.csv").is_file())

    def test_watchdog_is_fallback_not_normal_path(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            summary = self._run_proxy(out, watchdog=1.5, stop_after=None)
            self.assertLess(summary["_elapsed_s"], 5.0)
            self.assertEqual(summary["termination_reason"], "watchdog")

    def test_summary_fields_present(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            summary = self._run_proxy(out, watchdog=60.0, stop_after=0.5)
            for key in (
                "forward_frames", "reverse_frames", "parse_errors", "reconnects",
                "frames_discarded", "proxy_delay_ms", "parse_queue_drops",
                "termination_reason", "watchdog_s", "actual_runtime_s",
                "graceful_shutdown", "forward_csv_rows", "reverse_csv_rows",
            ):
                self.assertIn(key, summary)
            ps = json.loads((out / "proxy_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(ps["schema_version"], "proxy_v3")


if __name__ == "__main__":
    unittest.main()
