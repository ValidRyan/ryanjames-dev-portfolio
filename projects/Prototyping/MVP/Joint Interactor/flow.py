"""Pure precedence/resolution logic for wire signal-flow.

Zero Qt dependency, by design - `wires.py` (which *does* depend on Qt) owns
`Wire` and calls into this module, but everything here is plain dataclasses
and functions so it is fully unit-testable with plain stub objects
(`test_flow.py` never imports PySide6).

`WireState` lives here (not in wires.py) precisely so it stays Qt-free and
constructible in tests without a QApplication; `Wire` in wires.py simply
owns/holds an instance of it, matching the class-design note in the plan.

Core design decision: wires carry five INDEPENDENT booleans (On/Off/Uni/Bi/
One-shot), not a single mode enum, so more than one can be set at once. The
`resolve()` precedence rules below collapse all 32 raw combinations down to
exactly 5 distinct behavioral "shapes" - see `Predictions on Options.md` for
the full analysis.

This algorithm is proposed, not user-confirmed line-by-line - it was flagged
as such when work started so the resolution rules could be corrected before
being baked into both the code and the Predictions doc.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class Endpoint(Enum):
    A = "a"
    B = "b"


class Cadence(Enum):
    NONE = "none"
    ONCE = "once"
    CONTINUOUS = "continuous"


@dataclass
class WireState:
    """Five independently toggleable wire options - NOT a single enum.

    More than one may be True at once; default is all False (a freshly
    drawn wire carries no flow behavior until explicitly toggled).
    """

    on: bool = False
    off: bool = False
    uni: bool = False
    bi: bool = False
    one_shot: bool = False


@dataclass(frozen=True)
class FlowPlan:
    """The resolved outcome of a WireState + source Endpoint: which legs to
    run, at what cadence, whether to watch for live changes, and the render shape."""

    legs: tuple[tuple[Endpoint, Endpoint], ...]
    cadence: Cadence
    watch: bool
    shape: str  # "dead-wire" | "pulse-wire" | "mirror-pulse-wire" | "live-wire" | "live-mirror-wire"


def resolve(state: WireState, source_endpoint: Endpoint) -> FlowPlan:
    """Resolve a wire's 5 raw booleans + a designated source into a FlowPlan.

    Precedence rules:
      - Off always wins, regardless of any other toggle ("nothing runs
        through it - period"). No exceptions.
      - Bi wins over Uni when both are set - Bi is a superset of Uni (it
        already includes the forward-only leg), so Uni becomes redundant
        rather than contradicted. Neither Uni nor Bi set still defaults to
        forward-only flow (absence of a direction toggle isn't read as
        inert - only Off means "no signal").
      - On wins over One-shot when both are set - On is a superset of
        One-shot (continuous delivers every pulse One-shot would), so
        One-shot is absorbed. Neither On nor One-shot set still defaults to
        a single pulse (ONCE) - a bare Run is itself a discrete, pulse-shaped
        action.

    Net result: of 32 raw boolean combinations, only 5 distinct shapes
    emerge: dead-wire, pulse-wire, mirror-pulse-wire, live-wire,
    live-mirror-wire.
    """
    if state.off:
        return FlowPlan(legs=(), cadence=Cadence.NONE, watch=False, shape="dead-wire")

    mirror = state.bi  # Bi is a superset of Uni -> Bi wins when both set, Uni redundant
    continuous = state.on  # On is a superset of One-shot -> On wins when both set

    target = Endpoint.B if source_endpoint is Endpoint.A else Endpoint.A
    legs = ((source_endpoint, target), (target, source_endpoint)) if mirror else ((source_endpoint, target),)

    cadence = Cadence.CONTINUOUS if continuous else Cadence.ONCE  # neither set -> defaults to ONCE (Run is itself a pulse)

    shape = {
        (False, False): "pulse-wire",
        (True, False): "mirror-pulse-wire",
        (False, True): "live-wire",
        (True, True): "live-mirror-wire",
    }[(mirror, continuous)]

    return FlowPlan(legs=legs, cadence=cadence, watch=continuous, shape=shape)


def execute(plan: FlowPlan, nodes: Mapping[Endpoint, Any]) -> list:
    """Run each leg of `plan` once against an Endpoint -> node mapping.

    For each (from_endpoint, to_endpoint) leg:
      1. If `from_node` exposes `load_content()`, call it to refresh its
         `.content` buffer from disk first.
      2. Read `from_node.content`.
      3. If `to_node` exposes `receive_content(text)`, call it so the
         receiving node can apply its own in-memory-buffer guard (e.g. an
         OperatorNode skips updating `.content`/its display while a user
         edit is in progress); otherwise fall back to plain attribute
         assignment (`to_node.content = text`).
      4. Call `to_node.write_export(text)` - the receiving node writes the
         incoming content out as its own non-destructive export (never
         overwriting either node's original source file), regardless of
         whether step 3 actually updated `to_node.content`.

    Duck-typed against anything exposing `.content` / `.load_content()` /
    `.write_export()` (and optionally `.receive_content()`), so it is fully
    testable with plain stub objects - no Qt needed, no real OperatorNode
    required.

    Returns the list of results from each `write_export()` call, one per
    leg, in leg order. A dead-wire plan (no legs) returns an empty list and
    performs no I/O at all.
    """
    results = []
    for from_endpoint, to_endpoint in plan.legs:
        from_node = nodes[from_endpoint]
        to_node = nodes[to_endpoint]

        if hasattr(from_node, "load_content"):
            from_node.load_content()

        text = from_node.content
        if hasattr(to_node, "receive_content"):
            to_node.receive_content(text)
        else:
            to_node.content = text
        results.append(to_node.write_export(text))

    return results
