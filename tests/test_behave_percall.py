"""Per-call behaviour: alignment by (function, caller, ordinal) and the five verdicts, on mocked call files."""

import json
from pathlib import Path

from abu_decomp.behave.percall import align, read_calls
from abu_decomp.behave.prototype import parse_declaration

PROTOS = {
    "add": parse_declaration("int add(int a, int b);", "add"),
    "scale": parse_declaration("float scale(float x);", "scale"),
    "put": parse_declaration("void put(int *p, int v);", "put"),
}


def proto(name):
    return PROTOS[name]


def o_call(fn, caller, o, args, ret=None, frame=1, m=None, **extra):
    rows = [
        {
            "k": "call",
            "fn": fn,
            "caller": caller,
            "o": o,
            "frame": frame,
            "i": o,
            "a": [f"{a:08x}" for a in args],
            **extra,
        }
    ]
    if ret is not None:
        rows.append({"k": "ret", "fn": fn, "i": o, "r": [f"{ret:08x}", "00000000"], "f0": "0", "m": m})
    return rows


def p_call(fn, caller, o, args, ret=None, frame=9, m=None, **extra):
    rows = [
        {
            "k": "call",
            "order": 3,
            "fn": fn,
            "caller": caller,
            "o": o,
            "frame": frame,
            "i": o,
            "a": [f"{a:08x}" for a in args] + ["0"] * (8 - len(args)),
            **extra,
        }
    ]
    if ret is not None:
        rows.append({"k": "ret", "order": 3, "i": o, "r": [f"{ret:08x}", "00000000"], "st0": None, "m": m})
    return rows


def load(tmp_path: Path, name: str, rows) -> dict:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in [{"k": "meta", "first": 16}, *rows]) + "\n")
    return read_calls(path, name)[0]


def run(tmp_path, oracle_rows, port_rows):
    return align(load(tmp_path, "o", oracle_rows), load(tmp_path, "p", port_rows), proto)


def test_equal_calls_behave_whatever_the_poll_phase(tmp_path):
    o = o_call("add", "main", 0, [1, 2], 3, frame=4) + o_call("add", "main", 1, [5, 6], 11, frame=4)
    p = p_call("add", "main", 0, [1, 2], 3, frame=20) + p_call("add", "main", 1, [5, 6], 11, frame=21)
    verdict = run(tmp_path, o, p)["add"]
    assert (verdict.verdict, verdict.compared, verdict.first, verdict.cause) == ("behaves", 2, None, None)


def test_other_argument_is_differs_args_with_input_cause(tmp_path):
    o = o_call("add", "main", 0, [1, 2], 3) + o_call("add", "main", 1, [5, 6], 11)
    p = p_call("add", "main", 0, [1, 2], 3) + p_call("add", "main", 1, [5, 7], 12)
    verdict = run(tmp_path, o, p)["add"]
    assert verdict.verdict == "differs-args" and verdict.cause == "input"
    assert verdict.first["ordinal"] == 1
    assert verdict.first["diffs"] == [{"field": "b", "kind": "int32", "oracle": "0x6", "port": "0x7"}]


def test_equal_arguments_other_return_is_differs_result_with_own_cause(tmp_path):
    o = o_call("add", "main", 0, [1, 2], 3)
    p = p_call("add", "main", 0, [1, 2], 4)
    verdict = run(tmp_path, o, p)["add"]
    assert verdict.verdict == "differs-result" and verdict.cause == "own"
    assert verdict.first["diffs"][0] == {"field": "return", "kind": "int32", "oracle": "0x3", "port": "0x4"}


def window(*words):
    return "".join(w if isinstance(w, str) else f"{w:08x}" for w in words).ljust(128, "0")


def test_pointer_window_is_part_of_the_result(tmp_path):
    # `put(int *p, int v)`: the oracle names the window by a0, the port by cdecl word 0
    o = o_call("put", "main", 0, [0x80001000, 7], 0, m={"0": window(1, 2, 3)})
    p = p_call("put", "main", 0, [0x1000, 7], 0, m={"0": window(1, 9, 3)})
    verdict = run(tmp_path, o, p)["put"]
    assert (verdict.verdict, verdict.cause) == ("differs-result", "own")
    assert verdict.first["diffs"] == [{"field": "*p[1]", "kind": "memory", "oracle": "00000002", "port": "00000009"}]
    assert verdict.first["oracle"]["writes"].startswith("p: 000000010000000200000003")
    same = p_call("put", "main", 0, [0x1000, 7], 0, m={"0": window(1, 2, 3)})
    assert run(tmp_path, o, same)["put"].verdict == "behaves"


def test_a_window_one_side_did_not_record_is_not_compared(tmp_path):
    o = o_call("put", "main", 0, [0x80001000, 7], 0, m={"0": window(1)})
    p = p_call("put", "main", 0, [0x1000, 7], 0, m={})
    assert run(tmp_path, o, p)["put"].verdict == "behaves"


def test_ring_ordinals_align_and_a_divergence_after_the_first_n_is_found(tmp_path):
    ordinals = [0, 1, 8, 16, 32]  # first calls, then the power-of-two ring both sides keep
    o = [r for n in ordinals for r in o_call("add", "main", n, [n, 1], n + 1)]
    p = [r for n in ordinals for r in p_call("add", "main", n, [n, 1], n + 1 if n < 16 else 0)]
    verdict = run(tmp_path, o, p)["add"]
    assert verdict.compared == 5 and verdict.verdict == "differs-result"
    assert verdict.first["ordinal"] == 16
