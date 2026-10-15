"""AST-based static checks for the SAP Modifier (no Qt, nothing is imported or run).

The SAP Modifier reads files from paths users type, so the code base itself is
held to a few hard rules, enforced by scanning the SOURCE (never by executing
it):

1. no process / dynamic-code APIs (`subprocess`, `os.system`, `os.popen`,
   `os.startfile`, `eval`, `exec`, `__import__`, `importlib`, `pickle`,
   `marshal`, `QProcess`, `QDesktopServices`, ...);
2. no network APIs (`socket`, `urllib`, `requests`, `http`, `QtNetwork`, ...);
3. the Qt-free modules import no PySide (nor any Qt-using sibling module);
4. only `write_format.py` (and the build-time sample generator) may write files;
5. special-instruction extension literals appear only in `write_paths.py`;
6. module and test basenames are globally unique across every MVP folder
   (they share one flat `sys.path` in one pytest process) and sources are ASCII.

The scan is robust to modules that do not exist yet: it covers whichever
`SAP Modifier/*.py` files are present (plus `sample/generate_write_samples.py`
when it exists), so it keeps passing as later build steps add the Qt modules.
`test_detectors_flag_known_bad_code` proves each detector actually fires, so a
green scan is not vacuous. Known limits (documented, not hidden): string-built
attribute access (`getattr(os, "sys" + "tem")`) and Qt's in-memory
`QImage.save(QBuffer)` (allowed on purpose) are not distinguishable by name.
"""

import ast
import os
import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))

import write_paths

_WRITE_DIR = _MVP_ROOT / "SAP Modifier"
_GENERATOR = _MVP_ROOT / "sample" / "generate_write_samples.py"

QT_FREE_MODULES = ("write_paths", "write_model", "write_format", "write_grammar", "write_resolve")
QT_USING_MODULES = (
    "write_docmodel",
    "write_overlay",
    "write_embed",
    "write_highlight",
    "write_editor",
    "write_tree",
    "app_write",
)
QT_ROOTS = {"PySide6", "PySide2", "PyQt5", "PyQt6", "shiboken6", "shiboken2", "sip"}

# Files that predate the ASCII-source rule and belong to another MVP (a Program
# MVP sample generator with a deliberate CJK sample string). Not ours to edit;
# any OTHER non-ASCII source still fails.
LEGACY_NON_ASCII = {"generate_multi_samples.py"}

_PROCESS_MODULES = {
    "subprocess", "importlib", "pickle", "marshal", "multiprocessing", "ctypes", "webbrowser", "runpy", "shelve",
}
_NETWORK_MODULES = {
    "socket", "socketserver", "ssl", "urllib", "requests", "http", "ftplib", "smtplib", "poplib", "imaplib",
    "telnetlib", "xmlrpc", "QtNetwork",
}
_BANNED_BARE_NAMES = {"eval", "exec", "__import__", "startfile", "QProcess", "QDesktopServices"}
# `app.exec()` / `QApplication.exec()` starts the Qt event loop; it runs no code from data, so it is the
# one `.exec` allowed. Any other `obj.exec(...)` (a dialog, a shell wrapper) is still flagged.
_EVENT_LOOP_OWNERS = {"app", "QApplication"}
_BANNED_NETWORK_NAMES = {"QNetworkAccessManager", "QNetworkRequest", "QTcpSocket", "QUdpSocket", "QSslSocket"}
_OS_PROCESS_ATTRS = {
    "system", "popen", "startfile", "posix_spawn", "posix_spawnp",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
}
_WRITER_MODULES = {"shutil", "tempfile"}
_OS_WRITE_ATTRS = {
    "remove", "unlink", "rename", "renames", "replace", "mkdir", "makedirs", "rmdir", "removedirs",
    "symlink", "link", "truncate", "utime", "chmod", "chown", "fdopen", "write", "open",
}
_PATH_WRITE_ATTRS = {"write_text", "write_bytes", "unlink", "rmdir", "mkdir", "touch", "symlink_to", "hardlink_to"}
_QT_WRITE_NAMES = {"QFile", "QSaveFile", "QTemporaryFile", "QSettings"}


# -- detectors: pure functions over an ast.Module, returning problem strings ----


def _import_tokens(tree):
    """Yield `(lineno, token)` for every dotted-name part and imported name."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for part in alias.name.split("."):
                    yield node.lineno, part
        elif isinstance(node, ast.ImportFrom):
            for part in (node.module or "").split("."):
                if part:
                    yield node.lineno, part
            for alias in node.names:
                yield node.lineno, alias.name


def _os_from_imports(tree):
    """Names imported with `from os import ...` (a spelling that hides the `os.` prefix)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                yield node.lineno, alias.name


def find_process_and_eval(tree) -> list[str]:
    problems = []
    for lineno, token in _import_tokens(tree):
        if token in _PROCESS_MODULES or token in _BANNED_BARE_NAMES:
            problems.append(f"line {lineno}: imports {token}")
    for lineno, name in _os_from_imports(tree):
        if name in _OS_PROCESS_ATTRS:
            problems.append(f"line {lineno}: imports os.{name}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _BANNED_BARE_NAMES:
            problems.append(f"line {node.lineno}: uses {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr == "exec" and isinstance(node.value, ast.Name) and node.value.id in _EVENT_LOOP_OWNERS:
                continue  # the one legitimate `.exec`: starting Qt's event loop (`sys.exit(app.exec())`)
            if node.attr in _BANNED_BARE_NAMES:
                problems.append(f"line {node.lineno}: uses .{node.attr}")
            elif node.attr in _OS_PROCESS_ATTRS and isinstance(node.value, ast.Name) and node.value.id == "os":
                problems.append(f"line {node.lineno}: uses os.{node.attr}")
    return problems


def find_network(tree) -> list[str]:
    problems = []
    for lineno, token in _import_tokens(tree):
        if token in _NETWORK_MODULES:
            problems.append(f"line {lineno}: imports {token}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _BANNED_NETWORK_NAMES:
            problems.append(f"line {node.lineno}: uses {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _BANNED_NETWORK_NAMES | {"QtNetwork"}:
            problems.append(f"line {node.lineno}: uses .{node.attr}")
    return problems


def find_qt_imports(tree) -> list[str]:
    problems = []
    forbidden = QT_ROOTS | set(QT_USING_MODULES)
    for lineno, token in _import_tokens(tree):
        if token in forbidden:
            problems.append(f"line {lineno}: imports {token}")
    return problems


def _open_mode_problem(call: ast.Call) -> str | None:
    """A write-capable `open(...)` call, judged from a literal mode (see notes inline)."""
    func = call.func
    is_builtin = isinstance(func, ast.Name) and func.id == "open"
    is_attr = isinstance(func, ast.Attribute) and func.attr == "open"
    if not (is_builtin or is_attr):
        return None
    if is_attr and isinstance(func.value, ast.Name) and func.value.id == "os":
        return None  # os.open is reported by the os-attribute rule
    module_style = is_builtin or (isinstance(func.value, ast.Name) and func.value.id in ("io", "codecs", "builtins"))
    mode = None
    index = 1 if module_style else 0  # Path.open(mode) vs open(file, mode)
    if len(call.args) > index:
        mode = call.args[index]
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return None
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return "opens a file for writing" if any(ch in mode.value for ch in "wax+") else None
    return "opens a file with a non-literal mode" if is_builtin else None


def find_file_writes(tree) -> list[str]:
    problems = []
    for lineno, token in _import_tokens(tree):
        if token in _WRITER_MODULES:
            problems.append(f"line {lineno}: imports {token}")
    for lineno, name in _os_from_imports(tree):
        if name in _OS_WRITE_ATTRS:
            problems.append(f"line {lineno}: imports os.{name}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            problem = _open_mode_problem(node)
            if problem:
                problems.append(f"line {node.lineno}: {problem}")
            func = node.func
            if isinstance(func, ast.Attribute):
                if isinstance(func.value, ast.Name) and func.value.id == "os" and func.attr in _OS_WRITE_ATTRS:
                    problems.append(f"line {node.lineno}: calls os.{func.attr}")
                elif func.attr in _PATH_WRITE_ATTRS:
                    problems.append(f"line {node.lineno}: calls .{func.attr}()")
        if isinstance(node, ast.Name) and node.id in _QT_WRITE_NAMES:
            problems.append(f"line {node.lineno}: uses {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _QT_WRITE_NAMES:
            problems.append(f"line {node.lineno}: uses .{node.attr}")
    return problems


def _docstring_nodes(tree) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def find_si_literals(tree, extensions) -> list[str]:
    """String constants (docstrings excluded) that spell an SI extension."""
    skip = _docstring_nodes(tree)
    lowered = [ext.lower() for ext in extensions]
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            text = node.value.lower()
            for ext in lowered:
                if ext in text:
                    problems.append(f"line {node.lineno}: string literal contains {ext}")
    return problems


# -- source collection --------------------------------------------------------


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _write_modules() -> list[Path]:
    """Every Python file in `SAP Modifier/` (whatever exists right now)."""
    return sorted(_WRITE_DIR.glob("*.py"))


def _scanned_files() -> list[Path]:
    files = _write_modules()
    if _GENERATOR.exists():
        files.append(_GENERATOR)
    return files


def _all_mvp_python_files() -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(_MVP_ROOT):
        dirnames[:] = [d for d in dirnames if d not in (".venv", "__pycache__", ".pytest_cache", "node_modules")]
        found.extend(Path(dirpath) / name for name in filenames if name.endswith(".py"))
    return sorted(found)


def _report(found: dict) -> str:
    return "\n".join(f"{path.name}: {problem}" for path, problems in found.items() for problem in problems)


# -- the checks ----------------------------------------------------------------


def test_no_process_or_dynamic_code_apis():
    found = {p: find_process_and_eval(_parse(p)) for p in _scanned_files()}
    found = {p: probs for p, probs in found.items() if probs}
    assert not found, _report(found)


def test_no_network_apis():
    found = {p: find_network(_parse(p)) for p in _scanned_files()}
    found = {p: probs for p, probs in found.items() if probs}
    assert not found, _report(found)


def test_qt_free_modules_import_no_pyside():
    found = {}
    for name in QT_FREE_MODULES:
        path = _WRITE_DIR / f"{name}.py"
        assert path.exists(), f"{path.name} is a Qt-free module and must exist"
        problems = find_qt_imports(_parse(path))
        if problems:
            found[path] = problems
    assert not found, _report(found)


def test_write_modules_import_nothing_from_sibling_mvps():
    siblings = set()
    for folder in ("File Recompiler", "Joint Interactor", "Folder Replication"):
        for path in (_MVP_ROOT / folder).glob("*.py"):
            siblings.add(path.stem)
    found = {}
    for path in _write_modules():
        problems = [f"line {ln}: imports {tok}" for ln, tok in _import_tokens(_parse(path)) if tok in siblings]
        if problems:
            found[path] = problems
    assert not found, _report(found)


def test_only_write_format_and_the_generator_write_files():
    found = {}
    for path in _write_modules():
        if path.name == "write_format.py":
            continue
        problems = find_file_writes(_parse(path))
        if problems:
            found[path] = problems
    assert not found, _report(found)
    # ...and the one allowed writer really is the writer (the rule is not vacuous).
    assert find_file_writes(_parse(_WRITE_DIR / "write_format.py"))


def test_si_extension_literals_only_in_write_paths():
    assert set(write_paths.SI_EXTENSIONS) >= {".mwrite", ".mstore", ".mdoc", ".mprog"}
    extensions = list(write_paths.SI_EXTENSIONS)
    found = {}
    for path in _write_modules():
        if path.name == "write_paths.py":
            continue
        problems = find_si_literals(_parse(path), extensions)
        if problems:
            found[path] = problems
    assert not found, _report(found)
    # ...and write_paths does spell them (the rule is not vacuous).
    assert find_si_literals(_parse(_WRITE_DIR / "write_paths.py"), extensions)


def test_module_and_test_basenames_are_unique_and_sources_are_ascii():
    files = _all_mvp_python_files()
    by_name: dict[str, list[Path]] = {}
    for path in files:
        by_name.setdefault(path.name, []).append(path)
    duplicates = {name: paths for name, paths in by_name.items() if len(paths) > 1}
    assert not duplicates, f"basenames must be globally unique: {ascii(str(duplicates))}"

    for path in _write_modules():
        assert path.stem.startswith("write_") or path.stem == "app_write", f"{path.name} lacks the write_ prefix"

    non_ascii = []
    for path in files:
        try:
            path.read_bytes().decode("ascii")
        except UnicodeDecodeError:
            if path.name not in LEGACY_NON_ASCII:
                non_ascii.append(path.name)
    assert not non_ascii, f"non-ASCII source files: {non_ascii}"


def test_detectors_flag_known_bad_code():
    def hits(detector, code, *extra):
        return detector(ast.parse(code), *extra)

    for bad in (
        "import subprocess",
        "import os\nos.system('x')",
        "import os\nos.popen('x')",
        "from os import startfile",
        "eval('1')",
        "f = exec",
        "__import__('os')",
        "import importlib.util",
        "import pickle",
        "from PySide6.QtCore import QProcess",
        "from PySide6.QtGui import QDesktopServices",
        "dialog.exec()",
        "shell.exec('x')",
    ):
        assert hits(find_process_and_eval, bad), bad
    # ...but starting the Qt event loop is not dynamic code
    for good in ("import sys\nsys.exit(app.exec())", "QApplication.exec()"):
        assert not hits(find_process_and_eval, good), good
    for bad in ("import socket", "from urllib.request import urlopen", "from PySide6 import QtNetwork", "import http.client"):
        assert hits(find_network, bad), bad
    for bad in ("from PySide6.QtCore import Signal", "import PySide6", "import write_editor", "from write_tree import TreePane"):
        assert hits(find_qt_imports, bad), bad
    for bad in (
        "open('x', 'w')",
        "open('x', mode='ab')",
        "open('x', mode)",
        "p.open('w')",
        "p.write_text('y')",
        "p.write_bytes(b'')",
        "import tempfile",
        "import shutil",
        "import os\nos.replace(a, b)",
        "import os\nos.remove(a)",
        "from os import unlink",
        "p.unlink()",
        "QSaveFile(p)",
    ):
        assert hits(find_file_writes, bad), bad
    assert hits(find_si_literals, "x = 'main.MWRITE'", [".mwrite"])
    assert hits(find_si_literals, "x = f'{a}.mstore'", [".mstore"])

    for good in ("open('x')", "open('x', 'rb')", "open(p, mode='r')", "text.replace('a', 'b')", "os.path.join(a, b)", "p.exists()"):
        assert not hits(find_file_writes, good), good
    assert not hits(find_process_and_eval, "import os\nos.path.join('a')\nplatform.system()")
    assert not hits(find_network, "import json\nx = 'http'")
    assert not hits(find_si_literals, '"""Docstring mentioning .mwrite."""\ndef f():\n    """Also .mstore."""\n', [".mwrite", ".mstore"])
