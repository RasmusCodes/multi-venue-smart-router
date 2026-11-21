"""Deterministic math and random streams.

Everything that drives a simulation run uses IEEE-754 double arithmetic only
(+, -, *, /, and exact helpers such as ``math.frexp``/``math.ldexp``). The
platform C library's ``log``/``exp`` are not used on the simulation path because
their last bit may differ between C libraries, and a one-ulp difference in an
inter-arrival time can change the order of two events. With these helpers a run
is bit-identical on CPython for Windows, Linux and macOS and on Pyodide
(CPython compiled to WebAssembly), which is what lets the browser demo
reproduce the numbers in the research report seed for seed.
"""

from __future__ import annotations

import math
import random

LN2 = 0.6931471805599453
_SQRT_HALF = 0.7071067811865476
_MASK64 = 0xFFFFFFFFFFFFFFFF


def det_log(x: float) -> float:
    """Natural logarithm from basic arithmetic only (|error| of a few ulp)."""
    if not x > 0.0:
        raise ValueError("det_log needs a positive argument")
    m, e = math.frexp(x)  # x = m * 2**e, m in [0.5, 1); exact
    if m < _SQRT_HALF:
        m *= 2.0
        e -= 1
    # m in [0.7071, 1.4142): ln(m) = 2*atanh(s), s = (m-1)/(m+1), |s| <= 0.1716
    s = (m - 1.0) / (m + 1.0)
    s2 = s * s
    term = s
    acc = 0.0
    n = 1.0
    for _ in range(12):
        acc += term / n
        term *= s2
        n += 2.0
    return 2.0 * acc + e * LN2


def det_exp(x: float) -> float:
    """exp(x) from basic arithmetic only."""
    if x < -745.0:
        return 0.0
    if x > 709.0:
        raise OverflowError("det_exp argument too large")
    k = int(x / LN2 + (0.5 if x >= 0.0 else -0.5))
    # Cody-Waite reduction: ln 2 split in a high part exact in k * hi and a low correction
    r = (x - k * 6.93147180369123816490e-01) - k * 1.90821492927058770002e-10  # |r| <= ~0.35
    term = 1.0
    acc = 1.0
    for n in range(1, 19):
        term *= r / n
        acc += term
    return math.ldexp(acc, k)


def fnv1a64(data: bytes) -> int:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & _MASK64
    return h


def splitmix64(x: int) -> int:
    x = (x + 0x9E3779B97F4A7C15) & _MASK64
    z = x
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
    return z ^ (z >> 31)


def derive_seed(master: int, name: str) -> int:
    """Stable 64-bit seed for a named stream (never Python's salted hash())."""
    return splitmix64((fnv1a64(name.encode("utf-8")) ^ splitmix64(master & _MASK64)) & _MASK64)


def hash_uniform(master: int, *keys: int) -> float:
    """Counter-based uniform in [0, 1) keyed by integers.

    Used for draws that must stay aligned across strategies run on the same
    seed (common random numbers): e.g. the reaction delay of venue v to the
    k-th news jump is the same whatever our router did before it.
    """
    h = splitmix64(master & _MASK64)
    for k in keys:
        h = splitmix64((h ^ (k & _MASK64)) & _MASK64)
    return (h >> 11) * (1.0 / 9007199254740992.0)


class Stream:
    """A named random stream: Mersenne Twister (platform-stable) + det_log."""

    __slots__ = ("random", "_rng")

    def __init__(self, master: int, name: str):
        self._rng = random.Random(derive_seed(master, name))
        self.random = self._rng.random

    def exp(self, mean: float) -> float:
        """Exponential draw with the given mean."""
        return -det_log(1.0 - self.random()) * mean

    def normal(self) -> float:
        """Standard normal draw (Marsaglia polar method on det_log and sqrt)."""
        r = self.random
        while True:
            u = 2.0 * r() - 1.0
            v = 2.0 * r() - 1.0
            s = u * u + v * v
            if 0.0 < s < 1.0:
                return u * math.sqrt(-2.0 * det_log(s) / s)


def exp_from_uniform(u: float, mean: float) -> float:
    return -det_log(1.0 - u) * mean


def pick(cdf: list[float], u: float) -> int:
    """Index of the first cumulative probability above u."""
    for i, c in enumerate(cdf):
        if u < c:
            return i
    return len(cdf) - 1


def cumulative(probs: list[float]) -> list[float]:
    out, acc = [], 0.0
    for p in probs:
        acc += p
        out.append(acc)
    out[-1] = 1.0
    return out
