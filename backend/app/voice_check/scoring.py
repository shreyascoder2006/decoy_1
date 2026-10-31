"""Turn per-window detector scores for one voice note into a level, a score and reasons.

Pure functions, no model and no I/O, so the rules are unit testable. The policy
mirrors the live-call one: a single synthetic window is only a weak signal, and
a *run* of them is what raises the level to high. Skipped (too quiet) windows
are never counted and never break a run.
"""

from __future__ import annotations

from dataclasses import dataclass

LEVEL_UNKNOWN = "unknown"
LEVEL_LOW = "low"
LEVEL_MEDIUM = "medium"
LEVEL_HIGH = "high"


@dataclass(frozen=True)
class WindowScore:
    start_s: float
    end_s: float
    # P(synthetic) from the detector; None when the window was too quiet to judge.
    p_fake: float | None


@dataclass(frozen=True)
class VoiceSummary:
    level: str
    score: int
    reasons: list[str]
    analysed: int
    flagged: int
    longest_run: int
    skipped: int


def longest_run(windows: list[WindowScore], threshold: float) -> int:
    """Longest run of synthetic windows; skipped windows neither extend nor break it."""
    best = current = 0
    for window in windows:
        if window.p_fake is None:
            continue
        if window.p_fake >= threshold:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def summarise(
    windows: list[WindowScore], *, threshold: float, min_run: int, window_seconds: float
) -> VoiceSummary:
    scored = [w for w in windows if w.p_fake is not None]
    skipped = len(windows) - len(scored)
    if not scored:
        return VoiceSummary(
            level=LEVEL_UNKNOWN,
            score=0,
            reasons=["Not enough speech in this voice note to judge."],
            analysed=0,
            flagged=0,
            longest_run=0,
            skipped=skipped,
        )

    flagged = sum(1 for w in scored if w.p_fake >= threshold)
    run = longest_run(windows, threshold)
    average = sum(w.p_fake for w in scored) / len(scored)
    highest = max(w.p_fake for w in scored)

    if run >= min_run:
        level = LEVEL_HIGH
    elif flagged:
        level = LEVEL_MEDIUM
    else:
        level = LEVEL_LOW

    score = round(100 * flagged / len(scored))
    reasons = [
        f"{flagged} of {len(scored)} analysed windows (about {window_seconds:g} "
        f"seconds each) were classified as synthetic speech. The longest unbroken "
        f"run was {run}; a run of {min_run} or more is treated as high risk "
        f"because a single window is a weak signal.",
        f"Detector confidence across windows: average {round(100 * average)}%, "
        f"highest {round(100 * highest)}%. This is the model's own score, not a "
        f"calibrated probability.",
    ]
    if skipped:
        reasons.append(
            f"{skipped} window(s) were too quiet to judge and were not counted."
        )
    if level == LEVEL_LOW:
        reasons.append(
            "No synthetic signal was found in the audio analysed. That does not "
            "prove the voice is genuine."
        )
    else:
        reasons.append(
            "Why this matters: AI-generated voices are used in impersonation and "
            "payment-fraud messages. The detector looks for speech patterns typical "
            "of synthetic voices; it cannot prove who is speaking."
        )
    return VoiceSummary(
        level=level,
        score=score,
        reasons=reasons,
        analysed=len(scored),
        flagged=flagged,
        longest_run=run,
        skipped=skipped,
    )
