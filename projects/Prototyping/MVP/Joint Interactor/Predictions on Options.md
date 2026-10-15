# Predictions on Options

Analysis of what happens when Joint Interactor's five independent wire toggles
(On / Off / Uni / Bi / One-shot) are combined, and the distinct "shapes" of
wire behavior that actually emerge from those combinations.

## 1. Purpose

Joint Interactor treats a wire's five signal-flow options as independent booleans
rather than one fixed mode - more than one can be toggled at once, and
that raises an obvious question: what happens when they *disagree*? This
document is a prose mirror of the resolution logic implemented in
`flow.py::resolve()`, written to surface the actual distinct behaviors
("shapes") that fall out of all 32 raw boolean combinations.

**Tiebreaker note:** if anything in this document ever appears to disagree
with `flow.py::resolve()` (or with `test_flow.py`, which is parametrized
over all 32 combinations against the real function), the code and its
tests are authoritative, not this document. This document is meant to stay
in sync with them, but the code is the ground truth.

## 2. The five toggles, restated

| Toggle | Meaning |
|---|---|
| **On** | Continuous live sync - the wire stays "armed" (watches the source file) after every Run/trigger, propagating again on every subsequent change. |
| **Off** | Dead wire, absolute override - "nothing runs through it, period." |
| **Uni** | Flow away from the designated source only (one-way). |
| **Bi** | Flow both ways (source -> target *and* target -> source). |
| **One-shot** | A single pulse - one propagation, then done (no re-arming). |

All five default to `False` on a freshly drawn wire, and any subset may be
`True` simultaneously - there is no mutual exclusivity enforced at the data
level. `flow.py::resolve()` is what turns that raw, potentially
contradictory bag of booleans into one coherent plan.

## 3. Precedence rules, in plain English

1. **Off always wins**, regardless of any other toggle. If Off is set,
   nothing else matters - the wire is dead, no legs run, nothing is
   watched. No exceptions.
2. **Bi wins over Uni** when both are set. Bi already includes the
   forward-only leg Uni would produce, so Uni becomes *redundant* rather
   than contradicted - it's simply absorbed, not treated as an error.
   Critically, **when neither Uni nor Bi is set, direction does not
   collapse to "inert"** - it still defaults to forward-only flow (source
   -> target). Only Off means "no signal."
3. **On wins over One-shot** when both are set. On (continuous) already
   delivers every pulse One-shot would produce, so One-shot is absorbed
   into it rather than firing once and then "upgrading." Symmetrically,
   **when neither On nor One-shot is set, cadence does not collapse to
   "never"** - it still defaults to a single pulse (`ONCE`), because a
   bare Run is itself a discrete, pulse-shaped action.
4. Because direction and cadence each have their own independent default
   (forward-only, single-pulse) even when their toggles are both off, the
   only way to get *zero* behavior is the one absolute override: Off.

## 4. Headline finding: 32 combinations collapse to 5 shapes

`resolve()`'s output shape depends on exactly three of the five raw
booleans - `off`, `bi`, and `on` (`uni` and `one_shot` never change the
outcome once `bi`/`on` are known, since they're strictly redundant with
Bi/On respectively). That means the 32 raw combinations collapse to a
headline table of `Off x Bi x On`:

| Off | Bi | On | Shape |
|---|---|---|---|
| **True** | (any) | (any) | `dead-wire` |
| False | False | False | `pulse-wire` |
| False | True | False | `mirror-pulse-wire` |
| False | False | True | `live-wire` |
| False | True | True | `live-mirror-wire` |

This was verified by mechanically enumerating all 32 raw
(`on, off, uni, bi, one_shot`) combinations through the actual
`flow.py::resolve()` function (see the full appendix in section 7) - the
result set contains **exactly 5 distinct `shape` values**, confirming the
headline finding is not a hand-guess but the real, computed behavior of
the code:

```
pulse-wire:         4 of 32 combinations
mirror-pulse-wire:   4 of 32 combinations
dead-wire:          16 of 32 combinations
live-wire:           4 of 32 combinations
live-mirror-wire:    4 of 32 combinations
```

Note that `dead-wire` alone accounts for exactly half (16/32) of all raw
combinations - a direct consequence of Off being an absolute override
across every possible state of the other four toggles (2^4 = 16).

## 5. Named contradiction/redundancy deep-dives

**On + Off** (both `True`): Off wins outright. This is the starkest
contradiction of the five - "stay continuously live" directly conflicts
with "nothing runs through it, period" - and the resolution is total: the
continuous request is simply never reached, because `resolve()` returns
`dead-wire` before `on` is even inspected. There is no partial credit (e.g.
"live but only once") - Off short-circuits everything.

**On + One-shot** (both `True`, Off `False`): Not really a contradiction -
On is a strict superset of One-shot's behavior (continuous cadence
delivers every pulse a one-shot would, plus every one after it). Rather
than treating this as "fire once, then upgrade to continuous," `resolve()`
treats it as simply continuous from the start; One-shot's request is fully
satisfied by On and contributes nothing extra. Cadence resolves to
`Cadence.CONTINUOUS` and `watch=True`, identical to On alone.

**Uni + Bi** (both `True`, Off `False`): Same redundancy pattern as
On/One-shot - Bi is a superset of Uni (it performs the forward leg Uni
would, plus the reverse leg). `resolve()` produces two legs
(`mirror=True`), identical to Bi alone; Uni contributes nothing extra once
Bi is set.

**Neither Uni nor Bi set** (both `False`, Off `False`): This is *not* read
as "no direction, therefore inert." `resolve()` still produces exactly one
leg, source -> target (`mirror=False`), because Off is the only toggle
that means "no signal" - the absence of an explicit direction toggle
falls back to the natural default (forward flow from whoever ran the
wire), not to silence.

**Neither On nor One-shot set** (both `False`, Off `False`): Symmetric to
the direction case. `resolve()` still resolves to `Cadence.ONCE` /
`watch=False` - a single pulse - rather than "never." The reasoning: Run
is itself already a discrete, pulse-shaped user action, so a wire with no
cadence toggle set still honors that one pulse; it does not require
explicitly opting in to "at least one shot."

## 6. The five wire shapes (named vocabulary)

These five names are exactly the `FlowPlan.shape` strings `resolve()`
produces, and are what drives `wires.py`'s rendering (dash pattern, line
weight, arrowhead count):

| Shape | `legs` | `cadence` | `watch` | Rendering |
|---|---|---|---|---|
| `dead-wire` | none (0 legs) | `NONE` | `False` | Dashed grey line, no arrowhead. |
| `pulse-wire` | 1 leg, forward only | `ONCE` | `False` | Thin solid line, single arrowhead. |
| `mirror-pulse-wire` | 2 legs, both directions | `ONCE` | `False` | Thin solid line, double arrowheads. |
| `live-wire` | 1 leg, forward only | `CONTINUOUS` | `True` | Thick solid line, single arrowhead. |
| `live-mirror-wire` | 2 legs, both directions | `CONTINUOUS` | `True` | Thick solid line, double arrowheads. |

Line weight (thin vs. thick) encodes cadence (pulse vs. live/continuous);
arrowhead count (single vs. double) encodes direction (uni vs. bi); dashing
+ grey is reserved exclusively for `dead-wire`, so a dead wire is always
visually unambiguous at a glance regardless of what its other four toggles
are set to underneath.

## 7. Appendix: full 32-row truth table

Generated by mechanically enumerating every `(on, off, uni, bi, one_shot)`
combination through the real `flow.py::resolve()` function (not
hand-derived) - directly auditable against `test_flow.py`'s parametrized
cases, which assert the same 32 rows.

| # | On | Off | Uni | Bi | One-shot | Shape | Cadence | Watch | Legs |
|---|---|---|---|---|---|---|---|---|---|
| 1 | F | F | F | F | F | pulse-wire | once | F | 1 |
| 2 | F | F | F | F | T | pulse-wire | once | F | 1 |
| 3 | F | F | F | T | F | mirror-pulse-wire | once | F | 2 |
| 4 | F | F | F | T | T | mirror-pulse-wire | once | F | 2 |
| 5 | F | F | T | F | F | pulse-wire | once | F | 1 |
| 6 | F | F | T | F | T | pulse-wire | once | F | 1 |
| 7 | F | F | T | T | F | mirror-pulse-wire | once | F | 2 |
| 8 | F | F | T | T | T | mirror-pulse-wire | once | F | 2 |
| 9 | F | T | F | F | F | dead-wire | none | F | 0 |
| 10 | F | T | F | F | T | dead-wire | none | F | 0 |
| 11 | F | T | F | T | F | dead-wire | none | F | 0 |
| 12 | F | T | F | T | T | dead-wire | none | F | 0 |
| 13 | F | T | T | F | F | dead-wire | none | F | 0 |
| 14 | F | T | T | F | T | dead-wire | none | F | 0 |
| 15 | F | T | T | T | F | dead-wire | none | F | 0 |
| 16 | F | T | T | T | T | dead-wire | none | F | 0 |
| 17 | T | F | F | F | F | live-wire | continuous | T | 1 |
| 18 | T | F | F | F | T | live-wire | continuous | T | 1 |
| 19 | T | F | F | T | F | live-mirror-wire | continuous | T | 2 |
| 20 | T | F | F | T | T | live-mirror-wire | continuous | T | 2 |
| 21 | T | F | T | F | F | live-wire | continuous | T | 1 |
| 22 | T | F | T | F | T | live-wire | continuous | T | 1 |
| 23 | T | F | T | T | F | live-mirror-wire | continuous | T | 2 |
| 24 | T | F | T | T | T | live-mirror-wire | continuous | T | 2 |
| 25 | T | T | F | F | F | dead-wire | none | F | 0 |
| 26 | T | T | F | F | T | dead-wire | none | F | 0 |
| 27 | T | T | F | T | F | dead-wire | none | F | 0 |
| 28 | T | T | F | T | T | dead-wire | none | F | 0 |
| 29 | T | T | T | F | F | dead-wire | none | F | 0 |
| 30 | T | T | T | F | T | dead-wire | none | F | 0 |
| 31 | T | T | T | T | F | dead-wire | none | F | 0 |
| 32 | T | T | T | T | T | dead-wire | none | F | 0 |

(`F` = `False`, `T` = `True`. `Legs` is `len(plan.legs)`. `source_endpoint`
was held fixed at `Endpoint.A` for this enumeration since `shape`,
`cadence`, and `watch` never depend on it - only the specific leg tuples
in `legs` do, per rule 2/3 in section 3.)

## 8. Out of scope

Feed -> Churn -> Return -> Rest remains a conceptual frame only for this
document and for the Joint Interactor build as a whole. It has no UI, no state, and
no representation anywhere in `flow.py`, `wires.py`, or this analysis -
it is explicitly excluded from this deliverable per the confirmed
technical boundaries for this build.
