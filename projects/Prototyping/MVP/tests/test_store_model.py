"""Tests for store_model.py - zero Qt import anywhere in this file.

Covers add_folder/add_file/delete_item basic tree ops, arrival-order
preservation, nested folders, and sieve() (filename-match, content-match
against a real sample .docx, case-insensitivity, and a pruned-tree case
where only the deep path to a match should survive).
"""

import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "Folder Replication"))

import store_model
from store_model import StoreFile, StoreFolder, add_file, add_folder, delete_item, sieve

SAMPLE_DIR = _MVP_ROOT / "sample"
SAMPLE_DOCX = SAMPLE_DIR / "sample_input.docx"
WILDCARD_TXT = SAMPLE_DIR / "wildcard.txt"


def test_add_folder_appends_and_returns_new_folder():
    root = store_model.new_root()
    folder = add_folder(root, "Docs")

    assert folder.name == "Docs"
    assert folder.children == []
    assert root.children == [folder]


def test_add_file_appends_and_returns_new_file_with_derived_name():
    root = store_model.new_root()
    store_file = add_file(root, str(SAMPLE_DOCX))

    assert store_file.name == "sample_input.docx"
    assert store_file.path == str(SAMPLE_DOCX)
    assert root.children == [store_file]


def test_arrival_order_preserved_across_mixed_adds():
    root = store_model.new_root()
    folder_a = add_folder(root, "A")
    file_a = add_file(root, str(SAMPLE_DOCX))
    folder_b = add_folder(root, "B")

    assert root.children == [folder_a, file_a, folder_b]


def test_nested_folders_can_be_added():
    root = store_model.new_root()
    parent = add_folder(root, "Parent")
    child = add_folder(parent, "Child")
    grandchild_file = add_file(child, str(WILDCARD_TXT))

    assert root.children == [parent]
    assert parent.children == [child]
    assert child.children == [grandchild_file]


def test_delete_item_removes_child():
    root = store_model.new_root()
    folder = add_folder(root, "Docs")
    file_ref = add_file(root, str(SAMPLE_DOCX))

    delete_item(root, folder)

    assert root.children == [file_ref]


def test_delete_item_missing_item_is_noop():
    root = store_model.new_root()
    other_root = store_model.new_root()
    stray_folder = add_folder(other_root, "Stray")

    # stray_folder was never added to `root` - deleting it from `root`
    # should not raise (documented no-op behavior in store_model.delete_item).
    delete_item(root, stray_folder)

    assert root.children == []


def test_sieve_filename_match():
    root = store_model.new_root()
    add_file(root, str(WILDCARD_TXT))  # "wildcard.txt" - matches by name

    pruned = sieve(root, "wildcard")

    assert len(pruned.children) == 1
    assert pruned.children[0].name == "wildcard.txt"


def test_sieve_content_match_using_real_sample_docx():
    # "paragraph" appears in sample_input.docx's real content but not in its
    # filename - exercises content_read.py for real, not a mock.
    root = store_model.new_root()
    docx_file = add_file(root, str(SAMPLE_DOCX))

    pruned = sieve(root, "paragraph")

    assert len(pruned.children) == 1
    assert pruned.children[0].name == docx_file.name
    assert pruned.children[0] is docx_file  # matching leaves are the same instance


def test_sieve_is_case_insensitive():
    root = store_model.new_root()
    add_file(root, str(SAMPLE_DOCX))

    pruned_lower = sieve(root, "paragraph")
    pruned_upper = sieve(root, "PARAGRAPH")
    pruned_mixed = sieve(root, "PaRaGrAph")

    assert len(pruned_lower.children) == 1
    assert len(pruned_upper.children) == 1
    assert len(pruned_mixed.children) == 1


def test_sieve_pruned_tree_only_deep_matching_path_survives():
    root = store_model.new_root()

    # Sibling folder with no match anywhere inside it - must NOT appear.
    no_match_folder = add_folder(root, "NoMatch")
    add_file(no_match_folder, str(WILDCARD_TXT))

    # Sibling file directly under root with no match - must NOT appear.
    add_file(root, str(SAMPLE_DIR / "equations.txt"))

    # Nested path that DOES lead to a match, several folders deep.
    level_one = add_folder(root, "Level1")
    level_two = add_folder(level_one, "Level2")
    matching_file = add_file(level_two, str(SAMPLE_DOCX))  # matches "paragraph"

    pruned = sieve(root, "paragraph")

    assert [child.name for child in pruned.children] == ["Level1"]
    pruned_level_one = pruned.children[0]
    assert [child.name for child in pruned_level_one.children] == ["Level2"]
    pruned_level_two = pruned_level_one.children[0]
    assert pruned_level_two.children == [matching_file]

    # And the pruned tree is all-new StoreFolder instances, not the originals.
    assert pruned_level_one is not level_one
    assert pruned_level_two is not level_two


def test_sieve_never_mutates_the_original_tree():
    root = store_model.new_root()
    folder = add_folder(root, "Docs")
    add_file(folder, str(SAMPLE_DOCX))
    add_file(root, str(WILDCARD_TXT))

    original_root_children_count = len(root.children)
    original_folder_children_count = len(folder.children)

    sieve(root, "paragraph")

    assert len(root.children) == original_root_children_count
    assert len(folder.children) == original_folder_children_count


def test_sieve_empty_keyword_returns_empty_pruned_root():
    root = store_model.new_root()
    add_file(root, str(SAMPLE_DOCX))

    pruned_empty = sieve(root, "")
    pruned_whitespace = sieve(root, "   ")

    assert pruned_empty.children == []
    assert pruned_whitespace.children == []


def test_sieve_skips_unreadable_file_without_crashing_the_whole_sieve():
    root = store_model.new_root()
    add_file(root, "/this/path/does/not/exist.txt")  # unreadable - must be skipped, not raise
    matching_file = add_file(root, str(SAMPLE_DOCX))  # a real match, elsewhere in the same folder

    pruned = sieve(root, "paragraph")

    assert pruned.children == [matching_file]
