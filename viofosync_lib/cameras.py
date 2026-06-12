"""Camera registry — the single source of truth for Viofo lenses.

A clip's filename ends ``…NNNNN[PE]?<letter>.MP4`` where the
optional P/E prefix encodes parking/event and ``<letter>`` is the
camera. 2-channel models record F+R; 3-channel models add either
T (telephoto, e.g. A329) or I (interior, e.g. A139 / A229 3CH).

Everything camera-shaped derives from :data:`CAMERAS`: the
download/scan glob, the queue's filename regexes, timeline
channel keys and labels, archive pair slots, and the per-camera
export job types (see ``web/services/naming.py`` for the
app-level derivations). Adding a future lens means adding one
:class:`Camera` line here plus its UI strings.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Camera:
    letter: str   # filename suffix letter (the F in ``…0001F.MP4``)
    channel: str  # stable machine key: timeline channel / pair slot
    label: str    # human-facing label


# Declaration order is display order (timeline tracks, UI cycles).
CAMERAS: tuple[Camera, ...] = (
    Camera("F", "front", "Front"),
    Camera("R", "rear", "Rear"),
    Camera("T", "tele", "Tele"),
    Camera("I", "interior", "Interior"),
)

# "FRTI" — drops straight into regex/glob character classes.
CAMERA_LETTERS = "".join(c.letter for c in CAMERAS)

CHANNEL_FOR_LETTER = {c.letter: c.channel for c in CAMERAS}


def channel_of(camera: str | None) -> str:
    """Map a clip's ``camera`` code to its channel key.

    The lens is the trailing letter of the code, so parking/event
    prefixes (PF, ET, PI, …) resolve to the same channel as the
    bare letter. Anything unrecognised falls back to ``"other"``
    so an unexpected code still gets its own track rather than
    vanishing.
    """
    if not camera:
        return "other"
    return CHANNEL_FOR_LETTER.get(camera[-1].upper(), "other")


def pair_slot_of(camera: str | None) -> str:
    """Like :func:`channel_of`, but with the pairers' historical
    ``"rear"`` fallback: when the archive day view and the export
    pairer group same-capture clips into slots, an unknown letter
    has always been filed under rear rather than given its own
    slot."""
    if not camera:
        return "rear"
    return CHANNEL_FOR_LETTER.get(camera[-1].upper(), "rear")
