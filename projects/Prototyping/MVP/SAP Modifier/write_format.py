"""The `.mwrite` file format: strict JSON loader/validator and atomic saver.

Zero Qt dependency. This is the ONLY SAP Modifier module that writes files
(`test_write_static.py` enforces that with an AST scan, plus the sample
generator which is a build-time tool). It sits on top of `write_model.py`
(dataclasses, tag algebra, sanitiser, caps) and is used by `write_resolve.py`
(nested Writes) and `app_write.py` (Open/Save).

The file is one UTF-8 JSON object:

    {"version": 1, "paragraphs": [
        {"indent": 0, "runs": [{"text": "Phrase: ", "tags": {"bold": true}},
                               {"text": "@sub/a.mwrite"}]},
        {"indent": 1, "runs": [{"embed": {"png_b64": "...",
                                          "source": [{"text": "Goals: x"}]}}]}]}

`tags` is omitted when empty. Unknown tag keys and unknown extra keys on
objects are ignored (forward compatibility); a KNOWN key with a bad value is an
error.

Loading is strict, because a `.mwrite` may come from anywhere (including
nested references inside someone else's Write): the file is read as at most
`max_bytes + 1` bytes (never trusting a prior `stat`); decoded as strict UTF-8;
parsed with `NaN`/`Infinity` and duplicate keys rejected; `RecursionError` /
`ValueError` / `UnicodeError` from the parser become `WriteFormatError`;
`version` and `indent` must be genuine ints (`type(x) is int`, so `true` is
rejected); every count and size has a cap; an embed's `png_b64` length is
checked BEFORE it is decoded, decoded with `validate=True`, and its PNG header
must declare sane dimensions (`check_png`) - a decompression bomb is refused
from a few header bytes, without ever asking an image library to decode it.
Control characters in text are scrubbed by `sanitize_text` and reported as
warnings rather than rejected.

Return convention (decided once): `read_write_file` / `parse_write_bytes`
return a `LoadResult` - a `NamedTuple` `(document, warnings)` - so callers can
use `.document` / `.warnings` or unpack two values. `warnings` is a list of
human strings (capped; the last entry summarises any suppressed ones). Errors
raise `WriteFormatError` (a `ValueError` with `.message` and `.where`, the JSON
path of the offending value) or, for a file over the byte cap,
`WriteFileTooLarge` (a subclass, so the resolver can show "truncated" instead
of "broken"). Ordinary `OSError`s from opening the file propagate unchanged.

Saving (`save_write`) is atomic: serialise, refuse over `MAX_FILE_BYTES`,
refuse a directory target, then `tempfile.mkstemp` in the target's own folder
-> write -> flush -> `os.fsync` -> close -> `os.replace`; any exception closes
and unlinks the temp file and re-raises, so the original is never half-written.
Documented choice: before touching the disk the serialised bytes are re-parsed
with the same strict loader, and saving is refused if the result would not
load again (e.g. more than `MAX_EMBEDS` embeds, which the editor's deduping
registry cannot prevent) - better an honest refusal than a file the app then
rejects. A file that FAILED to load is never written to by this module.

Explicitly out of scope: Qt, image decoding (only the PNG header is read here),
and anything about references or the instruction grammar.
"""

import base64
import binascii
import json
import math
import os
import struct
import tempfile
from pathlib import Path
from typing import NamedTuple

from write_model import (  # noqa: F401 - re-exported so callers can take caps from either module
    FORMAT_VERSION,
    ITALIC_STATES,
    MAX_EMBED_PNG_BYTES,
    MAX_EMBEDS,
    MAX_IMAGE_DIM,
    MAX_IMAGE_PIXELS,
    MAX_INDENT,
    MAX_PARAGRAPHS,
    MAX_RUN_TEXT,
    MAX_RUNS_PER_PARAGRAPH,
    MAX_TOTAL_EMBED_BYTES,
    MAX_TOTAL_RUNS,
    SPREAD_MAX,
    SPREAD_MIN,
    EmbedRun,
    Paragraph,
    Run,
    WriteDocument,
    clamp_indent,
    coalesce_runs,
    normalize_tags,
    sanitize_text,
)

MAX_FILE_BYTES = 8 * 1024 * 1024

_MAX_WARNINGS = 100
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class WriteFormatError(ValueError):
    """A `.mwrite` file (or in-memory document) is invalid.

    `where` is a JSON-path-like locator ("$.paragraphs[3].runs[0]") or a
    "line L column C" position for a syntax error; `message` is the bare
    reason. `str(exc)` combines both.
    """

    def __init__(self, message: str, where: str = "$"):
        super().__init__(f"{message} (at {where})")
        self.message = message
        self.where = where


class WriteFileTooLarge(WriteFormatError):
    """The file is larger than the byte cap - distinct so callers can say "truncated"."""


class LoadResult(NamedTuple):
    """`(document, warnings)` - see the module docstring for the return convention."""

    document: WriteDocument
    warnings: list


def png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    """Read `(width, height)` from a PNG's signature + IHDR chunk, without decoding.

    Raises `ValueError` for a missing/bad signature, a first chunk that is not
    a 13-byte IHDR, or a zero dimension. Deliberately does not verify the chunk
    CRC or any later data - the point is a cheap, allocation-free size check
    BEFORE any image library is allowed near the bytes. Qt-free so
    `write_embed.py` can use it as its first gate.
    """
    if len(png_bytes) < 33 or png_bytes[:8] != _PNG_SIGNATURE:
        raise ValueError("not a PNG (bad signature or too short)")
    length, chunk_type = struct.unpack(">I4s", png_bytes[8:16])
    if chunk_type != b"IHDR" or length != 13:
        raise ValueError("PNG does not start with an IHDR chunk")
    width, height = struct.unpack(">II", png_bytes[16:24])
    if width == 0 or height == 0:
        raise ValueError("PNG declares a zero dimension")
    return width, height


def check_png(png_bytes: bytes) -> tuple[int, int]:
    """`png_dimensions` plus the size caps: each side <= `MAX_IMAGE_DIM`, area <= `MAX_IMAGE_PIXELS`.

    Returns `(width, height)`; raises `ValueError` if malformed or over a cap.
    """
    width, height = png_dimensions(png_bytes)
    if width > MAX_IMAGE_DIM or height > MAX_IMAGE_DIM:
        raise ValueError(f"PNG is {width}x{height}; a side may not exceed {MAX_IMAGE_DIM}")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError(f"PNG is {width}x{height}; more than {MAX_IMAGE_PIXELS} pixels")
    return width, height


class _Ctx:
    """Mutable counters and the warning list for one load."""

    def __init__(self):
        self.warnings: list[str] = []
        self._suppressed = 0
        self.total_runs = 0
        self.embeds = 0
        self.embed_bytes = 0

    def warn(self, message: str) -> None:
        if len(self.warnings) < _MAX_WARNINGS:
            self.warnings.append(message)
        else:
            self._suppressed += 1

    def finish(self) -> list[str]:
        if self._suppressed:
            self.warnings.append(f"{self._suppressed} further warnings suppressed")
        return self.warnings


def _reject_constant(name: str):
    raise WriteFormatError(f"JSON constant {name} is not allowed", "$")


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise WriteFormatError(f"duplicate key {key!r}", "$")
        out[key] = value
    return out


def _validate_tags(tags, where: str) -> dict:
    """Validate a `tags` value strictly (known keys) and return the canonical dict."""
    if tags is None:
        return {}
    if not isinstance(tags, dict):
        raise WriteFormatError("tags must be an object", where)
    for key in ("bold", "line", "dash"):
        if key in tags and not isinstance(tags[key], bool):
            raise WriteFormatError(f"{key} must be true or false", f"{where}.{key}")
    if "italic" in tags:
        value = tags["italic"]
        if type(value) is not str or value not in ITALIC_STATES:
            raise WriteFormatError('italic must be "right" or "left"', f"{where}.italic")
    if "spread" in tags:
        value = tags["spread"]
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        if not ok or not (SPREAD_MIN <= value <= SPREAD_MAX):
            raise WriteFormatError(f"spread must be a number from {SPREAD_MIN} to {SPREAD_MAX}", f"{where}.spread")
    return normalize_tags(tags)


def _text_run(obj: dict, where: str, ctx: _Ctx) -> Run:
    text = obj["text"]
    if not isinstance(text, str):
        raise WriteFormatError("text must be a string", f"{where}.text")
    if len(text) > MAX_RUN_TEXT:
        raise WriteFormatError(f"run text longer than {MAX_RUN_TEXT} characters", f"{where}.text")
    clean = sanitize_text(text)
    if clean != text:
        ctx.warn(f"{where}: control or newline characters were removed from the text")
    return Run(clean, _validate_tags(obj.get("tags"), f"{where}.tags"))


def _embed_run(obj: dict, where: str, ctx: _Ctx) -> EmbedRun:
    if not isinstance(obj, dict):
        raise WriteFormatError("embed must be an object", where)
    b64 = obj.get("png_b64")
    if not isinstance(b64, str):
        raise WriteFormatError("embed needs a png_b64 string", f"{where}.png_b64")
    ctx.embeds += 1
    if ctx.embeds > MAX_EMBEDS:
        raise WriteFormatError(f"more than {MAX_EMBEDS} embeds", where)
    # Length BEFORE decoding: a 4/3 expansion of the PNG cap, plus padding.
    if len(b64) > ((MAX_EMBED_PNG_BYTES + 2) // 3) * 4:
        raise WriteFormatError(f"embed picture larger than {MAX_EMBED_PNG_BYTES} bytes", f"{where}.png_b64")
    try:
        png = base64.b64decode(b64.encode("ascii"), validate=True)
    except (ValueError, binascii.Error):
        raise WriteFormatError("png_b64 is not valid base64", f"{where}.png_b64") from None
    if len(png) > MAX_EMBED_PNG_BYTES:
        raise WriteFormatError(f"embed picture larger than {MAX_EMBED_PNG_BYTES} bytes", f"{where}.png_b64")
    ctx.embed_bytes += len(png)
    if ctx.embed_bytes > MAX_TOTAL_EMBED_BYTES:
        raise WriteFormatError(f"embed pictures exceed {MAX_TOTAL_EMBED_BYTES} bytes in total", where)
    try:
        check_png(png)
    except ValueError as exc:
        raise WriteFormatError(str(exc), f"{where}.png_b64") from None
    source_raw = obj.get("source", [])
    if not isinstance(source_raw, list):
        raise WriteFormatError("embed source must be a list of text runs", f"{where}.source")
    if len(source_raw) > MAX_RUNS_PER_PARAGRAPH:
        raise WriteFormatError("too many source runs in an embed", f"{where}.source")
    source = []
    for j, item in enumerate(source_raw):
        item_where = f"{where}.source[{j}]"
        if not isinstance(item, dict):
            raise WriteFormatError("source run must be an object", item_where)
        if "embed" in item:
            raise WriteFormatError("embeds cannot be nested", item_where)
        if "text" not in item:
            raise WriteFormatError("source run needs text", item_where)
        source.append(_text_run(item, item_where, ctx))
    return EmbedRun(png, coalesce_runs(source))


def _validate(data) -> LoadResult:
    ctx = _Ctx()
    if not isinstance(data, dict):
        raise WriteFormatError("top level must be an object", "$")
    version = data.get("version")
    if type(version) is not int:
        raise WriteFormatError("version must be an integer", "$.version")
    if version != FORMAT_VERSION:
        raise WriteFormatError(f"unsupported version {version} (this app reads version {FORMAT_VERSION})", "$.version")
    paragraphs_raw = data.get("paragraphs")
    if not isinstance(paragraphs_raw, list):
        raise WriteFormatError("paragraphs must be a list", "$.paragraphs")
    if len(paragraphs_raw) > MAX_PARAGRAPHS:
        raise WriteFormatError(f"more than {MAX_PARAGRAPHS} paragraphs", "$.paragraphs")
    paragraphs = []
    for i, para in enumerate(paragraphs_raw):
        pw = f"$.paragraphs[{i}]"
        if not isinstance(para, dict):
            raise WriteFormatError("paragraph must be an object", pw)
        indent = para.get("indent", 0)
        if type(indent) is not int:
            raise WriteFormatError("indent must be an integer", f"{pw}.indent")
        if not 0 <= indent <= MAX_INDENT:
            raise WriteFormatError(f"indent must be 0 to {MAX_INDENT}", f"{pw}.indent")
        runs_raw = para.get("runs", [])
        if not isinstance(runs_raw, list):
            raise WriteFormatError("runs must be a list", f"{pw}.runs")
        if len(runs_raw) > MAX_RUNS_PER_PARAGRAPH:
            raise WriteFormatError(f"more than {MAX_RUNS_PER_PARAGRAPH} runs in a paragraph", f"{pw}.runs")
        ctx.total_runs += len(runs_raw)
        if ctx.total_runs > MAX_TOTAL_RUNS:
            raise WriteFormatError(f"more than {MAX_TOTAL_RUNS} runs in the document", "$.paragraphs")
        runs = []
        for j, item in enumerate(runs_raw):
            rw = f"{pw}.runs[{j}]"
            if not isinstance(item, dict):
                raise WriteFormatError("run must be an object", rw)
            has_text = "text" in item
            has_embed = "embed" in item
            if has_text == has_embed:
                raise WriteFormatError("a run needs exactly one of text or embed", rw)
            if has_text:
                runs.append(_text_run(item, rw, ctx))
            else:
                runs.append(_embed_run(item["embed"], f"{rw}.embed", ctx))
        paragraphs.append(Paragraph(indent, coalesce_runs(runs)))
    return LoadResult(WriteDocument(paragraphs, version), ctx.finish())


def parse_write_bytes(data: bytes, max_bytes: int = MAX_FILE_BYTES) -> LoadResult:
    """Parse and validate the bytes of a `.mwrite` file (see the module docstring).

    Raises `WriteFileTooLarge` if `len(data) > max_bytes`, otherwise
    `WriteFormatError` for anything invalid. Never raises any other exception
    for malformed content.
    """
    if len(data) > max_bytes:
        raise WriteFileTooLarge(f"file is larger than {max_bytes} bytes", "$")
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise WriteFormatError(f"file is not valid UTF-8 ({exc.reason})", f"byte {exc.start}") from None
    try:
        parsed = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_no_duplicate_keys)
    except WriteFormatError:
        raise
    except RecursionError:
        raise WriteFormatError("JSON is nested too deeply", "$") from None
    except json.JSONDecodeError as exc:
        raise WriteFormatError(f"not valid JSON: {exc.msg}", f"line {exc.lineno} column {exc.colno}") from None
    except (ValueError, UnicodeError) as exc:
        raise WriteFormatError(f"not valid JSON: {exc}", "$") from None
    return _validate(parsed)


def read_capped(path, max_bytes: int = MAX_FILE_BYTES) -> bytes:
    """Read at most `max_bytes` from `path`; raise `WriteFileTooLarge` if there is more.

    Reads `max_bytes + 1` bytes and checks the length rather than trusting a
    prior `stat` (a file can grow, or lie about its size). Never modifies the
    file. `OSError` propagates.
    """
    with open(path, "rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise WriteFileTooLarge(f"file is larger than {max_bytes} bytes", "$")
    return data


def read_write_file(path, max_bytes: int = MAX_FILE_BYTES) -> LoadResult:
    """Load and validate a `.mwrite` file: `read_capped` then `parse_write_bytes`.

    Returns `LoadResult(document, warnings)`. Raises `WriteFormatError` /
    `WriteFileTooLarge` for bad content and `OSError` for filesystem problems.
    A failed read leaves the file untouched.
    """
    return parse_write_bytes(read_capped(path, max_bytes), max_bytes)


def _text_run_json(run: Run) -> dict:
    obj: dict = {"text": run.text}
    tags = normalize_tags(run.tags)
    if tags:
        obj["tags"] = tags
    return obj


def encode_write(doc: WriteDocument) -> bytes:
    """Serialise a document to the canonical UTF-8 JSON bytes (no size or reload check).

    Runs are coalesced and tags canonicalised first, indents clamped, empty
    `tags` omitted. Raises `WriteFormatError` if the text cannot be encoded.
    """
    paragraphs = []
    for para in doc.paragraphs:
        runs = []
        for run in coalesce_runs(para.runs):
            if isinstance(run, EmbedRun):
                runs.append(
                    {
                        "embed": {
                            "png_b64": base64.b64encode(run.png).decode("ascii"),
                            "source": [_text_run_json(r) for r in coalesce_runs(run.source)],
                        }
                    }
                )
            else:
                runs.append(_text_run_json(run))
        paragraphs.append({"indent": clamp_indent(para.indent), "runs": runs})
    payload = {"version": FORMAT_VERSION, "paragraphs": paragraphs}
    try:
        return (json.dumps(payload, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    except UnicodeEncodeError:
        raise WriteFormatError("document contains text that cannot be saved as UTF-8", "$") from None


def save_write(path, doc: WriteDocument) -> None:
    """Atomically save `doc` to `path` (see the module docstring).

    Raises `WriteFileTooLarge` if the serialised file would exceed
    `MAX_FILE_BYTES`, `WriteFormatError` if it would not load back,
    `IsADirectoryError` for a directory target, and any `OSError` from the
    filesystem - in every failure case the existing file (if any) is left
    exactly as it was and no temp file remains. Documented choice: the temp
    file is created by `mkstemp` (mode 0600 on POSIX); on Windows this has no
    effect. A symlinked target is replaced by the new file, not written through.
    """
    target = Path(path)
    data = encode_write(doc)
    if len(data) > MAX_FILE_BYTES:
        raise WriteFileTooLarge(f"document would be {len(data)} bytes; the limit is {MAX_FILE_BYTES}", "$")
    try:
        parse_write_bytes(data)
    except WriteFormatError as exc:
        raise WriteFormatError(f"refusing to save a file that would not load again: {exc.message}", exc.where) from None
    if target.is_dir():
        raise IsADirectoryError(f"{target} is a directory")
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".write-", suffix=".tmp")
    try:
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
