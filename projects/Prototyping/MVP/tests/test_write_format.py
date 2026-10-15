"""Tests for write_format.py - the strict `.mwrite` loader and the atomic saver.

Zero Qt import anywhere in this file. Every file lives under `tmp_path`. The
"rejected before decoding" tests booby-trap `base64.b64decode` to prove the
size check really comes first. Source stays ASCII (escapes / `\\N{...}`
names) and printed strings go through `ascii()`.
"""

import base64
import json
import os
import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))

import pytest

import write_format
import write_testkit
from write_format import (
    LoadResult,
    WriteFileTooLarge,
    WriteFormatError,
    check_png,
    encode_write,
    parse_write_bytes,
    png_dimensions,
    read_write_file,
    save_write,
)
from write_model import EmbedRun, Paragraph, Run, WriteDocument

PNG = write_testkit.fake_png_header(12, 8)


def _b(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _doc(*paragraphs, **top):
    return {"version": 1, "paragraphs": list(paragraphs), **top}


def _para(*runs, indent=0):
    return {"indent": indent, "runs": list(runs)}


def _t(text, **tags):
    run = {"text": text}
    if tags:
        run["tags"] = tags
    return run


def _embed(png=PNG, source=None):
    return {"embed": {"png_b64": base64.b64encode(png).decode("ascii"), "source": source if source is not None else [{"text": "src"}]}}


def _expect_error(data, where=None, exc=WriteFormatError):
    with pytest.raises(exc) as info:
        parse_write_bytes(data if isinstance(data, bytes) else _b(data))
    if where is not None:
        assert where in info.value.where, ascii((info.value.where, str(info.value)))
    return info.value


def _rich_document():
    return WriteDocument(
        [
            Paragraph(0, [Run("Phrase: ", {"bold": True}), Run("@sub/a.txt")]),
            Paragraph(1, [Run("plain "), Run("lean", {"italic": "left", "spread": 2.5}), Run(" x", {"line": True, "dash": True})]),
            Paragraph(2, [EmbedRun(PNG, [Run("Goals: x", {"italic": "right"})])]),
            Paragraph(0, []),
            Paragraph(3, [Run("caf\u00e9 "), EmbedRun(PNG, [Run("emb")]), Run("tail", {"bold": True})]),
        ]
    )


def test_round_trip_preserves_tags_indents_and_embeds(tmp_path):
    doc = _rich_document()
    target = tmp_path / "doc.mwrite"
    save_write(target, doc)
    loaded = read_write_file(target)
    assert isinstance(loaded, LoadResult)
    assert loaded.document == doc
    assert loaded.warnings == []
    document, warnings = loaded  # documented: also unpackable
    assert document is loaded.document and warnings is loaded.warnings
    assert loaded.document.outline() == doc.outline()
    # saving the loaded document again is byte-for-byte stable
    again = tmp_path / "again.mwrite"
    save_write(again, loaded.document)
    assert again.read_bytes() == target.read_bytes()


def test_saved_json_has_the_documented_shape(tmp_path):
    target = tmp_path / "shape.mwrite"
    save_write(target, _rich_document())
    raw = target.read_bytes()
    assert raw.endswith(b"\n")
    assert "caf\u00e9".encode("utf-8") in raw  # readable UTF-8, not \u escapes
    data = json.loads(raw.decode("utf-8"))
    assert data["version"] == 1 and len(data["paragraphs"]) == 5
    first, second, third, empty, fifth = data["paragraphs"]
    assert first == {"indent": 0, "runs": [{"text": "Phrase: ", "tags": {"bold": True}}, {"text": "@sub/a.txt"}]}
    assert "tags" not in first["runs"][1]  # empty tags are omitted
    assert second["runs"][1]["tags"] == {"italic": "left", "spread": 2.5}
    assert second["runs"][2]["tags"] == {"line": True, "dash": True}
    assert empty == {"indent": 0, "runs": []}
    assert third["indent"] == 2
    embed = third["runs"][0]["embed"]
    assert base64.b64decode(embed["png_b64"]) == PNG
    assert embed["source"] == [{"text": "Goals: x", "tags": {"italic": "right"}}]
    assert fifth["runs"][1]["embed"]["source"] == [{"text": "emb"}]


def test_atomic_save_replaces_in_place_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "atomic.mwrite"
    save_write(target, WriteDocument([Paragraph(0, [Run("first")])]))
    first = target.read_bytes()
    save_write(target, WriteDocument([Paragraph(0, [Run("second")])]))
    assert target.read_bytes() != first
    assert read_write_file(target).document.outline() == [(0, "second")]
    assert [p.name for p in tmp_path.iterdir()] == ["atomic.mwrite"]


def test_failed_save_keeps_the_original_and_removes_the_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "keep.mwrite"
    save_write(target, WriteDocument([Paragraph(0, [Run("original")])]))
    before = target.read_bytes()
    new_doc = WriteDocument([Paragraph(0, [Run("replacement")])])

    def broken_replace(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError, match="simulated"):
        save_write(target, new_doc)
    monkeypatch.undo()
    assert target.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["keep.mwrite"]

    # a failure while writing (fsync) is cleaned up the same way
    def broken_fsync(fd):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    with pytest.raises(OSError, match="fsync"):
        save_write(target, new_doc)
    monkeypatch.undo()
    assert target.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["keep.mwrite"]


def test_save_refuses_oversize_directory_and_unloadable_documents(tmp_path, monkeypatch):
    target = tmp_path / "guard.mwrite"
    save_write(target, WriteDocument([Paragraph(0, [Run("kept")])]))
    before = target.read_bytes()
    # over MAX_FILE_BYTES
    monkeypatch.setattr(write_format, "MAX_FILE_BYTES", 100)
    with pytest.raises(WriteFileTooLarge):
        save_write(target, WriteDocument([Paragraph(0, [Run("x" * 500)])]))
    monkeypatch.undo()
    # a directory target
    folder = tmp_path / "a_folder"
    folder.mkdir()
    with pytest.raises(IsADirectoryError):
        save_write(folder, WriteDocument())
    # a document the strict loader would reject (more embeds than the cap) is refused up front
    too_many = WriteDocument([Paragraph(0, [EmbedRun(PNG, []) for _ in range(write_format.MAX_EMBEDS + 1)])])
    with pytest.raises(WriteFormatError, match="would not load again"):
        save_write(target, too_many)
    # text that cannot be encoded as UTF-8 (a lone surrogate) is a format error, not a crash
    with pytest.raises(WriteFormatError):
        save_write(target, WriteDocument([Paragraph(0, [Run("bad\ud800")])]))
    assert target.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a_folder", "guard.mwrite"]
    assert not (tmp_path / "never.mwrite").exists()
    with pytest.raises(WriteFormatError):
        save_write(tmp_path / "never.mwrite", too_many)
    assert not (tmp_path / "never.mwrite").exists()


@pytest.mark.parametrize(
    "data, where",
    [
        ({"version": 2, "paragraphs": []}, "$.version"),
        ({"version": 0, "paragraphs": []}, "$.version"),
        ({"version": True, "paragraphs": []}, "$.version"),  # bool is not an int
        ({"version": 1.0, "paragraphs": []}, "$.version"),
        ({"version": "1", "paragraphs": []}, "$.version"),
        ({"paragraphs": []}, "$.version"),
        ({"version": 1}, "$.paragraphs"),
        ({"version": 1, "paragraphs": {}}, "$.paragraphs"),
        ([], "$"),
        ("text", "$"),
        (None, "$"),
        (7, "$"),
    ],
)
def test_load_rejects_unknown_version_and_wrong_top_level(data, where):
    err = _expect_error(data, where)
    assert isinstance(err, ValueError) and err.message


def test_load_rejects_malformed_json_bytes():
    valid = _b(_doc(_para(_t("x"))))
    _expect_error(valid[: len(valid) // 2], "line")  # truncated
    _expect_error(b"", "line")
    _expect_error(b"\xff\xfe{}", "byte")  # not UTF-8
    _expect_error(b"\xef\xbb\xbf" + valid)  # a BOM is not accepted
    _expect_error(b'{"version": 1, "paragraphs": [], "x": NaN}')
    _expect_error(b'{"version": 1, "paragraphs": [], "x": Infinity}')
    _expect_error(b'{"version": 1, "paragraphs": [], "x": -Infinity}')
    _expect_error(b'{"version": 1, "version": 1, "paragraphs": []}')  # duplicate key
    _expect_error(b'{"version": 1, "paragraphs": [], } ')
    _expect_error(b"{'version': 1}")
    _expect_error(b"[1, 2, 3]")
    _expect_error(b'{"version": 1, "paragraphs": [' + b"9" * 6000 + b"]}")  # int too long to parse


def test_load_rejects_oversize_before_parsing(tmp_path):
    data = _b(_doc(_para(_t("x" * 200))))
    target = tmp_path / "big.mwrite"
    target.write_bytes(data)
    with pytest.raises(WriteFileTooLarge) as info:
        read_write_file(target, max_bytes=len(data) - 1)
    assert isinstance(info.value, WriteFormatError)  # callers may catch either
    assert read_write_file(target, max_bytes=len(data)).document.outline() == [(0, "x" * 200)]
    with pytest.raises(WriteFileTooLarge):
        parse_write_bytes(data, max_bytes=10)
    # the cap is enforced by reading max_bytes+1, not by trusting a stat: a lying stat changes nothing
    real_stat = os.stat

    def lying_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid, 5) + tuple(st)[7:10])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "stat", lying_stat)
        with pytest.raises(WriteFileTooLarge):
            read_write_file(target, max_bytes=len(data) - 1)


@pytest.mark.parametrize(
    "paragraph, where",
    [
        ({"indent": True, "runs": []}, ".indent"),  # bool for int
        ({"indent": "1", "runs": []}, ".indent"),
        ({"indent": 1.0, "runs": []}, ".indent"),
        ({"indent": None, "runs": []}, ".indent"),
        ({"indent": 0, "runs": {}}, ".runs"),
        ({"indent": 0, "runs": "text"}, ".runs"),
        ("paragraph", "$.paragraphs[0]"),
        ({"indent": 0, "runs": ["text"]}, ".runs[0]"),
        ({"indent": 0, "runs": [{"text": 5}]}, ".text"),
        ({"indent": 0, "runs": [{"text": None}]}, ".text"),
        ({"indent": 0, "runs": [{"text": "x", "tags": ["bold"]}]}, ".tags"),
        ({"indent": 0, "runs": [{"text": "x", "tags": {"bold": "yes"}}]}, ".tags.bold"),
        ({"indent": 0, "runs": [{"text": "x", "tags": {"bold": 1}}]}, ".tags.bold"),
        ({"indent": 0, "runs": [{"text": "x", "tags": {"line": None}}]}, ".tags.line"),
        ({"indent": 0, "runs": [{"text": "x", "tags": {"italic": "up"}}]}, ".tags.italic"),
        ({"indent": 0, "runs": [{"text": "x", "tags": {"italic": False}}]}, ".tags.italic"),
        ({"indent": 0, "runs": [{"text": "x", "tags": {"spread": "2"}}]}, ".tags.spread"),
        ({"indent": 0, "runs": [{"text": "x", "tags": {"spread": True}}]}, ".tags.spread"),
    ],
)
def test_load_rejects_wrong_types_including_bool_for_int(paragraph, where):
    err = _expect_error(_doc(paragraph), where)
    assert err.where.startswith("$.paragraphs[0]")


def test_load_rejects_bad_ranges():
    _expect_error(_doc(_para(indent=-1)), ".indent")
    _expect_error(_doc(_para(indent=33)), ".indent")
    assert parse_write_bytes(_b(_doc(_para(indent=32)))).document.paragraphs[0].indent == 32
    _expect_error(_doc(_para(_t("x", spread=32.5))), ".spread")
    _expect_error(_doc(_para(_t("x", spread=-3.5))), ".spread")
    assert parse_write_bytes(_b(_doc(_para(_t("x", spread=32))))).document.paragraphs[0].runs[0].tags == {"spread": 32.0}
    assert parse_write_bytes(_b(_doc(_para(_t("x", spread=-3))))).document.paragraphs[0].runs[0].tags == {"spread": -3.0}
    _expect_error(b'{"version": 1, "paragraphs": [{"runs": [{"text": "x", "tags": {"spread": 1e999}}]}]}', ".spread")


def test_load_enforces_count_caps(monkeypatch):
    monkeypatch.setattr(write_format, "MAX_PARAGRAPHS", 2)
    parse_write_bytes(_b(_doc(_para(), _para())))
    _expect_error(_doc(_para(), _para(), _para()), "$.paragraphs")
    monkeypatch.undo()

    monkeypatch.setattr(write_format, "MAX_RUNS_PER_PARAGRAPH", 2)
    parse_write_bytes(_b(_doc(_para(_t("a"), _t("b", bold=True)))))
    _expect_error(_doc(_para(_t("a"), _t("b"), _t("c"))), ".runs")
    monkeypatch.undo()

    monkeypatch.setattr(write_format, "MAX_TOTAL_RUNS", 3)
    parse_write_bytes(_b(_doc(_para(_t("a"), _t("b")), _para(_t("c")))))
    _expect_error(_doc(_para(_t("a"), _t("b")), _para(_t("c"), _t("d"))))
    monkeypatch.undo()

    monkeypatch.setattr(write_format, "MAX_RUN_TEXT", 5)
    parse_write_bytes(_b(_doc(_para(_t("12345")))))
    _expect_error(_doc(_para(_t("123456"))), ".text")
    monkeypatch.undo()

    monkeypatch.setattr(write_format, "MAX_EMBEDS", 2)
    parse_write_bytes(_b(_doc(_para(_embed(), _embed()))))
    _expect_error(_doc(_para(_embed()), _para(_embed(), _embed())))
    monkeypatch.undo()

    monkeypatch.setattr(write_format, "MAX_TOTAL_EMBED_BYTES", len(PNG) * 2)
    parse_write_bytes(_b(_doc(_para(_embed(), _embed()))))
    _expect_error(_doc(_para(_embed(), _embed(), _embed())))


def test_load_rejects_ambiguous_and_nested_embeds():
    _expect_error(_doc(_para({"text": "x", **_embed()})), ".runs[0]")  # both text and embed
    _expect_error(_doc(_para({})), ".runs[0]")  # neither
    _expect_error(_doc(_para({"tags": {"bold": True}})), ".runs[0]")
    _expect_error(_doc(_para(_embed(source=[_embed()]))), ".source[0]")  # nested embed
    _expect_error(_doc(_para({"embed": "not an object"})), ".embed")
    _expect_error(_doc(_para({"embed": {"source": []}})), "png_b64")
    _expect_error(_doc(_para({"embed": {"png_b64": 5, "source": []}})), "png_b64")
    _expect_error(_doc(_para({"embed": {"png_b64": base64.b64encode(PNG).decode(), "source": "text"}})), ".source")
    _expect_error(_doc(_para({"embed": {"png_b64": base64.b64encode(PNG).decode(), "source": [{"tags": {}}]}})), ".source[0]")
    _expect_error(_doc(_para({"embed": {"png_b64": base64.b64encode(PNG).decode(), "source": ["x"]}})), ".source[0]")
    ok = parse_write_bytes(_b(_doc(_para(_embed(source=[{"text": "a"}, {"text": "b", "tags": {"bold": True}}]))))).document
    assert ok.paragraphs[0].runs[0].source == [Run("a"), Run("b", {"bold": True})]
    assert ok.outline() == [(0, "ab")]


def test_bad_base64_and_oversize_pictures_are_refused_before_decoding(monkeypatch):
    for bad in ["!!!not base64!!!", "AAAA AAAA", "AAAA\nAAAA", "AAA", "caf\u00e9AAA=", "="]:
        _expect_error(_doc(_para({"embed": {"png_b64": bad, "source": []}})), "png_b64")

    def never_decode(*args, **kwargs):
        raise AssertionError("decoded an oversize picture")

    monkeypatch.setattr(write_format.base64, "b64decode", never_decode)
    limit = ((write_format.MAX_EMBED_PNG_BYTES + 2) // 3) * 4
    oversize = "A" * (limit + 4)
    err = _expect_error(_doc(_para({"embed": {"png_b64": oversize, "source": []}})), "png_b64")
    assert "larger than" in err.message
    monkeypatch.undo()
    # decoded size is checked too (a valid-looking string whose PNG exceeds the cap)
    monkeypatch.setattr(write_format, "MAX_EMBED_PNG_BYTES", len(PNG) - 1)
    _expect_error(_doc(_para(_embed())), "png_b64")


def test_png_dimension_bombs_and_bad_signatures_are_refused():
    assert png_dimensions(write_testkit.fake_png_header(7, 9)) == (7, 9)
    assert check_png(write_testkit.fake_png_header(2000, 2000)) == (2000, 2000)  # exactly 4,000,000 px
    for w, h in [(100000, 100000), (4096, 4096), (4097, 1), (1, 4097), (2001, 2000)]:
        with pytest.raises(ValueError):
            check_png(write_testkit.fake_png_header(w, h))
        _expect_error(_doc(_para(_embed(png=write_testkit.fake_png_header(w, h)))), "png_b64")
    for bad in (b"", b"not a png at all" * 5, PNG[:20], b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, write_testkit.fake_png_header(0, 5)):
        with pytest.raises(ValueError):
            png_dimensions(bad)
    tampered = bytearray(PNG)
    tampered[12:16] = b"IDAT"  # first chunk is not IHDR
    with pytest.raises(ValueError):
        png_dimensions(bytes(tampered))
    _expect_error(_doc(_para(_embed(png=b"GIF89a" + b"\x00" * 40))), "png_b64")  # bad signature


def test_unknown_tags_and_extra_keys_are_ignored():
    data = _doc(
        _para(_t("a", bold=True, sparkle=3, italic="right"), {"text": "b", "future": [1, 2]}, indent=1),
        generator="someone else",
    )
    data["paragraphs"][0]["note"] = "extra"
    loaded = parse_write_bytes(_b(data))
    assert loaded.document.paragraphs[0].indent == 1
    assert loaded.document.paragraphs[0].runs == [Run("a", {"bold": True, "italic": "right"}), Run("b", {})]
    assert loaded.warnings == []
    # zero paragraphs load as one empty paragraph; a missing indent/runs is an empty top-level line
    assert parse_write_bytes(_b(_doc())).document.outline() == [(0, "")]
    assert parse_write_bytes(_b(_doc({}))).document.outline() == [(0, "")]


def test_control_characters_are_scrubbed_with_a_warning():
    loaded = parse_write_bytes(_b(_doc(_para(_t("a\x00b"), _t("line1\nline2", bold=True), _t("clean")))))
    runs = loaded.document.paragraphs[0].runs
    assert [r.text for r in runs] == ["ab", "line1 line2", "clean"]
    assert len(loaded.warnings) == 2 and all("$.paragraphs[0].runs[" in w for w in loaded.warnings)
    embed_loaded = parse_write_bytes(_b(_doc(_para(_embed(source=[{"text": "s\x01rc"}])))))
    assert embed_loaded.document.outline() == [(0, "src")] and len(embed_loaded.warnings) == 1
    # warnings are capped, with a summary for the rest
    many = parse_write_bytes(_b(_doc(*[_para(_t("x\x00")) for _ in range(500)])))
    assert len(many.warnings) == 101 and "suppressed" in many.warnings[-1]
    assert many.document.outline()[0] == (0, "x")


def test_deep_nesting_is_a_format_error_and_a_failed_read_never_modifies_the_file(tmp_path):
    err = _expect_error(b"[" * 200_000)
    assert "deeply" in err.message
    _expect_error(b'{"version": 1, "paragraphs": ' + b"[" * 100_000 + b"]" * 100_000 + b"}")

    bad = tmp_path / "bad.mwrite"
    bad.write_bytes(b'{"version": 99, "paragraphs": []}')
    before = (bad.read_bytes(), bad.stat().st_mtime_ns)
    with pytest.raises(WriteFormatError):
        read_write_file(bad)
    assert (bad.read_bytes(), bad.stat().st_mtime_ns) == before
    # a missing file or a directory is an OSError, not a format error
    with pytest.raises(OSError) as info:
        read_write_file(tmp_path / "missing.mwrite")
    assert not isinstance(info.value, WriteFormatError)
    with pytest.raises(OSError):
        read_write_file(tmp_path)
    # encode_write on its own never touches the disk and is deterministic
    assert encode_write(_rich_document()) == encode_write(_rich_document())
