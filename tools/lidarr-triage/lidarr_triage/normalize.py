"""Name normalization shared by Spotify and Lidarr sides.

Spotify gives names and URIs, never MusicBrainz IDs, so matching is on
normalized text. Edition/remaster/live qualifiers are stripped so
"Abbey Road (Remastered 2019)" and "Abbey Road" compare equal; the fuzzy
matcher handles what's left.
"""

from __future__ import annotations

import re
import unicodedata

# Words that mark a bracketed or dash-suffixed qualifier as noise.
_QUALIFIER = (
    r"(?:remaster(?:ed)?|deluxe|expanded|anniversary|edition|live|mono|stereo|"
    r"bonus\s+track|special|collector'?s|reissue)"
)
# "(feat. X)", "[ft. X]", "(with X)"
_FEAT_BRACKET = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+[^\)\]]*[\)\]]", re.I)
# trailing " feat. X" / " ft. X" / " featuring X" (no brackets)
_FEAT_TRAIL = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.I)
# any bracketed group containing a qualifier word: "(Deluxe Edition)", "[2011 Remaster]", "(Live)"
_QUAL_BRACKET = re.compile(r"\s*[\(\[][^\)\]]*\b" + _QUALIFIER + r"\b[^\)\]]*[\)\]]", re.I)
# " - Remastered 2011", " - 2011 Remaster", " - Live at Wembley", " - Mono"
_QUAL_DASH = re.compile(r"\s+[-–—]\s+[^-–—]*\b" + _QUALIFIER + r"\b.*$", re.I)
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()


def normalize(text: str) -> str:
    """Normalize an album or track title."""
    if not text:
        return ""
    s = _FEAT_BRACKET.sub("", text)
    s = _FEAT_TRAIL.sub("", s)
    # Qualifiers can stack: "Song (Live) - 2011 Remaster"
    for _ in range(3):
        before = s
        s = _QUAL_DASH.sub("", s)
        s = _QUAL_BRACKET.sub("", s)
        if s == before:
            break
    s = _fold(s).replace("&", " and ")
    s = _NON_WORD.sub(" ", s)
    s = _SPACE.sub(" ", s).strip()
    # Never normalize a title away entirely ("(Live)" as a whole title).
    return s or _SPACE.sub(" ", _NON_WORD.sub(" ", _fold(text))).strip()


# Qualifiers that mean a *different recording*, not just a different release.
_RECORDING_TAGS = frozenset({"live", "mono", "stereo"})


def light(text: str) -> str:
    """Casefold + punctuation only; keeps qualifiers. Used to tie-break."""
    s = _fold(text or "").replace("&", " and ")
    return _SPACE.sub(" ", _NON_WORD.sub(" ", s)).strip()


def recording_tags(text: str) -> frozenset[str]:
    """Recording-level qualifiers that `normalize` stripped from `text`,
    e.g. "Song (Live)" -> {"live"}; "Song - 2011 Remaster" -> {}."""
    stripped = set(light(text).split()) - set(normalize(text).split())
    return frozenset(stripped & _RECORDING_TAGS)


def normalize_artist(text: str) -> str:
    """Normalize an artist name (no qualifier stripping; feat. still removed)."""
    if not text:
        return ""
    s = _FEAT_BRACKET.sub("", text)
    s = _FEAT_TRAIL.sub("", s)
    s = _fold(s).replace("&", " and ")
    s = _NON_WORD.sub(" ", s)
    return _SPACE.sub(" ", s).strip()
