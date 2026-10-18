import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from abu_decomp.stream import transport
from abu_decomp.stream.cli import run
from abu_decomp.stream.up import up


class TransportTests(unittest.TestCase):
    def config(self, root):
        return {
            "transport": "moonlight",
            "moonlight": {"units": ["capture.service", "browser.service"], "systemd_runtime_dir": "/run/user/42"},
            "up": {
                "run_dir": str(root),
                "public_host": "stream.test",
                "http_port": 8080,
                "output": {"name": "HEADLESS-1", "width": 1920, "height": 1080},
            },
        }

    def profile(self, config, root):
        tiles = {"layout": [["a", "b"], ["c", "d"]], "roles": {k: {} for k in "abcd"}}
        return SimpleNamespace(
            section=lambda name: {"stream": config, "tiles": tiles}[name],
            resolve_path=Path,
            source_file=root / "p.json",
        )

    def test_explicit_transport_and_units_are_required(self):
        for config in ({}, {"transport": "vnc"}, {"transport": "moonlight", "moonlight": {"units": ["--all"]}}):
            with self.assertRaises(ValueError):
                transport.settings(config)

    def test_systemd_uses_configured_bus_and_reverse_stop_order(self):
        config = self.config(Path("/unused"))
        with patch("abu_decomp.stream.transport.subprocess.run") as call:
            transport.change(config, "start")
            self.assertEqual(
                call.call_args.args[0], ["systemctl", "--user", "start", "capture.service", "browser.service"]
            )
            self.assertEqual(call.call_args.kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/run/user/42/bus")
            transport.change(config, "stop")
            self.assertEqual(call.call_args.args[0][-2:], ["browser.service", "capture.service"])
            self.assertTrue(call.call_args.kwargs["check"])

    def test_up_attaches_without_recreating_tiles_or_unlinking_sockets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sock = root / "sway-ipc.42.1.sock"
            sock.touch()
            config = self.config(root)
            profile = self.profile(config, root)
            with patch("abu_decomp.stream.up.SwayIPC") as ipc, patch("abu_decomp.stream.transport.subprocess.run"):
                with patch("abu_decomp.stream.up.Group") as group:
                    result = up(profile)
                    group.assert_not_called()
                ipc.return_value.tree.assert_called_once()
            self.assertTrue(sock.exists())
            self.assertEqual(result["url"], "http://stream.test:8080/stream.html")

    def test_down_only_stops_transport_and_propagates_failure(self):
        config = self.config(Path("/unused"))
        profile = SimpleNamespace(section=lambda _: config)
        with patch("abu_decomp.stream.transport.subprocess.run") as call:
            self.assertTrue(run(profile, ("down",))["stopped"])
            call.side_effect = RuntimeError("service failed")
            with self.assertRaisesRegex(RuntimeError, "service failed"):
                run(profile, ("down",))

    def test_stale_socket_fails_without_starting_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sway-ipc.42.1.sock").touch()
            profile = self.profile(self.config(root), root)
            with patch("abu_decomp.stream.up.SwayIPC") as ipc, patch("abu_decomp.stream.transport.change") as change:
                ipc.return_value.tree.side_effect = ConnectionError("stale")
                with self.assertRaises(ConnectionError):
                    up(profile)
                change.assert_not_called()
