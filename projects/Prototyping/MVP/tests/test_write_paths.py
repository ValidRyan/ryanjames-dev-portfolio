"""Tests for write_paths.py - the SAP Modifier's reference syntax + path-boundary policy.

Zero Qt import anywhere in this file. Everything on disk lives under `tmp_path`;
junction tests use `_winapi.CreateJunction` (no admin rights) via
`write_testkit.make_dir_link` and skip cleanly where links are unsupported.
Links to UNC/device targets and link loops are SIMULATED by monkeypatching the
module's `_lstat` / `_readlink` / `_realpath` seams, so the suite never touches
a network path and needs no privileges. Printed strings go through `ascii()`.
"""

import os
import stat
import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))

import pytest

import write_paths
import write_testkit
from write_paths import (
    MAX_COMPONENT_LEN,
    MAX_LINK_HOPS,
    MAX_REF_LEN,
    MAX_REF_PARTS,
    SI_EXTENSIONS,
    chain_key,
    check_ref_syntax,
    classify_ref,
    ext_for,
    is_inside,
    real_root,
    relative_ref_for,
    resolve_ref,
    si_kind_for_name,
)

IS_WINDOWS = os.name == "nt"


def _same(a, b):
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def _fake_stat(mode):
    return os.stat_result((mode, 0, 0, 1, 0, 0, 0, 0, 0, 0))


def _fake_links(monkeypatch, links):
    """Make every path whose LAST component is a key of `links` look like a link
    to `links[name]`; everything else uses the real `os.lstat`."""
    real_lstat = os.lstat
    calls = []

    def fake_lstat(path):
        calls.append(str(path))
        if os.path.basename(str(path)) in links:
            return _fake_stat(stat.S_IFLNK | 0o777)
        return real_lstat(path)

    monkeypatch.setattr(write_paths, "_lstat", fake_lstat)
    monkeypatch.setattr(write_paths, "_readlink", lambda p: links[os.path.basename(str(p))])
    return calls


@pytest.fixture
def link_to(request):
    """`link_to(link, target) -> bool`: make a directory link that is removed at teardown."""

    def make(link, target):
        made = write_testkit.make_dir_link(link, target)
        if made:
            request.addfinalizer(lambda: write_testkit.remove_dir_link(link))
        return made

    return make


@pytest.fixture
def world(tmp_path):
    """`tmp/root` (the tree fixture), a sibling-prefix trap `tmp/root2`, `tmp/outside`."""
    root = tmp_path / "root"
    fx = write_testkit.tree_fixture(root)
    (tmp_path / "root2").mkdir()
    (tmp_path / "root2" / "x.txt").write_text("trap", encoding="utf-8")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    return {"tmp": tmp_path, "fx": fx, "root": root, "root_real": real_root(str(root))}


def test_si_extensions_is_the_single_registry():
    assert SI_EXTENSIONS == {".mwrite": "write", ".mstore": "store", ".mdoc": "doc", ".mprog": "prog"}
    for ext, kind in SI_EXTENSIONS.items():
        assert ext_for(kind) == ext
    with pytest.raises(ValueError):
        ext_for("operator")


@pytest.mark.parametrize(
    "name, is_dir, expected",
    [
        ("a.mwrite", False, "write"),
        ("A.MWRITE", False, "write"),
        ("x.Mdoc", False, "doc"),
        ("x.mprog", False, "prog"),
        ("Notes.MStore", True, "store"),
        ("x.mstore", False, None),  # a regular file named like a store is an operator
        ("x.mwrite", True, None),  # a directory named like a Write is an ordinary folder
        ("a.txt", False, None),
        (".mwrite", False, None),  # no stem: not an extension
        ("a.mwrite.txt", False, None),
    ],
)
def test_si_kind_for_name_is_case_insensitive_and_needs_matching_dirness(name, is_dir, expected):
    assert si_kind_for_name(name, is_dir) == expected


@pytest.mark.parametrize(
    "raw, rel",
    [
        ("a.txt", "a.txt"),
        ("sub dir/it's.txt", "sub dir/it's.txt"),
        ("./a", "a"),
        ("a/../b", "a/../b"),
        ("a\\b", "a/b"),
        ("  padded.txt  ", "padded.txt"),
        ("..", ".."),
        ("../x/y.txt", "../x/y.txt"),
        (".", "."),
        ("./", "."),
        (".hidden", ".hidden"),
        ("a. b", "a. b"),
        ("caf\u00e9/na\u00efve.txt", "caf\u00e9/na\u00efve.txt"),
        ("console.txt", "console.txt"),
        ("com10", "com10"),
        ("nulled/aux2", "nulled/aux2"),
        ("x/y ", "x/y"),  # a trailing space on the WHOLE reference is stripped first
    ],
)
def test_syntax_accepts_ordinary_relative_references(raw, rel):
    syn = check_ref_syntax(raw)
    assert syn.ok, ascii((raw, syn))
    assert syn.state == "ok" and syn.reason == ""
    assert syn.rel == rel


@pytest.mark.parametrize(
    "raw",
    [
        "/x",
        "\\x",
        "/",
        "C:\\x",
        "c:/x",
        "C:foo",
        "Z:",
        "\\\\host\\share\\x",
        "//host/share/x",
        "\\\\?\\C:\\x",
        "\\\\.\\pipe\\p",
        "\\\\?\\UNC\\h\\s\\x",
    ],
)
def test_syntax_blocks_absolute_drive_rooted_and_unc_paths(raw):
    syn = check_ref_syntax(raw)
    assert not syn.ok and syn.state == "blocked", ascii((raw, syn))
    assert syn.rel == "" and syn.reason


@pytest.mark.parametrize(
    "raw",
    ["a.txt:stream", "a.txt::$DATA", "a<b", "a>b", 'a"b', "a|b", "a?b", "a*b", "a/b:c", "x/C:foo", "dir/a:b/c"],
)
def test_syntax_blocks_streams_and_illegal_characters(raw):
    syn = check_ref_syntax(raw)
    assert syn.state == "blocked", ascii((raw, syn))


@pytest.mark.parametrize(
    "raw",
    [
        "CON",
        "con",
        "CON.txt",
        "nul.tar.gz",
        "NUL",
        "prn",
        "AUX",
        "aux .txt",
        "COM1",
        "com9.log",
        "COM0",
        "lpt1",
        "LPT9.txt",
        "COM\u00b9",
        "lpt\u00b2.txt",
        "sub/AUX/x",
        "Conin$",
        "CONOUT$.txt",
    ],
)
def test_syntax_blocks_reserved_device_names(raw):
    syn = check_ref_syntax(raw)
    assert syn.state == "blocked" and "reserved" in syn.reason, ascii((raw, syn))


@pytest.mark.parametrize("raw", ["a.", "a /b", "sub./x", "...", "a/b.", "a/b /c"])
def test_syntax_blocks_trailing_dot_or_space_in_a_component(raw):
    syn = check_ref_syntax(raw)
    assert syn.state == "blocked", ascii((raw, syn))
    assert "trailing" in syn.reason


@pytest.mark.parametrize(
    "raw",
    ["a\x00b", "a\x01b", "a\x7fb", "a\u200bb", "a\u202eb", "\ufeffa", "a\nb", "a\tb", "a\ud800b", "a\u2066b"],
)
def test_syntax_blocks_control_format_and_surrogate_characters(raw):
    syn = check_ref_syntax(raw)
    assert syn.state == "blocked", ascii((raw, syn))
    assert not syn.ok


def test_empty_reference_is_broken_not_blocked():
    for raw in ["", "   ", "\t \n"]:
        syn = check_ref_syntax(raw)
        assert syn.state == "broken" and syn.reason == "empty reference" and not syn.ok


def test_syntax_caps_on_length_parts_and_component_size():
    assert check_ref_syntax("a" * (MAX_REF_LEN + 1)).reason == "reference too long"
    # exactly MAX_REF_LEN characters passes the overall cap (this one then trips the component cap)
    assert check_ref_syntax("a" * MAX_REF_LEN).reason == "name too long"
    assert check_ref_syntax("/".join(["a"] * MAX_REF_PARTS)).ok
    assert check_ref_syntax("/".join(["a"] * (MAX_REF_PARTS + 1))).state == "blocked"
    assert check_ref_syntax("b" * MAX_COMPONENT_LEN).ok
    assert check_ref_syntax("b" * (MAX_COMPONENT_LEN + 1)).state == "blocked"
    assert check_ref_syntax(None).state == "blocked"  # not text at all


def test_is_inside_string_only_sibling_prefix_and_edges(tmp_path):
    root = str(tmp_path / "root")
    assert is_inside(root, root)
    assert is_inside(os.path.join(root, "x", "y"), root)
    assert is_inside(root + os.sep, root)
    assert is_inside(os.path.join(root, "a", "..", "b"), root)
    # The sibling-prefix trap: "root2" starts with "root" but is NOT inside it.
    assert not is_inside(root + "2", root)
    assert not is_inside(os.path.join(root + "2", "x"), root)
    assert not is_inside(os.path.dirname(root), root)
    assert not is_inside(os.path.join(root, "..", "root2"), root)
    assert not is_inside("", root) and not is_inside(root, "") and not is_inside(None, root)


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows path semantics")
def test_is_inside_windows_case_extended_prefix_and_cross_drive():
    assert is_inside("C:\\Root\\Sub", "c:\\root")
    assert is_inside("c:/root/sub", "C:\\ROOT")
    assert is_inside("\\\\?\\C:\\Root\\x", "c:\\root")
    assert not is_inside("C:\\root2", "C:\\root")
    # different drives, rooted-without-drive and relative paths are False, never an exception
    assert not is_inside("D:\\root\\x", "C:\\root")
    assert not is_inside("\\root\\x", "C:\\root")
    assert not is_inside("relative\\x", "C:\\root")
    assert not is_inside("\\\\host\\share\\x", "C:\\root")
    assert chain_key("C:\\Root\\A.MWRITE") == chain_key("c:\\root\\a.mwrite")
    assert not real_root(os.getcwd()).startswith("\\\\?\\")


def test_resolve_ok_for_existing_files_and_folders(world):
    fx, base = world["fx"], world["root_real"]
    for raw, expected, is_dir in [
        ("a.txt", fx["a"], False),
        ("./a.txt", fx["a"], False),
        ("sub", fx["sub"], True),
        ("sub/b.txt", fx["sub_b"], False),
        ("sub\\b.txt", fx["sub_b"], False),
        ("sub dir/it's.txt", fx["spaced"], False),
        ("sub/../a.txt", fx["a"], False),
        ("notes.mstore", fx["store"], True),
        ("fake.mstore", fx["fake_store"], False),
        (".", fx["root"], True),
    ]:
        res = resolve_ref(raw, base, base)
        assert res.state == "ok", ascii((raw, res))
        assert res.is_dir is is_dir and res.reason == ""
        assert _same(res.path, expected), ascii((raw, res.path))
        assert chain_key(res.path) == os.path.normcase(res.path)
    nested = str(fx["sub"])
    assert _same(resolve_ref("../a.txt", nested, base).path, fx["a"])
    assert _same(resolve_ref("b.txt", nested, base).path, fx["sub_b"])


def test_resolve_missing_targets_are_broken_not_blocked(world):
    fx, base = world["fx"], world["root_real"]
    for raw in ["missing.txt", "sub/missing/deeper.txt", "a.txt/child", "sub/../missing"]:
        res = resolve_ref(raw, base, base)
        assert res.state == "broken", ascii((raw, res))
        assert res.path is None and res.reason
    assert resolve_ref("", base, base).state == "broken"


def test_resolve_dotdot_escapes_are_blocked(world):
    fx, base = world["fx"], world["root_real"]
    sub = str(fx["sub"])
    for raw, from_dir in [
        ("../outside.txt", base),
        ("..", base),
        ("sub/../../outside.txt", base),
        ("../root2/x.txt", base),  # sibling-prefix trap that really exists
        ("../../a.txt", sub),
        ("../..", sub),
    ]:
        res = resolve_ref(raw, from_dir, base)
        assert res.state == "blocked", ascii((raw, res))
        assert res.path is None
    # A Write outside the root cannot resolve anything.
    assert resolve_ref("a.txt", str(world["tmp"] / "outside"), base).state == "blocked"


def test_resolve_unsaved_is_reported_only_after_the_syntax_check(world):
    base = world["root_real"]
    assert resolve_ref("a.txt", None, None).state == "unsaved"
    assert resolve_ref("a.txt", None, base).state == "unsaved"
    assert resolve_ref("a.txt", base, None).state == "unsaved"
    assert resolve_ref("a.txt", None, None).rel == "a.txt"
    # a hostile reference in an unsaved Write is still BLOCKED, and an empty one broken
    assert resolve_ref("C:\\Windows\\win.ini", None, None).state == "blocked"
    assert resolve_ref("\\\\host\\share\\x", None, None).state == "blocked"
    assert resolve_ref("", None, None).state == "broken"


SYNTAX_BLOCKED = [
    "C:\\Windows\\win.ini",
    "c:foo",
    "\\\\host\\share\\x",
    "\\\\?\\C:\\x",
    "//host/share",
    "/etc/passwd",
    "a.txt:stream",
    "CON",
    "nul.tar.gz",
    "a.",
    "a\x00b",
    "a\u202eb",
    "a" * (MAX_REF_LEN + 1),
]
# Syntactically clean, but lexically outside the root: caught before any I/O too.
LEXICAL_ESCAPES = ["../up.txt", "sub/../../up.txt", "..", "a/../../b"]


def test_blocked_references_perform_no_io_at_all(world, monkeypatch):
    base = world["root_real"]
    fx = world["fx"]
    outside = str(world["tmp"] / "outside")
    # computed BEFORE the seams are booby-trapped (it legitimately reads the disk)
    assert relative_ref_for(str(fx["sub_b"]), base, base)[0] == "sub/b.txt"

    def boom(*args, **kwargs):
        raise AssertionError(f"filesystem touched by a blocked reference: {ascii(args)}")

    monkeypatch.setattr(write_paths, "_lstat", boom)
    monkeypatch.setattr(write_paths, "_readlink", boom)
    monkeypatch.setattr(write_paths, "_realpath", boom)

    for raw in SYNTAX_BLOCKED:
        assert check_ref_syntax(raw).state == "blocked", ascii(raw)
        assert resolve_ref(raw, base, base).state == "blocked", ascii(raw)
        assert classify_ref(raw, base, base) == "blocked"
        assert resolve_ref(raw, None, None).state == "blocked"  # unsaved does not excuse a hostile ref
    for raw in LEXICAL_ESCAPES:
        assert resolve_ref(raw, base, base).state == "blocked", ascii(raw)
    # a Write folder outside the root: blocked before any walk
    assert resolve_ref("a.txt", outside, base).state == "blocked"
    # syntax-only outcomes that are not "blocked" are I/O free too
    assert resolve_ref("", base, base).state == "broken"
    assert resolve_ref("a.txt", None, None).state == "unsaved"
    # relative_ref_for refuses an outside target without touching the disk
    assert relative_ref_for(str(world["tmp"] / "outside.txt"), base, base)[0] is None
    assert relative_ref_for(str(world["tmp"] / "root2" / "x.txt"), base, base)[0] is None


def test_junction_inside_the_root_is_followed_safely(world, link_to):
    fx, base = world["fx"], world["root_real"]
    real_dir = world["root"] / "real"
    real_dir.mkdir()
    (real_dir / "inner.txt").write_text("inner", encoding="utf-8")
    if not link_to(world["root"] / "alias", real_dir):
        pytest.skip("directory links are not supported here")
    res = resolve_ref("alias/inner.txt", base, base)
    assert res.state == "ok", ascii(res)
    assert _same(res.path, real_dir / "inner.txt")
    folder = resolve_ref("alias", base, base)
    assert folder.state == "ok" and folder.is_dir and _same(folder.path, real_dir)
    # `..` after a link is taken from the link TARGET, then re-checked against the root
    assert _same(resolve_ref("alias/../a.txt", base, base).path, fx["a"])
    # the resolved path is the link-free one, which is what cycle detection keys on
    assert chain_key(res.path) == chain_key(str(real_dir / "inner.txt"))


def test_links_with_in_root_targets_are_spliced_in_and_others_blocked(world, monkeypatch):
    fx, base = world["fx"], world["root_real"]
    _fake_links(
        monkeypatch,
        {
            "abs_in": os.path.join(base, "sub"),
            "rel_in": "sub",
            "abs_out": os.path.join(os.path.dirname(base), "outside"),
            "rel_out": "..",
            "rel_deep_out": "../../..",
            "drive_rel": "C:relative" if IS_WINDOWS else "/nonexistent-root-elsewhere",
        },
    )
    assert _same(resolve_ref("abs_in/b.txt", base, base).path, fx["sub_b"])
    assert _same(resolve_ref("rel_in/b.txt", base, base).path, fx["sub_b"])
    for raw in ["abs_out/secret.txt", "rel_out/outside.txt", "rel_deep_out/x", "drive_rel/x"]:
        res = resolve_ref(raw, base, base)
        assert res.state == "blocked", ascii((raw, res))


def test_junction_pointing_outside_the_root_is_blocked(world, link_to):
    base = world["root_real"]
    if not link_to(world["root"] / "escape", world["tmp"] / "outside"):
        pytest.skip("directory links are not supported here")
    for raw in ["escape/secret.txt", "escape"]:
        res = resolve_ref(raw, base, base)
        assert res.state == "blocked", ascii((raw, res))
        assert "link" in res.reason and res.path is None
    # ...and a sibling-prefix junction target ("root2") is outside too
    if link_to(world["root"] / "trap", world["tmp"] / "root2"):
        assert resolve_ref("trap/x.txt", base, base).state == "blocked"


@pytest.mark.parametrize(
    "target",
    [
        "\\\\evil-host\\share\\x",
        "//evil-host/share/x",
        "\\\\?\\UNC\\evil-host\\share\\x",
        "\\??\\UNC\\evil-host\\share\\x",
        "\\\\.\\pipe\\evil",
        "\\\\?\\GLOBALROOT\\Device\\x",
        "\\??\\Volume{00000000-0000-0000-0000-000000000000}\\x",
    ],
)
def test_link_to_a_unc_or_device_target_is_blocked_without_following(world, monkeypatch, target):
    base = world["root_real"]
    calls = _fake_links(monkeypatch, {"netlink": target})

    def no_realpath(*args, **kwargs):
        raise AssertionError("realpath reached for a blocked link")

    monkeypatch.setattr(write_paths, "_realpath", no_realpath)
    res = resolve_ref("netlink/payload.txt", base, base)
    assert res.state == "blocked", ascii((target, res))
    assert "link" in res.reason
    # Only the link itself was ever lstat-ed: nothing beyond it, and never a UNC-looking path.
    assert [os.path.basename(c) for c in calls] == ["netlink"]
    assert not any(c.startswith(("\\\\", "//")) for c in calls)


def test_link_loops_and_the_hop_cap_are_broken(world, monkeypatch):
    fx, base = world["fx"], world["root_real"]
    links = {"loop": "loop"}
    for i in range(MAX_LINK_HOPS + 1):
        links[f"h{i}"] = f"h{i + 1}" if i < MAX_LINK_HOPS else "sub"
    # h0 -> h1 -> ... -> h{MAX} -> sub : MAX+1 hops in total, one over the cap
    _fake_links(monkeypatch, links)
    res = resolve_ref("loop/x", base, base)
    assert res.state == "broken" and "too many links" in res.reason
    assert resolve_ref("h0/b.txt", base, base).state == "broken"
    # exactly MAX_LINK_HOPS hops is fine: h1 -> ... -> h{MAX} -> sub is MAX hops
    ok = resolve_ref("h1/b.txt", base, base)
    assert ok.state == "ok", ascii(ok)
    assert _same(ok.path, fx["sub_b"])


def test_relative_ref_for_builds_only_safe_in_root_references(world):
    fx, base = world["fx"], world["root_real"]
    assert relative_ref_for(str(fx["sub_b"]), base, base) == ("sub/b.txt", "")
    assert relative_ref_for(str(fx["spaced"]), base, base) == ("sub dir/it's.txt", "")
    assert relative_ref_for(str(fx["a"]), str(fx["sub"]), base) == ("../a.txt", "")
    assert relative_ref_for(str(fx["store"]), base, base) == ("notes.mstore", "")
    # every produced reference is accepted verbatim by the resolver
    for target, from_dir in [(fx["sub_b"], base), (fx["a"], str(fx["sub"])), (fx["spaced"], base)]:
        ref = relative_ref_for(str(target), from_dir, base)[0]
        assert resolve_ref(ref, from_dir, base).state == "ok"
    # outside the root, the sibling-prefix trap and an unsaved Write are refused with a reason
    for target, from_dir, root in [
        (world["tmp"] / "outside.txt", base, base),
        (world["tmp"] / "root2" / "x.txt", base, base),
        (fx["a"], None, base),
        (fx["a"], base, None),
        (fx["a"], str(world["tmp"] / "outside"), base),
    ]:
        ref, reason = relative_ref_for(str(target), from_dir, root)
        assert ref is None and reason, ascii((target, reason))
    # a missing but in-root target is still expressible (with a warning reason)
    ref, reason = relative_ref_for(str(world["root"] / "not_yet.txt"), base, base)
    assert ref == "not_yet.txt" and reason


def test_classify_ref_matches_resolve_ref_state(world):
    fx, base = world["fx"], world["root_real"]
    cases = [
        ("a.txt", base, base),
        ("missing.txt", base, base),
        ("../outside.txt", base, base),
        ("C:\\x", base, base),
        ("", base, base),
        ("a.txt", None, None),
        ("sub/b.txt", base, base),
        ("CON", base, base),
        ("../a.txt", str(fx["sub"]), base),
    ]
    seen = set()
    for raw, from_dir, root in cases:
        state = classify_ref(raw, from_dir, root)
        assert state == resolve_ref(raw, from_dir, root).state, ascii(raw)
        seen.add(state)
    assert seen == {"ok", "broken", "blocked", "unsaved"}
