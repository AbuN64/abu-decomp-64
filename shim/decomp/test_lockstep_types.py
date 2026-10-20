"""Type extraction must use the native C dialect and expose compiler failures."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

spec=importlib.util.spec_from_file_location("lockstep",Path(__file__).with_name("lockstep.py"))
ls=importlib.util.module_from_spec(spec);spec.loader.exec_module(ls)

class ExtractionTests(unittest.TestCase):
    def test_native_c89_conversion_keeps_function_signature(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); (root/"decomp").mkdir()
            (root/"decomp/unit.i").write_text("void fn_one(void) { return 7; }\n")
            (root/"trace.tsv").write_text("order\taddress\tsymbol\n1\t0x80001000\tfn_one\n")
            self.assertEqual(ls.build_types(root,root/"trace.tsv",root/"types.tsv","gcc",1),(1,1))
            self.assertEqual(ls.read_types(root/"types.tsv"),{"fn_one":("void",[])})

    def test_compile_failure_cannot_reuse_stale_auxiliary_output(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); (root/"decomp").mkdir(); (root/"types.aux").mkdir()
            (root/"decomp/unit.i").write_text("this is not C;\n")
            (root/"trace.tsv").write_text("order\taddress\tsymbol\n1\t0x80001000\tfn_one\n")
            (root/"types.aux/0.txt").write_text("/* old.i:1:NF */ extern void fn_one (void);\n")
            with self.assertRaises(subprocess.CalledProcessError):
                ls.build_types(root,root/"trace.tsv",root/"types.tsv","gcc",1)
            self.assertFalse((root/"types.aux/0.txt").exists())
            self.assertFalse((root/"types.tsv").exists())

if __name__=="__main__":
    unittest.main()
