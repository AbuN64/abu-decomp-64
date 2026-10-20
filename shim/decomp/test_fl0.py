# SPDX-License-Identifier: GPL-3.0-or-later
"""Mock only native process execution; exercise real image/hash/acceptance logic."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

parser=argparse.ArgumentParser()
parser.add_argument("--work",type=Path,required=True)
args=parser.parse_args()
args.work.mkdir(parents=True,exist_ok=True)
sys.dont_write_bytecode=True
spec=importlib.util.spec_from_file_location("fl0",Path(__file__).with_name("fl0.py"))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
def png(rgb):
    def chunk(kind,data):
        return struct.pack(">I",len(data))+kind+data+struct.pack(">I",zlib.crc32(kind+data))
    scan=b"".join(b"\0"+rgb[y*48:(y+1)*48] for y in range(16))
    return b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",16,16,8,2,0,0,0))+chunk(b"IDAT",zlib.compress(scan))+chunk(b"IEND",b"")
class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=args.work);self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.gold=self.root/"gold";self.gold.mkdir()
        self.binary=self.root/"binary";self.binary.write_bytes(b"mock process")
        self.rom=self.root/"rom";self.rom.write_bytes(b"test user input")
        self.rgb=b"".join(bytes([255 if 4<=x<12 and 4<=y<12 else 0])*3 for y in range(16) for x in range(16))
        (self.gold/"vi.png").write_bytes(png(self.rgb))
        self.manifest={"checkpoints":{"test":{"vi":3,"files":{"vi":{"path":"vi.png","sha256":module.digest(self.gold/"vi.png")}}}}}
        (self.gold/"manifest.json").write_text(json.dumps(self.manifest))
        self.dl = struct.pack("<IIII", 0xDB060000, 0, 0xDF000000, 0)
        (self.gold/"dl.bin").write_bytes(self.dl)
        self.manifest["checkpoints"]["boot"] = {"files": {"dl": {"path": "dl.bin", "bytes": len(self.dl), "sha256": module.digest(self.gold/"dl.bin"), "task_seq": 1, "task_vi": 39}}}
        (self.gold/"manifest.json").write_text(json.dumps(self.manifest))
        self.project={"version":"test","dumps":[{"id":"test","supported":True,"sha256":module.digest(self.rom)}],"frame_gate":{"checkpoint":"test","retrace":3,"framebuffer":[16,16],"output":[16,16],"crop":[0,0,16,16],"threshold":0.9}}
        self.project_path=self.root/"project.json"
    def execute(self,mode="success"):
        self.project_path.write_text(json.dumps(self.project))
        def process(command,stdout,**kwargs):
            if mode != "empty":
                Path(kwargs["env"]["N64C_FIRST_GFX_DL"]).write_bytes(self.dl)
                stdout.write("boot-gfx: first submit seq=2 retrace=0 data=0x80000100 bytes=16 audio_before=1\n")
            if mode not in ["stop","empty"]:
                rgb=self.rgb if mode=="success" else bytes(len(self.rgb))
                Path(command[command.index("--capture")+1]).write_bytes(b"P6\n16 16\n255\n"+rgb)
                stdout.write("boot-frame: retrace=3 tasks=1 audio=1 commands=4 surface=0x00000300\n")
                stdout.flush()
            return subprocess.CompletedProcess(command,78 if mode=="stop" else 0)
        with patch.object(module.subprocess,"run",side_effect=process) as called:
            report=module.run(self.binary,self.rom,self.project_path,self.gold,self.root/"out")
        return report,called.call_count
    def test_frame_requires_original_execution_and_new_evidence(self):
        good,count=self.execute();self.assertTrue(good["accepted"]);self.assertEqual(count,1)
        self.assertAlmostEqual(good["whole_ssim"],1)
        self.assertAlmostEqual(good["content_ssim"],1)
        for mode in ["stop","empty","black"]:
            report,count=self.execute(mode);self.assertFalse(report["accepted"])
            self.assertEqual(count,1);self.assertNotEqual(report["attempt"],good["attempt"])
        self.assertTrue((Path(good["attempt"])/"native.ppm").is_file())
    def test_invalid_inputs_never_start_native_process(self):
        original=copy.deepcopy(self.project)
        for change in [lambda p:p["frame_gate"].update(threshold=0.89),
                       lambda p:p["frame_gate"].update(retrace=4),
                       lambda p:p["dumps"][0].update(sha256="0"*64),
                       lambda p:p["frame_gate"].update(output=[0,16])]:
            self.project=copy.deepcopy(original);change(self.project)
            report,count=self.execute();self.assertFalse(report["accepted"]);self.assertEqual(count,0)
    def test_golden_hash_and_missing_image(self):
        (self.gold/"vi.png").write_bytes(b"changed")
        report,count=self.execute();self.assertFalse(report["accepted"]);self.assertEqual(count,0)
        self.manifest["checkpoints"]["test"]["files"]["vi"]=None
        (self.gold/"manifest.json").write_text(json.dumps(self.manifest))
        report,count=self.execute();self.assertFalse(report["accepted"]);self.assertEqual(count,0)
    def test_blank_oracle_and_native_are_not_acceptance(self):
        self.rgb=bytes(len(self.rgb));(self.gold/"vi.png").write_bytes(png(self.rgb))
        self.manifest["checkpoints"]["test"]["files"]["vi"]["sha256"]=module.digest(self.gold/"vi.png")
        (self.gold/"manifest.json").write_text(json.dumps(self.manifest))
        report,count=self.execute();self.assertFalse(report["accepted"]);self.assertEqual(count,1)
        self.assertIn("blank",report["error"])
    def test_dl_exact_words_lengths_and_receipt(self):
        native=self.root/"native.dl"; log=self.root/"boot.log"
        log.write_text("boot-step: 473/932 submit_task\nboot-gfx: first submit seq=2 retrace=0 data=0x80000100 bytes=16 audio_before=1\n")
        native.write_bytes(self.dl)
        report=module.compare_dl(native,self.gold,log)
        self.assertTrue(report["accepted"])
        self.assertEqual(report["submission"]["seq"],2)
        self.assertEqual(report["last_first_hit"]["name"],"submit_task")
        changed=bytearray(self.dl);changed[5]=1;native.write_bytes(changed)
        report=module.compare_dl(native,self.gold,log)
        self.assertFalse(report["accepted"])
        self.assertEqual(report["first_diff"]["byte_offset"],5)
        self.assertEqual(report["first_diff"]["native"],["0xdb060000","0x00000100"])
        for data in [self.dl[:8],self.dl+bytes(8),b"",b"bad"]:
            native.write_bytes(data)
            self.assertFalse(module.compare_dl(native,self.gold)["accepted"])
        native.write_bytes(self.dl);log.write_text("")
        self.assertFalse(module.compare_dl(native,self.gold,log)["accepted"])
        (self.gold/"dl.bin").write_bytes(bytes(16))
        self.assertFalse(module.compare_dl(native,self.gold)["accepted"])

    def test_dl_survives_later_boot_failure_without_frame_acceptance(self):
        report,_=self.execute("stop")
        self.assertTrue(report["fl0_a"]["accepted"])
        self.assertFalse(report["accepted"])

unittest.main(argv=[sys.argv[0]])
