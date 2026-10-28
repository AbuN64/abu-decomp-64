# SPDX-License-Identifier: GPL-3.0-or-later
"""Case-blind source/header discovery shared by the boot tools and CMake.

`python srcfiles.py --cmake-patterns` emits CMake glob patterns from the same source suffixes.
"""
SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx")
HEADER_SUFFIXES = (".h", ".hh", ".hpp", ".hxx")


def is_source(path):
    return str(path).lower().endswith(SOURCE_SUFFIXES)


def is_header(path):
    return str(path).lower().endswith(HEADER_SUFFIXES)


def sources(source, folder="src"):
    """Every source file under source/folder, sorted; the host supplies libgcc."""
    root = source / folder
    return sorted((p for p in root.rglob("*") if p.is_file() and is_source(p)
                   and "libgcc" not in p.relative_to(root).parts[:-1]), key=str)


def headers(source, folder="include"):
    return sorted(p for p in (source / folder).rglob("*") if p.is_file() and is_header(p))


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
