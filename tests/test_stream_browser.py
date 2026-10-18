import tempfile
import unittest
from pathlib import Path

from abu_decomp.stream.browser import AUDIO_TRACK_NEW, AUDIO_TRACK_OLD, install
from abu_decomp.stream.up import endpoint_url


class BrowserTests(unittest.TestCase):
    def test_install_replaces_false_audio_success_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            player = Path(directory) / "stream/audio/audio_element.js"
            player.parent.mkdir(parents=True)
            player.write_text(AUDIO_TRACK_OLD)
            page = Path(directory) / "stream.html"
            page.write_text(
                '<body><div id="root"></div><button id="stream-audio">Old</button><script>old()</script></body>'
            )
            config = {"web_root": directory, "bitrate_kbps": 40000, "fps": 60}
            output = {"width": 1920, "height": 1080}
            install(config, output)
            first = page.read_text()
            install(config, output)
            self.assertEqual(page.read_text(), first)
            result = page.read_text()
            self.assertEqual(result.count("<!-- taste-controls -->"), 1)
            self.assertNotIn("old()", result)
            self.assertEqual(player.read_text(), AUDIO_TRACK_NEW)
            self.assertIn("await audio.play()", result)
            self.assertIn('"width": 1920', result)
            self.assertIn('"bitrate": 40000', result)

    def test_bad_facts_do_not_rewrite_page(self):
        with tempfile.TemporaryDirectory() as directory:
            player = Path(directory) / "stream/audio/audio_element.js"
            player.parent.mkdir(parents=True)
            player.write_text(AUDIO_TRACK_OLD)
            page = Path(directory) / "stream.html"
            page.write_text("<body></body>")
            with self.assertRaises(ValueError):
                install({"web_root": directory, "bitrate_kbps": 0, "fps": 60}, {"width": 1920, "height": 1080})
            self.assertEqual(page.read_text(), "<body></body>")

    def test_secure_endpoint_is_explicit(self):
        self.assertEqual(
            endpoint_url({"http_scheme": "https", "public_host": "stream.test", "http_port": 8443}),
            "https://stream.test:8443/stream.html",
        )
