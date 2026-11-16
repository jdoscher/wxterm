"""Colour themes. Mono themes drive everything from one phosphor hue; 'color' uses conventional radar colours."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    name: str
    bg: str
    fg: str
    dim: str
    bright: str
    accent: str
    warn: str
    levels: tuple[str, ...]          # radar levels 1..5
    snow: str
    faint: str = ""                  # roads, grid
    line: str = ""                   # graph line
    rain: str = ""                   # precip bars
    water: str = ""                  # tide / surf
    land: str = ""                   # coast, terrain

    def c(self, key: str) -> str:
        return getattr(self, key) or {"faint": self.dim, "line": self.bright, "rain": self.fg,
                                      "water": self.fg, "land": self.fg}.get(key, self.fg)


def _mono(name, bg, fg, dim, faint, bright, accent, warn, levels):
    return Theme(name, bg, fg, dim, bright, accent, warn, levels, levels[-1], faint=faint)


THEMES = [
    _mono("AMBER", "#0b0700", "#ffb000", "#8a5a00", "#4a3000", "#ffd98a", "#ffc640", "#ff7b00",
          ("#6a4300", "#a06800", "#d89000", "#ffb000", "#fff0c8")),
    _mono("GREEN", "#020b03", "#33ff66", "#1d8a3a", "#0e4a1e", "#c2ffd2", "#7dff9e", "#d5ff3f",
          ("#145a24", "#1f8a38", "#2fcc55", "#5cff85", "#e0ffe8")),
    _mono("WHITE", "#0c0c0f", "#d6d6cf", "#74746e", "#3a3a37", "#ffffff", "#ffffff", "#ffffff",
          ("#4a4a47", "#7a7a75", "#a8a8a0", "#d6d6cf", "#ffffff")),
    Theme("IBM", "#0000a8", "#aaaaaa", "#5555ff", "#ffffff", "#ffff55", "#ff5555",
          ("#00aa00", "#55ff55", "#ffff55", "#ff5555", "#ff55ff"), "#55ffff",
          faint="#3a3ad8", line="#ffff55", rain="#55ffff", water="#55ffff", land="#aaaaaa"),
    Theme("COLOR", "#0b0c10", "#c9c9c4", "#5d5f66", "#ffffff", "#5fd7ff", "#ffaf00",
          ("#2e7d32", "#43c94a", "#e8e337", "#ff8f1f", "#ff2b2b"), "#9ec9ff",
          faint="#303238", line="#ffaf5f", rain="#5f9fff", water="#4fb4ff", land="#a8a37a"),
]


def by_name(name: str | None) -> int:
    for i, t in enumerate(THEMES):
        if t.name == (name or "").upper():
            return i
    return 0
