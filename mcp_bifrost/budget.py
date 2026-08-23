"""
Spending limits.

An orchestration bug — a retry loop, a batch built from a bad symbol list —
must not be able to run up a bill unattended. The limit is a hard stop, not
a warning: once it trips, calls stop.

Counted per Engine instance, which is per session. Deliberately not
persisted: the point is to bound one runaway session, and a limit that
survives restarts would need managing rather than just working.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    max_calls: int = 200
    max_tokens: int = 400_000

    calls: int = field(default=0, init=False)
    tokens_in: int = field(default=0, init=False)
    tokens_out: int = field(default=0, init=False)

    def check(self) -> None:
        """Called before spending. Raises rather than returning a flag so a
        forgotten check cannot silently continue."""
        if self.calls >= self.max_calls:
            raise BudgetExceeded(
                f"call limit reached ({self.calls}/{self.max_calls}). "
                f"Raise max_calls if this batch is genuinely that large."
            )
        total = self.tokens_in + self.tokens_out
        if total >= self.max_tokens:
            raise BudgetExceeded(
                f"token limit reached ({total:,}/{self.max_tokens:,})."
            )

    def spend(self, tokens_in: int | None, tokens_out: int | None) -> None:
        self.calls += 1
        self.tokens_in += tokens_in or 0
        self.tokens_out += tokens_out or 0

    def summary(self) -> str:
        return (f"{self.calls}/{self.max_calls} calls, "
                f"{self.tokens_in + self.tokens_out:,}/{self.max_tokens:,} tokens")
