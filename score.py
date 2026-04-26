"""Ranking and breakout scoring for StarPulse repos."""
import math


def compute_score(
    stars: int,
    forks: int,
    open_issues: int,
    delta_7d: int,
) -> float:
    base = (
        math.log10(max(stars, 1)) * 1.0
        + math.log10(max(forks, 1)) * 0.5
        + math.log10(max(open_issues, 1)) * 0.25
    )
    vel_bonus = (delta_7d / max(stars, 1)) * 5.0
    return round(base + vel_bonus, 4)


def is_breakout(stars: int, delta_7d: int) -> bool:
    prior = max(stars - delta_7d, 1)
    return (delta_7d / prior) > 0.15
