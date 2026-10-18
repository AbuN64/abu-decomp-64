import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from abu_decomp.cli import main as public_main
from abu_decomp.stream.cli import capture, status


class Profile:
    def __init__(self, root):
        self.root = root

    def section(self, name):
        assert name == "stream"
        return {
            "capture_root": "captures",
            "capture_image_command": ["/bin/sh", "-c", 'printf real-capture > "$1"', "_", "{output}"],
            "capture_video_command": ["/bin/sh", "-c", 'printf real-video > "$1"', "_", "{output}"],
            "runtime_dir": "runtime",
            "wayland_display": "wayland-test",
            "endpoints": [{"name": "unbound", "host": "127.0.0.1", "port": 1}],
        }

    def resolve_path(self, value):
        return self.root / value


class StreamTests(unittest.TestCase):
    def test_capture_command_and_endpoint_status_are_profile_driven(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Profile(Path(temporary))
            result = capture(profile, "image")
            assert Path(result["path"]).read_text() == "real-capture"
            assert len(result["sha256"]) == 64
            state = status(profile)
            assert not state["wayland_socket_present"]
            assert state["endpoints"][0]["active"] is False

    def test_public_capture_and_publish_emit_one_json_envelope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            profile_path = root / "profile.json"
            section = {
                "runtime_dir": str(root / "runtime"),
                "wayland_display": "wayland-test",
                "services": {},
                "capture_root": str(root / "captures"),
                "capture_image_command": [
                    "/bin/sh",
                    "-c",
                    "printf image > $1; printf capture-completed",
                    "_",
                    "{output}",
                ],
                "capture_video_command": ["/bin/false"],
                "publish_command": ["/bin/sh", "-c", "printf publish-record"],
            }
            profile_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "stream-test",
                        "stream": section,
                    }
                )
            )
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = public_main(["--profile", str(profile_path), "stream", "capture", "image", "--publish"])
            self.assertEqual(code, 0)
            envelope = json.loads(out.getvalue())
            self.assertTrue(envelope["ok"])
            self.assertIn("capture-completed", err.getvalue())
            self.assertIn("publish-record", err.getvalue())
            self.assertEqual(len(out.getvalue().splitlines()), 1)
            section["capture_image_command"] = ["/bin/sh", "-c", "printf failed-child; exit 7"]
            profile_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "stream-test",
                        "stream": section,
                    }
                )
            )
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = public_main(["--profile", str(profile_path), "stream", "capture", "image"])
            self.assertEqual(code, 1)
            failure = json.loads(out.getvalue())
            self.assertFalse(failure["ok"])
            self.assertEqual(failure["findings"][0]["key"], "stream.capture_failed")
            self.assertIn("failed-child", err.getvalue())
