"""Instruction vocabulary for the ternary a(instruction)b(instruction)c pipeline.

Each instruction function takes plain text and returns a (result, message)
tuple: result is the transformed text on success, or None if the instruction
isn't implemented yet (message then explains why) - an honest placeholder
rather than a silent no-op.
"""

import ast
import math
import operator

_MAX_POW_DIGITS = 4_000  # stay safely under CPython's default int<->str conversion
# cap (sys.int_info.default_max_str_digits == 4300) so we never even attempt a
# pow() whose result we could not later str() anyway - and, more importantly,
# never pay for computing a multi-million-digit bignum from a handful of
# characters like "9**9999999".


def _safe_pow(base, exponent):
    """pow(base, exponent), but reject results that would be absurdly large.

    Only int**int is unbounded (float ** float overflows to a hardware error
    quickly, already handled by the caller's broad except). A short literal
    like "9**9999999" can ask for a result with millions of digits, which is
    expensive/memory-hungry to even compute, let alone stringify - exactly the
    "tiny input, huge output" DoS shape a restricted evaluator needs to guard
    against. Estimate the digit count cheaply (no big-int work) before doing
    the real (potentially very slow) computation.
    """
    if isinstance(base, int) and isinstance(exponent, int) and exponent > 1 and abs(base) > 1:
        if exponent * math.log10(abs(base)) > _MAX_POW_DIGITS:
            raise ValueError("pow result too large")
    return operator.pow(base, exponent)


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: _safe_pow,
    ast.FloorDiv: operator.floordiv,
}

_UNARY_OPS = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_node(node):
    """Evaluate a restricted arithmetic AST node; raise ValueError on anything else.

    Deliberately NOT built on eval()/exec() - only literal numeric constants,
    the arithmetic binary/unary operators above, and their combinations are
    ever evaluated. Names, calls, attribute access, strings, etc. all raise.
    """
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(
        node.value, bool
    ):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("unsupported expression")


def _try_calculate_line(line: str) -> str | None:
    """Return the computed value of a line as a string, or None if it isn't safe arithmetic."""
    try:
        tree = ast.parse(line.strip(), mode="eval")
        result = _eval_node(tree)
        return str(result)
    except Exception:  # noqa: BLE001 - any parse/validation/eval/stringify failure means "not calculable"
        return None


def equivalate(text: str) -> tuple[str | None, str]:
    """Pure passthrough - the verbatim '==' instruction."""
    return text, ""


def calculate(text: str) -> tuple[str | None, str]:
    """Evaluate each line as arithmetic where possible; leave non-arithmetic lines unchanged.

    Uses a restricted ast-based evaluator (no eval()/exec()) so untrusted
    document content can never execute arbitrary code - only literal numeric
    arithmetic (+ - * / // % ** and unary +/-) is ever computed.
    """
    result_lines = []
    for line in text.split("\n"):
        computed = _try_calculate_line(line)
        result_lines.append(computed if computed is not None else line)
    return "\n".join(result_lines), ""


def copy_instruction(text: str) -> tuple[str | None, str]:
    return None, "Copy: not implemented yet"


def paste_instruction(text: str) -> tuple[str | None, str]:
    return None, "Paste: not implemented yet"


def cut_instruction(text: str) -> tuple[str | None, str]:
    return None, "Cut: not implemented yet"


INSTRUCTIONS = {
    "Equivalate (==)": equivalate,
    "Calculate (=)": calculate,
    "Copy (//)": copy_instruction,
    "Paste (/)": paste_instruction,
    "Cut (-)": cut_instruction,
}
