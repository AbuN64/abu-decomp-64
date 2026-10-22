import unittest

from abu_decomp import tas_serve


class FollowTests(unittest.TestCase):
    def test_starting_frame_is_a_jpeg_shown_before_a_new_run_has_a_picture(self):
        frame = tas_serve.STARTING_FRAME
        self.assertTrue(frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9"))

    def test_page_rebinds_picture_and_sound_when_the_run_changes(self):
        page = tas_serve.PAGE()
        self.assertIn("function follow(", page)
        self.assertIn('$("video").src = "/video?run="', page)
        self.assertIn('a.src = "/audio?"', page)


if __name__ == "__main__":
    unittest.main()
