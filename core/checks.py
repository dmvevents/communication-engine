"""core/checks.py — health checks that cannot silently no-op (gate G4; R5, R6).

This module exists because of a measured defect that hit **three times in one session**:

  1. The incumbent watchdog's inbox check read a field name (`.timestamp`) the events never
     carried (`.ts`). It emitted neither PASS nor FAIL for weeks while the summary line kept
     reading "OK — 7 checks passed".
  2. A secret-scanning gate returned PASS when its candidate file list was empty.
  3. A liveness probe read an action-only log as staleness.

All three share one shape: **a check that cannot register failure**. The framework here makes
that shape impossible rather than asking reviewers to notice it:

  * a check function MUST return a Verdict; returning None is itself a FAIL
  * a check that raises is a FAIL (with the exception recorded), never a skip
  * a check that inspected NOTHING must say so — `Verdict.passed(...)` requires a non-zero
    `inspected` count, so "nothing to check" cannot masquerade as success
  * `run_all()` asserts every registered check produced exactly one verdict
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


class CheckContractError(RuntimeError):
    """A check violated the framework contract (e.g. claimed PASS on zero evidence)."""


@dataclass(frozen=True)
class Verdict:
    name: str
    ok: bool
    detail: str = ""
    inspected: int = 0

    @staticmethod
    def passed(name: str, inspected: int, detail: str = "") -> "Verdict":
        """A PASS must have looked at something.

        `inspected` is the count of items the check actually examined. Zero means the check
        had no evidence, which is the vacuous-pass bug — refused here at construction time.
        """
        if inspected <= 0:
            raise CheckContractError(
                f"check {name!r} claimed PASS having inspected {inspected} items — an empty "
                "candidate set is a hard error, never a pass")
        return Verdict(name=name, ok=True, detail=detail, inspected=inspected)

    @staticmethod
    def failed(name: str, detail: str, inspected: int = 0) -> "Verdict":
        return Verdict(name=name, ok=False, detail=detail, inspected=inspected)

    def __str__(self) -> str:
        return f"[{'PASS' if self.ok else 'FAIL'}] {self.name}: {self.detail}"


@dataclass
class Registry:
    checks: dict = field(default_factory=dict)

    def add(self, name: str, fn: Callable[[], Verdict]) -> None:
        self.checks[name] = fn

    def run_all(self) -> list[Verdict]:
        """Run every check. Guarantees len(results) == len(checks), each a real verdict."""
        results = []
        for name, fn in self.checks.items():
            try:
                v = fn()
            except CheckContractError:
                raise                       # a contract breach is a bug, not a health signal
            except Exception as ex:          # noqa: BLE001 — a raising check is a FAILURE
                v = Verdict.failed(name, f"check raised {type(ex).__name__}: {ex}")
            if v is None:
                v = Verdict.failed(name, "check returned None — emitted no verdict")
            if not isinstance(v, Verdict):
                v = Verdict.failed(name, f"check returned {type(v).__name__}, not a Verdict")
            results.append(v)
        if len(results) != len(self.checks):
            raise CheckContractError(
                f"{len(self.checks)} checks registered but {len(results)} verdicts produced")
        return results

    @staticmethod
    def summary(results: list[Verdict]) -> str:
        bad = [r for r in results if not r.ok]
        head = "OK" if not bad else f"FAIL ({len(bad)}/{len(results)})"
        return f"{head} — " + " ".join(
            f"{r.name}{'' if r.ok else '(FAIL)'}" for r in results)


# ---- reusable check builders ------------------------------------------------
def schema_check(name: str, records: list, required_fields: tuple) -> Verdict:
    """Every record must carry every required field.

    This is the `.timestamp` vs `.ts` bug as a check: a renamed field produces a FAIL with
    the offending field named, not silence.
    """
    if not records:
        return Verdict.failed(name, "no records to inspect — cannot conclude health")
    missing = {}
    for i, rec in enumerate(records):
        for f in required_fields:
            if f not in rec:
                missing.setdefault(f, []).append(i)
    if missing:
        return Verdict.failed(
            name, "records missing required field(s): "
                  + ", ".join(f"{f} (x{len(idx)})" for f, idx in missing.items()),
            inspected=len(records))
    return Verdict.passed(name, inspected=len(records),
                          detail=f"{len(records)} records carry {len(required_fields)} fields")


def watcher_source_check(name: str, source_desc: str,
                         source_exists: Callable[[], bool]) -> Verdict:
    """R6 / the F-1 class: a watcher whose SOURCE vanished must alert, not poll forever.

    The incumbent host runs a reply-scraper watching a tmux pane that no longer exists. It
    is inert only by accident, and would reactivate silently if that pane name were reused.
    A watcher that cannot see its own source is unhealthy by definition.
    """
    try:
        exists = source_exists()
    except Exception as ex:  # noqa: BLE001
        return Verdict.failed(name, f"source probe raised {type(ex).__name__}: {ex}")
    if not exists:
        return Verdict.failed(
            name, f"watcher source is GONE: {source_desc} — the watcher is a zombie "
                  "(it will poll forever, and would reactivate silently if the source "
                  "name is ever reused)")
    return Verdict.passed(name, inspected=1, detail=f"source present: {source_desc}")


def freshness_check(name: str, age_s: float | None, budget_s: float | None,
                    action_only_log: bool = False) -> Verdict:
    """Age-vs-budget, with the action-only-log trap made explicit.

    A log that is written ONLY when something happens is healthy when silent. Using its age
    as liveness produced a false DEGRADED on a perfectly healthy watchdog (6,324 lines, all
    restarts). Callers must state which kind of source this is.
    """
    if action_only_log:
        return Verdict.passed(
            name, inspected=1,
            detail="action-only source: silence is health, so age is not a health signal")
    if age_s is None:
        return Verdict.failed(name, "no evidence timestamp available — cannot conclude health")
    if budget_s is None:
        return Verdict.failed(name, f"no cadence budget defined for {name} — age "
                                    f"{age_s:.0f}s cannot be judged")
    if age_s > budget_s:
        return Verdict.failed(name, f"stale: {age_s:.0f}s exceeds budget {budget_s:.0f}s",
                              inspected=1)
    return Verdict.passed(name, inspected=1,
                          detail=f"{age_s:.0f}s within budget {budget_s:.0f}s")
