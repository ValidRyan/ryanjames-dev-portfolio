"""Shared pytest fixtures for the MVP test suite.

Sets QT_QPA_PLATFORM=offscreen *before* any PySide6 import, so the whole
session runs headless (no real display needed, locally or in CI). Program
MVP's existing tests are pure Qt-free logic and never needed this, but Doc
MVP's test_operators.py / test_wires.py / test_live_sync.py construct real
QGraphicsObject / QObject instances and need a QApplication.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    """Session-scoped QApplication, created once and reused by every test
    that needs one (constructing QGraphicsObject/QObject/QWidget subclasses
    requires a live QApplication instance to exist first)."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
