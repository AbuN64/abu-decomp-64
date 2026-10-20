"""Reference instability must never suppress unrelated stable call differences."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('lockstep', Path(__file__).with_name('lockstep.py'))
ls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ls)


def call(a, sp=0x80003f00, f12=0, f14=0):
    return dict(a=[f'{v:08x}' for v in a], sp=f'{sp:08x}',
                f12=f'{f12:016x}', f14=f'{f14:016x}')


class ReferenceCallTests(unittest.TestCase):
    def test_masks_and_missing_samples_fail_closed(self):
        a, b = call([0xffff0001, 2, 3, 4]), call([1, 9, 3, 4])
        self.assertEqual(set(ls.reference_call_changes(['uint16', 'int32'], None, a, b)), {'arg1'})
        self.assertEqual(ls.reference_call_changes(['int32'], 'int32', a, None), {})
        self.assertEqual(ls.reference_call_changes(None, None, a, b), {})

    def test_floating_and_aligned_64_bit_reference_abi(self):
        a = call([9, 123, 0x11223344, 0x55667788], f12=0x400000003f800000)
        b = call([9, 999, 0x11223344, 0x55667789], f12=0x499999993f800000)
        self.assertEqual(set(ls.reference_call_changes(['int32', 'int64'], None, a, b)), {'arg1'})
        self.assertEqual(ls.reference_call_changes(['float'], None, a, b), {})
        self.assertEqual(set(ls.reference_call_changes(['double'], None, a, b)), {'arg0'})

    def test_stack_relocation_is_not_reference_instability(self):
        a, b = call([0x80003f10, 1, 0, 0]), call([0x80004f10, 2, 0, 0], sp=0x80004f00)
        self.assertEqual(set(ls.reference_call_changes(['ptr', 'int32'], None, a, b)), {'arg1'})

    def test_return_kind_and_observation_coverage(self):
        a = call([1, 0, 0, 0])
        left = dict(r=['11223344', '55667788'], f0='000000003f800000')
        right = dict(r=['11223344', '55667789'], f0='0000000040000000')
        for kind in ('int64', 'float', 'double'):
            self.assertEqual(set(ls.reference_call_changes([], kind, a, a, left, right)), {'return'})
        for kind in ('void', 'int32', None):
            self.assertEqual(ls.reference_call_changes([], kind, a, a, left, right), {})
        self.assertEqual(ls.reference_call_changes([], 'float', a, a, left, None), {})

    def test_production_report_keeps_stable_mismatch_and_lists_volatile_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'trace').write_text('order\taddress\tsymbol\n1\t0x80001000\tfixture\n')
            (root / 'types').write_text('name\treturn\tparams\nfixture\tint32\tint32,int32\n')
            (root / 'accept').write_text('\t'.join(ls.ACCEPT_FIELDS) + '\n')
            page = bytes(4096)
            h = ls.page_hash(page)
            for name, values in [('oracle', [1, 2, 0, 0]), ('peer', [1, 3, 0, 0]),
                                 ('port', [8, 9, 0, 0])]:
                directory = root / name
                directory.mkdir()
                original = name != 'port'
                identity = {'fn': 'fixture'} if original else {'order': 1}
                data = [dict(k='meta', ram=4096, bytes='canonical-be' if original else 'native'),
                        dict(k='hit', pages=[[0, f'{h:016x}']], **identity),
                        dict(k='call', i=0, thr=1, **identity, **call(values))]
                (directory / 'lockstep.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in data))
                (directory / 'pages.bin').write_bytes(struct.pack('<IIQ', 0, 0, h) + page)
            argv = ['compare', '--oracle', str(root / 'oracle'), '--oracle2', str(root / 'peer'),
                    '--port', str(root / 'port'), '--trace', str(root / 'trace'),
                    '--types', str(root / 'types'), '--accept', str(root / 'accept')]
            args = ls.build_parser().parse_args(argv)
            report = ls.compare(args)
            self.assertEqual(len(report['calls']), 1)
            self.assertTrue(report['calls'][0]['why'].startswith('arg0 '))
            self.assertEqual(report['matched_through_after_accepted'], 0)
            self.assertEqual(report['volatile_calls'][0]['field'], 'arg1')
            self.assertEqual(report['volatile_calls'][0]['oracle2'], '0x3')
            args.oracle2 = None
            self.assertEqual(len(ls.compare(args)['calls']), 2)


if __name__ == '__main__':
    unittest.main()
