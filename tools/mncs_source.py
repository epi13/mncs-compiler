"""Small source-location helpers for Compiler self-consumption probes."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def _tokens(source: bytes) -> list[tuple[bytes, int, int]]:
    """Return source tokens while skipping comments and quoted literals."""
    tokens: list[tuple[bytes, int, int]] = []
    offset = 0
    size = len(source)
    while offset < size:
        value = source[offset]
        if value in b" \t\r\n\v\f":
            offset += 1
            continue
        if source.startswith(b"//", offset):
            newline = source.find(b"\n", offset + 2)
            offset = size if newline < 0 else newline + 1
            continue
        if source.startswith(b"/*", offset):
            depth = 1
            offset += 2
            while offset < size and depth:
                if source.startswith(b"/*", offset):
                    depth += 1
                    offset += 2
                elif source.startswith(b"*/", offset):
                    depth -= 1
                    offset += 2
                else:
                    offset += 1
            if depth:
                raise ValueError("unterminated block comment in MNCS source")
            continue
        if value in (ord('"'), ord("'")):
            quote = value
            offset += 1
            while offset < size:
                if source[offset] == ord("\\"):
                    offset += 2
                elif source[offset] == quote:
                    offset += 1
                    break
                else:
                    offset += 1
            else:
                raise ValueError("unterminated quoted literal in MNCS source")
            continue
        start = offset
        if (ord("A") <= value <= ord("Z") or ord("a") <= value <= ord("z")
                or value == ord("_")):
            offset += 1
            while offset < size:
                item = source[offset]
                if (ord("A") <= item <= ord("Z") or ord("a") <= item <= ord("z")
                        or ord("0") <= item <= ord("9") or item == ord("_")):
                    offset += 1
                else:
                    break
            tokens.append((source[start:offset], start, offset))
            continue
        if source.startswith(b"->", offset):
            offset += 2
        else:
            offset += 1
        tokens.append((source[start:offset], start, offset))
    return tokens


def extract_mncs_function(source: bytes, name: str) -> tuple[bytes, int, int]:
    """Return one exact ``fn name`` declaration, ignoring comments/literals."""
    try:
        wanted = name.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("MNCS function names must be ASCII identifiers") from error
    tokens = _tokens(source)
    definitions = [
        index for index in range(len(tokens) - 2)
        if tokens[index][0] == b"fn" and tokens[index + 1][0] == wanted
        and tokens[index + 2][0] in (b"<", b"(")
    ]
    if len(definitions) != 1:
        raise ValueError(f"expected one fn {name} definition, found {len(definitions)}")

    start_index = definitions[0]
    start = tokens[start_index][1]
    parens = brackets = angles = 0
    body_open = None
    for index in range(start_index + 2, len(tokens)):
        token = tokens[index][0]
        if token == b"(" :
            parens += 1
        elif token == b")":
            parens -= 1
        elif token == b"[":
            brackets += 1
        elif token == b"]":
            brackets -= 1
        elif token == b"<":
            angles += 1
        elif token == b">" and angles:
            angles -= 1
        elif token == b"{" and not (parens or brackets or angles):
            body_open = index
            break
        if parens < 0 or brackets < 0 or angles < 0:
            raise ValueError(f"malformed signature for fn {name}")
    if body_open is None:
        raise ValueError(f"fn {name} has no body")

    depth = 0
    for token, _, token_end in tokens[body_open:]:
        if token == b"{":
            depth += 1
        elif token == b"}":
            depth -= 1
            if depth == 0:
                return source[start:token_end], start, token_end
            if depth < 0:
                break
    raise ValueError(f"fn {name} has an unterminated body")


def write_json_exclusive(path: Path, value: object) -> None:
    """Publish complete JSON atomically without replacing existing evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        try:
            # A hard link publishes the already complete inode and fails if
            # another writer created the destination in the meantime.
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as error:
            raise FileExistsError(f"refusing to overwrite immutable evidence: {path}") from error
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
