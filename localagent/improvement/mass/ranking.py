"""Within-task tournaments and Bradley-Terry ranking (MASS self-evaluation)."""
from __future__ import annotations

import math
import random


def tournament(ids: list[str], opponents: int = 6, seed: int = 0) -> list[tuple[str, str]]:
    """A connected schedule where each candidate meets ~``opponents`` others.

    A random ring guarantees connectivity (so BT strengths are comparable);
    extra random pairings fill up to the requested degree.
    """
    n = len(ids)
    if n < 2:
        return []
    rng = random.Random(seed)
    order = ids[:]
    rng.shuffle(order)
    pairs: set[frozenset[str]] = set()
    for i in range(n):
        if n == 2 and i == 1:
            break
        pairs.add(frozenset((order[i], order[(i + 1) % n])))
    target = min(opponents, n - 1)
    degree = {x: 0 for x in ids}
    for p in pairs:
        for x in p:
            degree[x] += 1
    candidates = [frozenset((a, b)) for i, a in enumerate(ids) for b in ids[i + 1:]]
    rng.shuffle(candidates)
    for p in candidates:
        a, b = tuple(p)
        if p not in pairs and degree[a] < target and degree[b] < target:
            pairs.add(p)
            degree[a] += 1
            degree[b] += 1
    out = []
    for p in sorted(pairs, key=lambda p: sorted(p)):
        a, b = sorted(p)
        out.append((a, b) if rng.random() < 0.5 else (b, a))
    return out


def bradley_terry(ids: list[str], results: list[tuple[str, str, str]], *, iters: int = 500,
                  prior: float = 0.5) -> dict[str, float]:
    """Fit BT log-strengths from ``(a, b, winner)`` where winner is a, b or "tie".

    Uses the MM algorithm; ``prior`` adds virtual half-wins against an
    average opponent so undefeated/winless candidates stay finite.
    """
    idx = {x: i for i, x in enumerate(ids)}
    n = len(ids)
    wins = [[0.0] * n for _ in range(n)]
    for a, b, w in results:
        if a not in idx or b not in idx:
            continue
        i, j = idx[a], idx[b]
        if w == a:
            wins[i][j] += 1
        elif w == b:
            wins[j][i] += 1
        else:
            wins[i][j] += 0.5
            wins[j][i] += 0.5
    p = [1.0] * n
    for _ in range(iters):
        new = []
        for i in range(n):
            num = sum(wins[i]) + prior
            den = prior * 2 / (p[i] + 1.0)
            for j in range(n):
                if i != j and (games := wins[i][j] + wins[j][i]):
                    den += games / (p[i] + p[j])
            new.append(num / den if den > 0 else p[i])
        mean = sum(new) / n
        p = [x / mean for x in new]
    return {x: math.log(p[idx[x]]) for x in ids}
