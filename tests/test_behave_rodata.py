import struct
import tempfile
import unittest
from pathlib import Path

from abu_decomp.behave import inputs as i
from abu_decomp.behave import permutations, rodata
from abu_decomp.behave import prototype as p

PROTO = p.parse_declaration("void f(Actor *a, s32 n);", "f", {})
SPLAT = """segments:
  - name: seg_a
    type: code
    start: 0x1000
    vram: 0x80100000
    subsegments:
      - [0x1000, c, a]
      - [0x1100, rodata, lit]
      - [0x1120, data, d]
  - name: seg_b
    type: code
    start: 0x2000
    vram: 0x80200000
    subsegments:
      - [0x2000, rodata, tail]
  - name: end
    type: bin
    start: 0x2040
"""


def elf(sections):
    """A minimal 32-bit big-endian ELF: sections = [(flags, addr, bytes)]."""
    body = b"".join(data for _, _, data in sections)
    shoff = 0x34 + len(body)
    head = b"\x7fELF" + bytes([1, 2, 1]) + bytes(9) + struct.pack(">HHI", 2, 8, 1)
    head += struct.pack(">III", 0, 0, shoff) + struct.pack(">IHHHHHH", 0, 0x34, 0, 0, 40, len(sections) + 1, 0)
    rows, offset = [bytes(40)], 0x34
    for flags, addr, data in sections:
        rows.append(struct.pack(">IIIIIIIIII", 0, 1, flags, addr, offset, len(data), 0, 0, 4, 0))
        offset += len(data)
    return head + body + b"".join(rows)


class RodataTests(unittest.TestCase):
    def test_rodata_extents_come_from_the_splat_subsegments(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "versions/v").mkdir(parents=True)
            (Path(tmp) / "versions/v/splat.yaml").write_text(SPLAT)
            got = rodata.read_only_ranges(Path(tmp), "v")
            self.assertEqual(got, ((0x80100100, 0x80100120), (0x80200000, 0x80200040)))
            self.assertEqual(rodata.read_only_ranges(Path(tmp), "none"), ())

    def test_only_words_inside_the_ranges_are_taken_from_the_elf(self):
        image = elf([(3, 0x800D0000, struct.pack(">IIII", 0x3F800000, 7, 8, 9))])  # writable output section, as linked
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "X.elf"
            path.write_bytes(image)
            got = rodata.words(path, (0x800D0000, 0x800D0004, 0x800D000C, 0x800D0010), ((0x800D0000, 0x800D0008),))
        self.assertEqual(got, {0x800D0000: 0x3F800000, 0x800D0004: 7})

    def test_a_missing_elf_seeds_nothing(self):
        self.assertEqual(rodata.words(Path("/nonexistent/x.elf"), (0x800D0000,), ((0x800D0000, 0x800D0010),)), {})

    def test_the_rom_words_are_patched_in_every_case_and_never_swept(self):
        learned = i.Learned(state=(0x800D0000, 0x80102000), rom=((0x800D0000, 0x3F800000),), hot=((0, ((0, 4),)),))
        cases, _ = i.seeded_cases(PROTO, 12, 3, i.Layout(), learned)
        words = [{m["addr"]: m["hex"] for m in c["memory"] if m["addr"] in (0x800D0000, 0x80102000)} for c in cases]
        self.assertTrue(all(w[0x800D0000] == "3f800000" for w in words))
        self.assertGreater(len({w.get(0x80102000) for w in words}), 1)  # real state still varies

    def test_generated_cases_do_not_vary_the_roms_constants(self):
        reads = [(0x800C9028, 4), (0x800C9030, 4)]
        real = [{"registers": {"r29": "0x80200000"}}]
        image = type("I", (), {"read": lambda self, c, a, w: {0x800C9028: 5, 0x800C9030: 6}[a]})()
        args = (real, [{"reads": reads}], image, PROTO, Path("."), ())
        names = [f["name"] for f in permutations._factors(*args, ((0x800C9028, 0x800C902C),)) if f["kind"] == "memory"]
        self.assertNotIn("0x800c9028/4", names)


if __name__ == "__main__":
    unittest.main()
