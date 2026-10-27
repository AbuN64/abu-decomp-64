"""Per-call behaviour: alignment by (function, caller, ordinal) and the five verdicts, on mocked call files."""

import json
from pathlib import Path

import pytest

from abu_decomp.behave.percall import align, read_calls, summarise
from abu_decomp.behave.prototype import parse_declaration

PROTOS = {
    "add": parse_declaration("int add(int a, int b);", "add"),
    "scale": parse_declaration("float scale(float x);", "scale"),
    "put": parse_declaration("void put(int *p, int v);", "put"),
}


def proto(name):
    return PROTOS[name]


def o_call(fn, caller, o, args, ret=None, frame=1, **extra):
    rows = [{"k": "call", "fn": fn, "caller": caller, "o": o, "frame": frame, "i": o, "a": [f"{a:08x}" for a in args], **extra}]
    if ret is not None:
        rows.append({"k": "ret", "fn": fn, "i": o, "r": [f"{ret:08x}", "00000000"], "f0": "0"})
    return rows


def p_call(fn, caller, o, args, ret=None, frame=9, **extra):
    rows = [{"k": "call", "order": 3, "fn": fn, "caller": caller, "o": o, "frame": frame, "i": o,
             "a": [f"{a:08x}" for a in args] + ["0"] * (8 - len(args)), **extra}]
    if ret is not None:
        rows.append({"k": "ret", "order": 3, "i": o, "r": [f"{ret:08x}", "00000000"], "st0": None})
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


def test_write_digest_is_part_of_the_result(tmp_path):
    o = o_call("put", "main", 0, [0, 7], 0, w="aa")
    p = p_call("put", "main", 0, [0, 7], 0, w="bb")
    assert run(tmp_path, o, p)["put"].first["diffs"][0]["field"] == "writes"
    p_same = p_call("put", "main", 0, [0, 7], 0, w="aa")
    assert run(tmp_path, o, p_same)["put"].verdict == "behaves"


def test_pointer_arguments_compare_as_null_or_not(tmp_path):
    o = o_call("put", "main", 0, [0x80123456, 7])
    p = p_call("put", "main", 0, [0x08049000, 7])
    assert run(tmp_path, o, p)["put"].verdict == "behaves"
    p_null = p_call("put", "main", 0, [0, 7])
    assert run(tmp_path, o, p_null)["put"].verdict == "differs-args"


def test_float_result_within_four_ulp_behaves(tmp_path):
    o = o_call("scale", "main", 0, [0x3F800000], f12="3f800000") + [{"k": "ret", "fn": "scale", "i": 0, "r": ["0", "0"], "f0": "3f800000"}]
    p = p_call("scale", "main", 0, [0x3F800000]) + [{"k": "ret", "order": 3, "i": 0, "r": ["0", "0"], "st0": "3ff0000020000000"}]
    assert run(tmp_path, o[:1] + o[2:], p[:1] + p[2:])["scale"].verdict == "behaves"


def test_called_by_one_side_only(tmp_path):
    o = o_call("add", "main", 0, [1, 2], 3)
    p = p_call("put", "main", 0, [0, 1])
    verdicts = run(tmp_path, o, p)
    assert verdicts["add"].verdict == "missing" and verdicts["put"].verdict == "port-only"
    assert summarise(verdicts) == {
        "buckets": {"behaves": 0, "differs-args": 0, "differs-result": 0, "missing": 1, "port-only": 1},
        "own_fault": 0,
    }


def test_same_function_from_another_caller_is_differs_args(tmp_path):
    o = o_call("add", "main", 0, [1, 2], 3)
    p = p_call("add", "other", 0, [1, 2], 3)
    verdict = run(tmp_path, o, p)["add"]
    assert verdict.verdict == "differs-args" and verdict.first["diffs"][0]["field"] == "caller"


def test_ordinals_align_per_caller_not_globally(tmp_path):
    o = o_call("add", "a", 0, [1, 1]) + o_call("add", "b", 0, [2, 2])
    p = p_call("add", "b", 0, [2, 2]) + p_call("add", "a", 0, [1, 1])
    assert run(tmp_path, o, p)["add"].verdict == "behaves"


def test_a_capture_without_caller_is_refused(tmp_path):
    path = tmp_path / "old.jsonl"
    path.write_text(json.dumps({"k": "call", "order": 1, "i": 0, "a": ["0"]}) + "\n")
    with pytest.raises(ValueError, match="predates per-call"):
        read_calls(path, "oracle")


def test_prototype_sources_cover_cpp_definitions(tmp_path):
    """The project defines its functions in .CPP units with no header: those definitions are the declarations, so a void
    function is not judged on a garbage return word."""
    from abu_decomp import tas_coverage
    from abu_decomp.behave import replay

    unit = tmp_path / "src" / "game" / "A_1.CPP"
    unit.parent.mkdir(parents=True)
    unit.write_text("void Scene_Queue(s32 a, void *b) {\n    a = 1;\n}\n")
    proto = replay.Prototypes(tmp_path, tas_coverage.HEADERS).get("Scene_Queue")
    assert proto.ret == "void" and len(proto.params) == 2
