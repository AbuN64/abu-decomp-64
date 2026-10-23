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

## Development

```sh
pip install -e '.[dev]' && ci/check
```

## License

Released under [GNU GPL version 3 or later](LICENSE).
