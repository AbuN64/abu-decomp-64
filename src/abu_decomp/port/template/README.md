# {{name}}

A native port of {{title}} in C23, built from its decompilation. `{{executable}}` boots until the first original owner that has no native implementation, then stops there with an error.

> This is a personal tool. It is not officially maintained and may change or break without notice.

## Build

```sh
cmake --preset release && cmake --build --preset release
```

This writes `build/release/{{executable}}`. It needs CMake 3.28+, Ninja and GCC 14+ or Clang 19+.

## Usage

```sh
build/release/{{executable}} --headless
```

It prints each boot step and exits 1 at the first owner with no native implementation. `--help` lists the options.

## Development

Run `ci/check`. It builds the debug preset with sanitizers and runs the unit tests and the boot gate.

## License

{{license_line}}
