# SPDX-License-Identifier: GPL-3.0-or-later
"""Case-blind source/header discovery shared by the boot tools and CMake: the one definition of a project's source and
header files for the shim. Suffixes match case-blind, so RageWars' `.CPP` units, its `.c` units and `.H`/`.h`
headers are all seen. Never hard-code `*.CPP`.

`python srcfiles.py --cmake-patterns` emits CMake glob patterns from the same source suffixes.
"""
import pathlib

SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx")
HEADER_SUFFIXES = (".h", ".hh", ".hpp", ".hxx")


def is_source(path):
    return str(path).lower().endswith(SOURCE_SUFFIXES)


def is_header(path):
    return str(path).lower().endswith(HEADER_SUFFIXES)


def layout(source):
    """The tree's layout: unbake's packaged [layout] (src/, include/, src/sdk/{library}) with the tree's config.toml
    [layout] keys over it."""
    from unbake import api  # the boot tools run under the decomp venv; CMake's --cmake-patterns does not need it

    return api.layout(pathlib.Path(source))


def library_folder(source, library):
    """The tree-relative folder of one library (layout library_source: src/sdk/libgcc unless config.toml says)."""
    return layout(source)["library_source"].format(library=library)


def library_prefix(source):
    """The lower-case tree-relative folder prefix of the libraries (src/sdk/ unless config.toml says)."""
    return layout(source)["library_source"].partition("{")[0].lower()


def include_flags(source):
    """-I of each header root of the tree (layout header_roots), then of its libgcc folder (its own headers)."""
    source = pathlib.Path(source)
    return [f"-I{source / h}" for h in layout(source)["header_roots"]] + [f"-I{source / library_folder(source, 'libgcc')}"]


def sources(source, folder=None):
    """Every source file under source/folder (default: the layout's source roots; libgcc excluded: the host supplies
    it), sorted."""
    source = pathlib.Path(source)
    folders = [folder] if folder else layout(source)["source_roots"]
    found = []
    for f in folders:
        root = source / f
        found += [p for p in root.rglob("*") if p.is_file() and is_source(p)
                  and "libgcc" not in p.relative_to(root).parts[:-1]]
    return sorted(found, key=str)


def headers(source, folder=None):
    """Every header under source/folder (default: the layout's header roots), sorted."""
    source = pathlib.Path(source)
    folders = [folder] if folder else layout(source)["header_roots"]
    return sorted(p for f in folders for p in (source / f).rglob("*") if p.is_file() and is_header(p))


def cmake_patterns():
    return ["*" + "".join(f"[{c}{c.upper()}]" if c.isalpha() else c for c in suffix)
            for suffix in SOURCE_SUFFIXES]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cmake-patterns", action="store_true", required=True,
                        help="emit semicolon-separated case-blind source globs")
    parser.parse_args()
    print(";".join(cmake_patterns()))
