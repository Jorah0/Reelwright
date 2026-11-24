"""Setup-payoff guard: a pick that opens mid-story ("He says…", "And then…", "So we ran…") loses the viewer who never saw
the setup. guard() reaches the start back to the nearest sentence that stands on its own, as long as the clip stays within
the max length and doesn't cross an arc boundary (a long pause, or a hard scene cut between sentences). When the setup
can't fit, the clip is flagged and card() can write a 2-second "Earlier: …" line, shown through the existing headline
banner (card_edit), so rendering needs no change.
"""
from pydantic import BaseModel, Field

from . import ai, media

OPENERS = {"and", "but", "so", "because", "then", "which", "also", "plus", "or", "anyway", "that", "those",
           "it", "he", "she", "they", "him", "her", "them", "his", "its", "their"}
PRONOUNS = {"he", "she", "they", "him", "her", "them", "his", "hers", "their"}  # someone named earlier, not in the clip
PAUSE = 2.0       # seconds of silence between sentences that mark a new arc
CARD_SECONDS = 2.0


def _words(text: str) -> list[str]:
    return [w.strip(".,!?;:\"()…-").split("'")[0] for w in str(text or "").lower().replace("’", "'").split()]


def dangling(text: str) -> bool:
    """The sentence leans on something said before it: opens with a connector or a pronoun, or names a person by
    pronoun in its first few words. ponytail: word lists, not coreference; misses "Honestly, it was the printer"."""
    w = [x for x in _words(text) if x]
    return bool(w) and (w[0] in OPENERS or bool(PRONOUNS & set(w[:6])))


def guard(clips: list[dict], segments: list[dict], max_len: float, signals: dict | None = None) -> list[dict]:
    """Each clip, its start moved back to its setup when that fits ("context": {"extended_from"}), or flagged
    "needs_context" when it can't be reached. Clips that already start on a standalone sentence come back unchanged."""
    cuts = (signals or {}).get("scenes") or []
    out = []
    for c in clips:
        i = next((k for k, s in enumerate(segments) if s["end"] > c["start"] + 0.25), None)
        if i is None or not dangling(segments[i]["text"]):
            out.append(c)
            continue
        j = i
        while j > 0 and dangling(segments[j]["text"]):
            prev, cur = segments[j - 1], segments[j]
            if (c["end"] - prev["start"] > max_len or cur["start"] - prev["end"] >= PAUSE
                    or any(prev["end"] - 0.05 <= t <= cur["start"] + 0.05 for t in cuts)):
                break
            j -= 1
        if dangling(segments[j]["text"]):  # blocked before reaching a standalone sentence: keep the cut, say so
            out.append({**c, "needs_context": True})
        else:
            out.append({**c, "start": segments[j]["start"], "context": {"extended_from": c["start"]}})
    return out


class ContextCard(BaseModel):
    needed: bool = Field(description="true only if a viewer who missed what came before would be lost")
    card: str = Field(description="what happened just before the clip, at most 60 characters, plain words")


CARD_SYSTEM = ("A short clip was cut from a longer video, but it starts after its setup. Write the one line a viewer who missed "
               "the start needs (who or what it is about), at most 60 characters, shown for 2 seconds. No 'Earlier' prefix, "
               "no spoilers of the payoff. If the clip makes sense on its own, say it isn't needed.")


def card(clip: dict, segments: list[dict]) -> str | None:
    """One small, low-effort call (the provider routed for titles) over the 2 minutes before the clip."""
    before = ai.transcript_lines(segments, clip["start"] - 120, clip["start"])
    inside = ai.transcript_lines(segments, clip["start"], clip["end"])
    r = ai._ask(CARD_SYSTEM, f"BEFORE THE CLIP:\n{before or '(nothing)'}\n\nTHE CLIP:\n{inside[:2000]}", ContextCard,
                effort="low", provider=ai.route("meta"))
    text = media.headline_text(r.card.strip().removeprefix("Earlier:").strip(), 60) if r.needed else ""
    return f"Earlier: {text}" if text else None


def card_edit(edit: dict, text: str) -> dict | None:
    """The edit patch that shows the card as a headline for its first seconds; None when the clip has a headline of its own."""
    if ((edit or {}).get("headline") or {}).get("on"):
        return None
    return {"headline": {"on": True, "text": text, "kicker": "", "theme": "auto", "position": "top",
                         "show": "first_n_seconds", "seconds": CARD_SECONDS, "push": False}}
