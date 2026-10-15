"""Tests for flow.py - pure precedence/resolution logic, no Qt at all.

Parametrized over all 32 raw (on, off, uni, bi, one_shot) combinations,
using plain stub objects for execute() - directly mirrors the appendix
table in Joint Interactor/Predictions on Options.md, generated the same way (by
actually calling resolve()), so this suite is the audit trail for that
document.
"""

import sys
from itertools import product
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "Joint Interactor"))

import pytest

from flow import Cadence, Endpoint, WireState, execute, resolve

ALL_32 = list(product([False, True], repeat=5))


def _expected_shape(on, off, uni, bi, one_shot):
    if off:
        return "dead-wire"
    mirror = bi
    continuous = on
    return {
        (False, False): "pulse-wire",
        (True, False): "mirror-pulse-wire",
        (False, True): "live-wire",
        (True, True): "live-mirror-wire",
    }[(mirror, continuous)]


@pytest.mark.parametrize("on,off,uni,bi,one_shot", ALL_32)
def test_resolve_matches_expected_shape_for_all_32_combinations(on, off, uni, bi, one_shot):
    state = WireState(on=on, off=off, uni=uni, bi=bi, one_shot=one_shot)
    plan = resolve(state, Endpoint.A)
    assert plan.shape == _expected_shape(on, off, uni, bi, one_shot)


@pytest.mark.parametrize("on,off,uni,bi,one_shot", ALL_32)
def test_resolve_dead_wire_iff_off(on, off, uni, bi, one_shot):
    state = WireState(on=on, off=off, uni=uni, bi=bi, one_shot=one_shot)
    plan = resolve(state, Endpoint.A)
    if off:
        assert plan.legs == ()
        assert plan.cadence == Cadence.NONE
        assert plan.watch is False
    else:
        assert len(plan.legs) in (1, 2)


def test_off_always_wins_regardless_of_other_toggles():
    for on, uni, bi, one_shot in product([False, True], repeat=4):
        state = WireState(on=on, off=True, uni=uni, bi=bi, one_shot=one_shot)
        plan = resolve(state, Endpoint.A)
        assert plan.shape == "dead-wire"
        assert plan.legs == ()
        assert plan.cadence == Cadence.NONE
        assert plan.watch is False


def test_bi_wins_over_uni_producing_two_legs():
    state = WireState(bi=True, uni=True)
    plan = resolve(state, Endpoint.A)
    assert len(plan.legs) == 2
    assert (Endpoint.A, Endpoint.B) in plan.legs
    assert (Endpoint.B, Endpoint.A) in plan.legs


def test_bi_alone_matches_bi_plus_uni():
    bi_only = resolve(WireState(bi=True), Endpoint.A)
    bi_and_uni = resolve(WireState(bi=True, uni=True), Endpoint.A)
    assert bi_only.shape == bi_and_uni.shape
    assert bi_only.legs == bi_and_uni.legs


def test_on_wins_over_one_shot_producing_continuous_cadence_and_watch():
    state = WireState(on=True, one_shot=True)
    plan = resolve(state, Endpoint.A)
    assert plan.cadence == Cadence.CONTINUOUS
    assert plan.watch is True


def test_on_alone_matches_on_plus_one_shot():
    on_only = resolve(WireState(on=True), Endpoint.A)
    on_and_one_shot = resolve(WireState(on=True, one_shot=True), Endpoint.A)
    assert on_only.shape == on_and_one_shot.shape
    assert on_only.cadence == on_and_one_shot.cadence
    assert on_only.watch == on_and_one_shot.watch


def test_neither_direction_toggle_still_defaults_to_forward_flow():
    state = WireState()
    plan = resolve(state, Endpoint.A)
    assert plan.legs == ((Endpoint.A, Endpoint.B),)
    assert plan.shape == "pulse-wire"


def test_neither_cadence_toggle_still_defaults_to_a_single_pulse():
    state = WireState()
    plan = resolve(state, Endpoint.A)
    assert plan.cadence == Cadence.ONCE
    assert plan.watch is False


def test_resolve_respects_source_endpoint_for_direction():
    state = WireState()
    plan = resolve(state, Endpoint.B)
    assert plan.legs == ((Endpoint.B, Endpoint.A),)


def test_wire_state_defaults_all_false():
    state = WireState()
    assert state.on is False
    assert state.off is False
    assert state.uni is False
    assert state.bi is False
    assert state.one_shot is False


class _StubNode:
    """Plain duck-typed stand-in for an OperatorNode - no Qt involved.

    `load_content()` resets `.content` back to the original construction-time
    value (a stand-in "source of truth", mirroring how a real OperatorNode's
    `load_content()` re-reads from its own on-disk source file rather than
    from whatever another wire leg may have just assigned into `.content` -
    see `execute()`'s new plain-attribute-assignment fallback path for why
    this decoupling matters for a mirror (Bi) plan's second leg).
    """

    def __init__(self, content):
        self.content = content
        self._source_content = content
        self.written = None
        self.load_calls = 0

    def load_content(self):
        self.load_calls += 1
        self.content = self._source_content

    def write_export(self, text):
        self.written = text
        return f"stub-sibling::{text}"


def test_execute_runs_single_leg_writing_from_content_to_target():
    node_a = _StubNode("hello from a")
    node_b = _StubNode("hello from b")
    plan = resolve(WireState(), Endpoint.A)  # defaults to forward-only, single pulse
    nodes = {Endpoint.A: node_a, Endpoint.B: node_b}

    result = execute(plan, nodes)

    assert node_a.load_calls == 1
    assert node_b.written == "hello from a"
    assert node_a.written is None
    assert result == ["stub-sibling::hello from a"]
    # gap #1: execute() now also updates the target's in-memory .content,
    # not just its written export.
    assert node_b.content == "hello from a"


def test_execute_runs_both_legs_for_mirror_plan():
    node_a = _StubNode("a-content")
    node_b = _StubNode("b-content")
    plan = resolve(WireState(bi=True), Endpoint.A)
    nodes = {Endpoint.A: node_a, Endpoint.B: node_b}

    execute(plan, nodes)

    assert node_b.written == "a-content"
    assert node_a.written == "b-content"


def test_execute_no_legs_for_dead_wire_does_nothing():
    node_a = _StubNode("x")
    node_b = _StubNode("y")
    plan = resolve(WireState(off=True), Endpoint.A)
    nodes = {Endpoint.A: node_a, Endpoint.B: node_b}

    result = execute(plan, nodes)

    assert result == []
    assert node_a.written is None
    assert node_b.written is None


def test_execute_duck_typed_stub_without_load_content_still_works():
    class _MinimalStub:
        def __init__(self, content):
            self.content = content
            self.written = None

        def write_export(self, text):
            self.written = text
            return "ok"

    node_a = _MinimalStub("no load_content here")
    node_b = _MinimalStub("")
    plan = resolve(WireState(), Endpoint.A)
    nodes = {Endpoint.A: node_a, Endpoint.B: node_b}

    execute(plan, nodes)

    assert node_b.written == "no load_content here"
    # gap #1: the plain-attribute-assignment fallback path still applies
    # .content even without receive_content().
    assert node_b.content == "no load_content here"


class _ReceiveContentStubNode:
    """Duck-typed stand-in exposing `receive_content()`, so `execute()`
    prefers calling it over plain attribute assignment for the target leg."""

    def __init__(self, content):
        self.content = content
        self.written = None
        self.received = []

    def load_content(self):
        pass

    def receive_content(self, text):
        self.received.append(text)
        self.content = text

    def write_export(self, text):
        self.written = text
        return f"received-sibling::{text}"


def test_execute_calls_receive_content_when_target_exposes_it():
    node_a = _ReceiveContentStubNode("hello from a")
    node_b = _ReceiveContentStubNode("hello from b")
    plan = resolve(WireState(), Endpoint.A)  # forward-only, single pulse
    nodes = {Endpoint.A: node_a, Endpoint.B: node_b}

    result = execute(plan, nodes)

    assert node_b.received == ["hello from a"]
    assert node_b.content == "hello from a"
    assert node_b.written == "hello from a"
    assert result == ["received-sibling::hello from a"]
    # the source node's own receive_content is never invoked - only the leg's target
    assert node_a.received == []
