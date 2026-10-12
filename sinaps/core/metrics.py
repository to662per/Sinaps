"""Метрики задержек.

Требование проекта — ответ за < 1–2 секунды. Без замеров это невозможно
проверить, поэтому каждая стадия пайплайна пишет отметку, а `Metrics`
считает скользящие p50/p95.

Стадии, которые мы измеряем (в порядке):
    vad_end -> stt_done -> intent -> skill -> first_audio
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

STAGES = ("vad_end", "stt_done", "intent", "skill", "first_audio")

#: Главная метрика проекта: от конца фразы до первого звука ответа.
#: Именно её сравнивают с требованием «< 1–2 секунды».
#:
#: Почему не `total`: total включает время, пока пользователь говорил и пока
#: проигрывался входной файл. При записи с микрофона это не задержка
#: системы, а длительность речи — и включать её в SLA бессмысленно.
RESPONSE_STAGE = "response"


@dataclass
class TurnMetrics:
    """Метрики одного диалога (одной фразы пользователя)."""

    marks: dict[str, float] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    def mark(self, stage: str) -> None:
        self.marks.setdefault(stage, time.monotonic())

    @property
    def total(self) -> float:
        last = self.marks.get("first_audio") or self.marks.get("skill")
        return (last - self.started) if last else 0.0

    @property
    def response_time(self) -> float:
        """Задержка ответа: конец речи -> первый звук.

        Это то, что чувствует пользователь и то, что попадает в критерий
        приёмки. Не включает длительность самой речи.
        """
        start = self.marks.get("vad_end")
        end = self.marks.get("first_audio")
        if start is None or end is None or end < start:
            return 0.0
        return end - start

    def deltas(self) -> dict[str, float]:
        """Сколько заняла каждая стадия по отдельности (сек)."""
        out: dict[str, float] = {}
        prev = self.started
        for stage in STAGES:
            if stage in self.marks:
                out[stage] = round(self.marks[stage] - prev, 3)
                prev = self.marks[stage]
        return out

    def summary(self) -> str:
        parts = " ".join(f"{k}={v:.3f}" for k, v in self.deltas().items())
        return f"итого={self.total:.3f}с | {parts}"


class Metrics:
    """Скользящая статистика по последним N диалогам."""

    def __init__(self, window: int = 100) -> None:
        self.window = window
        self._turns: deque[TurnMetrics] = deque(maxlen=window)
        self._stages: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window))

    def new_turn(self) -> TurnMetrics:
        turn = TurnMetrics()
        self._turns.append(turn)
        return turn

    def finish(self, turn: TurnMetrics) -> None:
        for stage, value in turn.deltas().items():
            self._stages[stage].append(value)
        if "first_audio" in turn.marks:
            self._stages["total"].append(turn.total)
            self._stages[RESPONSE_STAGE].append(turn.response_time)

    @staticmethod
    def _p(values: list[float] | deque[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
        return ordered[idx]

    def stats(self, stage: str = "total") -> dict[str, float]:
        vals = self._stages.get(stage, ())
        if not vals:
            return {}
        return {
            "n": len(vals),
            "p50": self._p(vals, 0.50),
            "p95": self._p(vals, 0.95),
            "mean": round(statistics.fmean(vals), 3),
            "max": round(max(vals), 3),
        }

    def report(self) -> str:
        """Человекочитаемый отчёт — печатается по `--report` и в лог."""
        lines = ["Метрики задержек (секунды):"]
        header = f"  {'стадия':<12} {'n':>4} {'p50':>8} {'p95':>8} {'mean':>8} {'max':>8}"
        lines.append(header)
        lines.append("  " + "-" * (len(header) - 2))
        for stage in (*STAGES, RESPONSE_STAGE, "total"):
            s = self.stats(stage)
            if not s:
                continue
            lines.append(
                f"  {stage:<12} {int(s['n']):>4} {s['p50']:>8.3f} {s['p95']:>8.3f} "
                f"{s['mean']:>8.3f} {s['max']:>8.3f}"
            )
        return "\n".join(lines)

    def pass_rate(self, stage: str = "total", threshold: float = 1.6) -> float:
        """Доля диалогов быстрее порога (для критерия приёмки p50 <= 1.6 с)."""
        vals = self._stages.get(stage, ())
        if not vals:
            return 0.0
        return round(sum(1 for v in vals if v <= threshold) / len(vals), 3)
