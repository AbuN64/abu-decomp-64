# abu-decomp-64

Runs the N64 decompilation dev cycle over unbake, Recipe Maker and Cake Taste, driven by AI lanes, and shows it on a streamed seat.

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
decomp port new --spec port.json ../my-port
```

Every command prints one JSON result. `decomp mcp` serves the same commands as MCP tools ([docs/mcp.md](docs/mcp.md)). `decomp --help` lists the rest.

## Development

```sh
pip install -e '.[dev]' && ci/check
```

## License

Released under [GNU GPL version 3 or later](LICENSE).
