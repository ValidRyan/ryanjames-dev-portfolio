"""Reference syntax, path-boundary policy and the safe resolver for SAP Modifier.

This is the security core of the SAP Modifier: every `@relative/path` typed into a
Write goes through here before anything on disk is touched. Zero Qt (and no
other third-party) dependency - `test_write_paths.py` never imports PySide6.

Policy (the "folder is the boundary" decision): a reference must be a
RELATIVE path that resolves to something inside the root Write's folder.
Absolute paths, drive-letter paths, UNC paths (`\\\\host\\share`, `\\\\?\\`,
`\\\\.\\pipe`), alternate data streams, reserved device names and any `..`
that climbs out of the root are BLOCKED, and a blocked reference performs no
filesystem access at all: `check_ref_syntax` is a pure string function and
`resolve_ref` runs it first. Blocked UNC paths matter most - merely `stat`-ing
one makes Windows try to authenticate to the host over SMB.

Three rules the code is built around (each one is a test):
- `os.path.isabs` is NEVER trusted (it is False for `\\\\host\\share`, `/foo`
  and `C:foo`, all of which escape), and `os.path.join` is never called with an
  untrusted string before the syntactic check (`join(root, "/x")` -> `C:/x`).
- The filesystem walk is "openat"-style: one component at a time, `lstat` each,
  and a link (symlink OR NTFS junction) is never followed by the OS - its
  target is read, vetted (UNC/device -> blocked without contacting it; outside
  the root -> blocked) and spliced back into the pending components.
- Containment is decided on strings (`is_inside`: normcase + normpath +
  commonpath), so the sibling-prefix trap (`root2` vs `root`) and cross-drive
  paths are classified without any I/O.

The three module-level seams `_lstat`, `_readlink` and `_realpath` exist so the
tests can prove "blocked means no I/O" (they monkeypatch the seams to raise)
and can simulate a link to a UNC target without needing privileges.

`SI_EXTENSIONS` (the special-instruction extension registry) lives here and
ONLY here - `test_write_static.py` fails if any other SAP Modifier module spells an
SI extension as a literal. Other modules use `ext_for()` / `si_kind_for_name()`.

Explicitly out of scope: reading file contents (`write_format.py` /
`write_resolve.py` do that), and any write to disk. Known residuals, accepted
and documented: a TOCTOU window between this check and the later read, a root
folder that itself lives on a network share, and hard links (which are
indistinguishable from the file they alias).
"""

import os
import re
import stat
import unicodedata
from collections import deque
from dataclasses import dataclass

# The single registry of special-instruction (SI) file kinds. Everything else
# in the app derives from this dict (file-dialog filters, the resolver's kind
# classification, the highlighter) - never spell these literals elsewhere.
SI_EXTENSIONS = {".mwrite": "write", ".mstore": "store", ".mdoc": "doc", ".mprog": "prog"}

MAX_REF_LEN = 1024
MAX_REF_PARTS = 64
MAX_COMPONENT_LEN = 255
MAX_LINK_HOPS = 16

_EXT_BY_KIND = {kind: ext for ext, kind in SI_EXTENSIONS.items()}

# Windows reserved device stems (checked case-insensitively, on the part of a
# component before its first dot, so `CON.txt` and `nul.tar.gz` are covered).
# COM/LPT also accept the superscript digits on current Windows builds.
_SUPERSCRIPTS = "\u00b9\u00b2\u00b3"
_RESERVED_STEMS = frozenset(
    ["con", "prn", "aux", "nul", "conin$", "conout$"]
    + [f"com{c}" for c in "0123456789" + _SUPERSCRIPTS]
    + [f"lpt{c}" for c in "0123456789" + _SUPERSCRIPTS]
)

# `:` opens alternate data streams (`a.txt:s`) and drive specs (`C:foo`); the
# rest are illegal in Windows file names. Checked on the whole reference.
_ILLEGAL_CHARS = frozenset('<>:"|?*')
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# Categories refused anywhere in a reference: Cc (NUL, controls), Cf (zero
# width, bidi overrides that could disguise a path in the UI) and Cs (a lone
# surrogate cannot be encoded for the OS call and would raise, not fail).
_BLOCKED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})

# Test seams - see the module docstring. Always call through the module
# globals (never a local alias) so monkeypatching takes effect.
_lstat = os.lstat
_readlink = os.readlink
_realpath = os.path.realpath

_REPARSE_MOUNT_POINT = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def ext_for(kind: str) -> str:
    """Return the file extension (with the dot) registered for an SI kind.

    Raises `ValueError` for a kind that is not in `SI_EXTENSIONS`. This is how
    every other module refers to an extension (file-dialog filters, default
    suffix on Save As) so the registry stays the single source of truth.
    """
    try:
        return _EXT_BY_KIND[kind]
    except KeyError:
        raise ValueError(f"unknown special-instruction kind: {kind!r}") from None


def si_kind_for_name(name: str, is_dir: bool) -> str | None:
    """Classify a file or folder name as an SI kind ("write"/"store"/"doc"/"prog") or None.

    Extension match is case-insensitive and uses `os.path.splitext`, so a bare
    `.mwrite` (no stem) is not an SI. Documented choice: the directory-ness
    must agree with the kind - only a DIRECTORY can be a "store" (a regular
    file named `x.mstore` is an operator) and only a FILE can be write/doc/prog
    (a directory named `x.mwrite` is an ordinary folder).
    """
    kind = SI_EXTENSIONS.get(os.path.splitext(name)[1].lower())
    if kind is None:
        return None
    if (kind == "store") != bool(is_dir):
        return None
    return kind


@dataclass(frozen=True)
class RefSyntax:
    """Result of the pure-string check. `state` is "ok", "blocked" or "broken".

    `rel` is the normalised forward-slash relative path (`..` kept, `.` and
    empty parts dropped; "." for a reference to the folder itself) and is ""
    unless `ok`. `reason` is "" when ok.
    """

    ok: bool
    rel: str
    reason: str
    state: str


def _syntax_fail(state: str, reason: str) -> RefSyntax:
    return RefSyntax(False, "", reason, state)


def _component_problem(comp: str) -> str | None:
    """Return why a single non-`..` path component is unacceptable, or None."""
    if comp[-1] in (".", " "):
        return "trailing dot or space in a name"
    if comp.split(".")[0].rstrip(" ").casefold() in _RESERVED_STEMS:
        return "reserved device name"
    if len(comp) > MAX_COMPONENT_LEN:
        return "name too long"
    return None


def check_ref_syntax(raw: str) -> RefSyntax:
    """Classify a raw reference string with ZERO filesystem access.

    Order matters and follows the plan: strip; empty -> broken; too long;
    control/format/surrogate characters; backslash -> slash; `//` -> unc;
    leading `/` -> absolute; `X:` -> drive; split (dropping empty and `.`);
    illegal characters (incl. every `:`); per-component reserved names,
    trailing dot/space and length; component count. Only a syntactically clean,
    relative reference is `ok` - and even then it is not yet known to be
    inside the root (that needs `resolve_ref`).
    """
    if not isinstance(raw, str):
        return _syntax_fail("blocked", "reference is not text")
    text = raw.strip()
    if not text:
        return _syntax_fail("broken", "empty reference")
    if len(text) > MAX_REF_LEN:
        return _syntax_fail("blocked", "reference too long")
    for ch in text:
        if unicodedata.category(ch) in _BLOCKED_CATEGORIES:
            return _syntax_fail("blocked", "control or invisible character in reference")
    text = text.replace("\\", "/")
    if text.startswith("//"):
        return _syntax_fail("blocked", "unc/network path")
    if text.startswith("/"):
        return _syntax_fail("blocked", "absolute path")
    if _DRIVE_RE.match(text):
        return _syntax_fail("blocked", "drive letter path")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if any(ch in _ILLEGAL_CHARS for ch in text):
        return _syntax_fail("blocked", "illegal character (':' and <>\"|?* are not allowed)")
    if len(parts) > MAX_REF_PARTS:
        return _syntax_fail("blocked", "too many path components")
    for comp in parts:
        if comp == "..":
            continue
        problem = _component_problem(comp)
        if problem:
            return _syntax_fail("blocked", problem)
    return RefSyntax(True, "/".join(parts) if parts else ".", "", "ok")


def _strip_extended(path: str) -> str:
    """Drop a Windows extended-length / NT-object prefix from a drive or UNC path.

    `\\\\?\\C:\\x` and `\\??\\C:\\x` become `C:\\x`; `\\\\?\\UNC\\h\\s` becomes
    `\\\\h\\s`. Any OTHER `\\\\?\\`/`\\??\\` form (volume GUIDs, device paths) is
    left alone on purpose so it later reads as a device/rooted path and is
    blocked instead of being mistaken for a relative one.
    """
    head = path[:8].upper()
    if head == "\\\\?\\UNC\\" or head == "\\??\\UNC\\":
        return "\\\\" + path[8:]
    if path[:4] in ("\\\\?\\", "\\??\\") and len(path) >= 6 and path[4].isalpha() and path[5] == ":":
        return path[4:]
    return path


def _is_unc_or_device(path: str) -> bool:
    """True for any `\\\\host\\share`, `\\\\?\\...` or `\\\\.\\...` shaped path."""
    return path.replace("/", "\\").startswith("\\\\")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(_strip_extended(path)))


def is_inside(path: str, root: str) -> bool:
    """True when `path` is `root` itself or below it - string-only, no I/O.

    Uses normcase + normpath + commonpath, so it is case-insensitive on
    Windows, is not fooled by the sibling-prefix trap (`C:\\root2` is not inside
    `C:\\root`) and returns False (never raises) for paths on different drives
    or a mix of rooted and relative paths.
    """
    if not isinstance(path, str) or not isinstance(root, str) or not path or not root:
        return False
    try:
        a = _norm(path)
        b = _norm(root)
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


def real_root(root_dir: str) -> str:
    """Return the link-free absolute form of the root folder, computed once per refresh.

    `realpath` plus stripping any `\\\\?\\` prefix so the string containment
    checks compare like with like. Everything that resolves references takes
    this as `root_real`; a base folder handed to `resolve_ref` must itself be a
    real (link-free) path - a `real_root()` result or the directory of a
    previous `RefResolution.path`.
    """
    return _strip_extended(_realpath(root_dir))


@dataclass(frozen=True)
class RefResolution:
    """Outcome of `resolve_ref`.

    `state` is one of "ok", "broken", "blocked", "unsaved". When "ok", `path`
    is the real, link-free, in-root absolute path and `is_dir` says whether it
    is a directory; otherwise `path` is None. `reason` is a short human string.
    """

    state: str
    reason: str = ""
    rel: str = ""
    path: str | None = None
    is_dir: bool = False


def _blocked(reason: str, rel: str = "") -> RefResolution:
    return RefResolution("blocked", reason, rel)


def _broken(reason: str, rel: str = "") -> RefResolution:
    return RefResolution("broken", reason, rel)


def _is_link(st) -> bool:
    """A symlink, or an NTFS junction (a directory with the mount-point reparse tag).

    `os.lstat` reports a junction as a plain directory (mode 0o40777) and only
    the reparse tag betrays it, so checking `S_ISLNK` alone would silently
    follow junctions.
    """
    if stat.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) == _REPARSE_MOUNT_POINT


def _split_path(path: str) -> list[str]:
    return [c for c in re.split(r"[\\/]+", path) if c not in ("", ".")]


def resolve_ref(raw: str, base_dir: str | None, root_real: str | None) -> RefResolution:
    """Resolve a raw `@reference` to a real in-root path, or explain why not.

    `base_dir` is the (real) folder of the Write that contains the reference;
    `root_real` is the `real_root()` of the root Write. Either being None means
    the Write is unsaved - reported as "unsaved" only AFTER the syntax check so
    a hostile reference is still "blocked" in an unsaved Write.

    The walk (see the module docstring) consumes one component at a time; no
    untrusted component is `lstat`-ed unless the directory it is joined to is
    already verified to be inside the root. Junctions and symlinks are never
    followed by the OS: the target is vetted and spliced into the pending list.
    A lexical containment pre-check runs before any I/O; it can reject a `..`
    chain that would only have stayed inside via a link (the safe direction).
    Never raises on I/O problems - they become "broken".
    """
    syn = check_ref_syntax(raw)
    if not syn.ok:
        return RefResolution(syn.state, syn.reason)
    rel = syn.rel
    if base_dir is None or root_real is None:
        return RefResolution("unsaved", "the Write has not been saved yet", rel)
    cur = _strip_extended(base_dir)
    if not is_inside(cur, root_real):
        return _blocked("the Write's folder is outside the root folder", rel)
    parts = [] if rel == "." else rel.split("/")
    if not is_inside(os.path.normpath(os.path.join(cur, *parts)), root_real):
        return _blocked("reference escapes the root folder", rel)

    pending = deque(parts)
    hops = 0
    last_st = None
    while pending:
        if last_st is not None and not stat.S_ISDIR(last_st.st_mode):
            return _broken("not a folder", rel)
        comp = pending.popleft()
        if comp == "..":
            cur = os.path.dirname(cur)
            if not is_inside(cur, root_real):
                return _blocked("reference escapes the root folder", rel)
            last_st = None
            continue
        # Link targets are attacker-controlled too: vet their components with
        # the same rules as typed ones before they reach the OS.
        problem = _component_problem(comp)
        if problem:
            return _blocked(problem, rel)
        nxt = os.path.join(cur, comp)
        try:
            st = _lstat(nxt)
        except FileNotFoundError:
            return _broken("not found", rel)
        except (OSError, ValueError):
            return _broken("not accessible", rel)
        if _is_link(st):
            hops += 1
            if hops > MAX_LINK_HOPS:
                return _broken("too many links", rel)
            try:
                target = _strip_extended(_readlink(nxt))
            except (OSError, ValueError):
                return _broken("link cannot be read", rel)
            if _is_unc_or_device(target):
                # NEVER followed: touching a UNC path can leak credentials.
                return _blocked("link to a network or device path", rel)
            if os.path.splitdrive(target)[0] or target.startswith(("/", "\\")):
                target = os.path.normpath(target)
                if not is_inside(target, root_real):
                    return _blocked("link leaves the root folder", rel)
                cur = _strip_extended(root_real)
                pending = deque(_split_path(os.path.relpath(target, cur))) + pending
                last_st = None
            else:
                pending = deque(_split_path(target)) + pending
            continue
        cur = nxt
        last_st = st

    if last_st is None:
        try:
            last_st = _lstat(cur)
        except FileNotFoundError:
            return _broken("not found", rel)
        except (OSError, ValueError):
            return _broken("not accessible", rel)
    mode = last_st.st_mode
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        return _blocked("not a regular file or folder", rel)
    try:
        path = _strip_extended(_realpath(cur))
    except (OSError, ValueError):
        return _broken("not accessible", rel)
    if not is_inside(path, root_real):
        return _blocked("reference resolves outside the root folder", rel)
    return RefResolution("ok", "", rel, path, stat.S_ISDIR(mode))


def classify_ref(raw: str, base_dir: str | None, root_real: str | None) -> str:
    """Return just the state of `resolve_ref` - the highlighter's colour key."""
    return resolve_ref(raw, base_dir, root_real).state


def chain_key(real_path: str) -> str:
    """Key used for cycle detection: the real path, case-folded like the OS does."""
    return os.path.normcase(real_path)


def relative_ref_for(target: str, base_dir: str | None, root_real: str | None) -> tuple[str | None, str]:
    """Build the relative reference text for an absolute `target`, for "Insert reference".

    Returns `(ref, message)`. `(rel, "")` when the reference resolves ok;
    `(rel, reason)` when it is safe but currently broken (the caller may still
    insert it); `(None, reason)` when it cannot be expressed as an in-root
    relative reference or would be blocked. Containment is checked lexically
    first, then the produced text is re-run through `check_ref_syntax` and
    `resolve_ref` so what the UI inserts is exactly what the resolver accepts.
    Forward slashes always (documented choice: refs stay as typed).
    """
    if base_dir is None or root_real is None:
        return None, "the Write has not been saved yet"
    if not is_inside(base_dir, root_real):
        return None, "the Write's folder is outside the root folder"
    if not is_inside(target, root_real):
        return None, "target is outside the root folder"
    try:
        rel = os.path.relpath(_strip_extended(target), _strip_extended(base_dir))
    except ValueError:
        return None, "target is on a different drive"
    rel = rel.replace("\\", "/")
    syn = check_ref_syntax(rel)
    if not syn.ok:
        return None, syn.reason
    res = resolve_ref(syn.rel, base_dir, root_real)
    if res.state == "blocked":
        return None, res.reason
    return syn.rel, "" if res.state == "ok" else res.reason
