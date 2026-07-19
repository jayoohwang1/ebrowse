"""Example flexible evaluator: scores process, not just the final answer."""

from ebrowse_evals.tasks import EvalResult
from ebrowse_evals.trace.store import TraceReader


def evaluate(trace: TraceReader) -> EvalResult:
    steps = trace.steps()
    reached_detail = any("detail" in (s.browser.get("url") or "") for s in steps)
    errors = [s for s in steps if s.error is not None]
    return EvalResult(
        success=reached_detail,
        score=None,
        details={"steps": len(steps), "errors": len(errors)},
    )
