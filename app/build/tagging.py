"""Tag derivation.

Kept in one small pure module because both the seed importer (M1) and the build
pipeline (M3) attach the same derived tags, and the gallery's tag chips only
stay useful if the vocabulary is stable rather than model-dependent.
"""

from __future__ import annotations

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "games": ("game", "playable", "score", "high score", "player", "level", "arcade"),
    "productivity": ("timer", "pomodoro", "todo", "task", "note", "calendar", "focus"),
    "design": ("color", "colour", "palette", "font", "layout", "design"),
    "tools": ("convert", "calculator", "generator", "converter", "counter", "formatter"),
    "random": ("random", "dice", "roll", "shuffle", "spinner"),
    "graphics": ("canvas", "animation", "draw", "render", "svg", "parallax"),
    "audio": ("sound", "audio", "synth", "web audio", "beep", "music"),
    "puzzle": ("puzzle", "guess", "solve", "match", "memory game"),
    "interactive": ("click", "drag", "keyboard", "hover", "tap", "input"),
    "single-file": ("single-file", "single file", "self-contained", "index.html"),
}


def derive_tags(text: str, *, limit: int = 6) -> tuple[str, ...]:
    """Keyword-derived tags, ordered by how often the group matched."""
    haystack = text.lower()
    scored: list[tuple[int, str]] = []
    for tag, needles in _KEYWORDS.items():
        hits = sum(haystack.count(needle) for needle in needles)
        if hits:
            scored.append((hits, tag))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return tuple(tag for _, tag in scored[:limit])
