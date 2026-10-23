import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace

spec=importlib.util.spec_from_file_location("lockstep",Path(__file__).with_name("lockstep.py"))
ls=importlib.util.module_from_spec(spec);spec.loader.exec_module(ls)

class AliasTests(unittest.TestCase):
    def matcher(self, reverse=False):
        symbols={"mapped_callback":0x00401234,"callback":0x80401234,"other":0x80405678}
        entries=[(0x08001000,0,"mapped_callback",False),(0x08001000,32,"callback",False)]
        if reverse:entries.reverse()
        entries.append((0x08002000,16,"other",False))
        return ls.Matcher(0x800000,(sorted((v,k) for k,v in symbols.items()),symbols),entries)

    def test_nm_size_less_code_alias_is_retained(self):
        output = "08001000 T mapped_callback\n08001000 00000020 T callback\n08003000 B unknown_size_data\n"
        with patch.object(ls.subprocess, "run", return_value=SimpleNamespace(stdout=output)):
            entries = ls.read_exe_functions("fixture")
            self.assertEqual(entries, [(0x08001000,0,"mapped_callback",False),
                                       (0x08001000,32,"callback",False)])
            self.assertEqual(ls.read_exe_data("fixture"), [])
        symbols={"mapped_callback":0x00401234,"callback":0x80401234}
        matcher=ls.Matcher(0x800000,(sorted((v,k) for k,v in symbols.items()),symbols),entries)
        self.assertTrue(matcher.same_function(0x00401234,0x08001000))

    def test_every_symbol_proven_alias_is_order_independent(self):
        for reverse in (False,True):
            m=self.matcher(reverse)
            for original in (0x00401234,0x80401234):
                self.assertTrue(m.same_function(original,0x08001000))
                self.assertTrue(m.same_address(original,0x08001000))

    def test_unrelated_symbols_and_heap_addresses_are_not_equivalent(self):
        m=self.matcher()
        for original,native in ((0x00401235,0x08001000),(0x80405678,0x08001000),
                                (0x00401234,0x08001020),(0x80401234,0x80670000),
                                (0x80670000,0x08001000),(0x00401234,0x08000000)):
            self.assertFalse(m.same_address(original,native))

    def test_memory_words_use_the_same_function_identity(self):
        original=(0x00401234).to_bytes(4,"big")+bytes(4092)
        native=(0x08001000).to_bytes(4,"little")+bytes(4092)
        self.assertEqual(self.matcher().page_diff(original,native),{})

if __name__=="__main__":unittest.main()
