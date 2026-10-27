# abu-decomp-64

Runs the N64 decompilation dev cycle over unbake, Recipe Maker and Cake Taste, driven by AI lanes,; the TAS page (`decomp tas serve`) shows the movies it plays.

> This is a personal tool. It is not officially maintained and may change or break without notice.

## Install

Python 3.13 or newer, in its own virtual environment.

```sh
python3.13 -m venv ~/.venvs/decomp && . ~/.venvs/decomp/bin/activate
pip install git+https://github.com/AbuN64/abu-decomp-64 && decomp dependencies
```

`decomp dependencies` checks that unbake, Recipe Maker and Cake Taste are installed at their pinned commits.

## Usage

```sh
decomp --profile seat.json unbake report --next
decomp port new --spec port.json ../my-port   # a port on the n64 shim (shim/)
```

```sh
decomp --profile seat.json behave --project TREE --host HOST --function func_80299B74_de --candidate cand.c
decomp --profile seat.json behave --project TREE --host HOST --function func_80299B74_de   # describe the original
```

`decomp behave` runs the original function and a candidate C function on the same generated inputs and compares their
behaviour: `same`, `differs` (first difference and the inputs that show it) or `untestable` (with the reason). A near miss
that is `same` is wrong only in code shape; one that `differs` has wrong logic. It never replaces the byte check. Without
`--candidate` it describes the original: the offsets it reads and writes through each pointer argument (a struct-typing
helper), its globals and callees. See [CONTRACTS.md](CONTRACTS.md), module `behave`.

Every command prints one JSON result. `decomp mcp` serves the same commands as MCP tools ([docs/mcp.md](docs/mcp.md)). `decomp --help` lists the rest.

`decomp cleanup owners --dir HEADERS` lists the owning module of every declaration of a header folder (definer for externs,
majority user for types and macros). Without `--dir` it plans the unit-local `extern` declarations of every source:
the owning header of an id is its definer's (for a symbol no C unit defines: the header of the unit whose `start`/`end`
address range in the module map holds it, else its majority user), the prototype is the definition's or the one every
declaration agrees on, and an id whose declarations conflict, or whose name an includer of the destination header spells
otherwise, is refused and listed with the reason. A unit sees a header only through its unconditional `#include`s (one under an
`#if`/`#ifdef` is not read by every build): a unit that has the destination header only that way gets a plain `#include`.
`--packs DIR` writes `externs-batch-NN.diff` (each over the tree the
earlier ones leave, applied like any tool batch: one stamp per batch), `externs.json` (unit-extern counts before and
after, per batch the estimated stamp CPU, the refusals) and `externs.tsv`; `--batch-cpu` and `--unit-cpu` size a batch
under the stamp CPU cap. Project facts come from `--map` (default `docs/MODULES.tsv`).

Phase playbooks (what each of the four phases and the naming/real-C track is for, its measurement and order of attack): [docs/playbooks](docs/playbooks/README.md).

## Development

```sh
pip install -e '.[dev]' && ci/check
```

`pip install -e '.[dev]'` installs the pinned tools the tests import (Cake Taste, unbake, Recipe Maker, gcovr) and the lint
tools (ruff, pytest); `ci/check` runs with the first python that has them (`ABU_PYTHON`, else `python3`, else
`$ABU_VENV/bin/python`, default `~/venv-decomp`) and stops with that instruction when none does. While working,
`ci/check --changed` lints the changed files and runs only the tests that import the changed modules (under a second to
seconds); run the full `ci/check` before a push that touches `shim/` or `ci/`.

## License

Released under [GNU GPL version 3 or later](LICENSE).
