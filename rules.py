"""What the assistant will not discuss, and how it refuses.

Kept in one place because it is enforced twice over: in the voice
model's instructions, and here on the server, so a blocked subject
cannot arrive through a search result or a page instead.
"""
from core import *                                   # noqa: F401,F403
from core import _re_scrub


BLOCKED_TERMS = {
    "sex", "sexual", "sexy", "porn", "pornography", "nude", "nudity", "naked",
    "erotic", "explicit", "intimacy", "intimate", "arousal", "arousing",
    "adultery", "affair", "underwear", "lingerie", "bikini", "puberty",
    "fertility", "dating", "tinder", "hookup", "romance", "romantic",
    "marriage counseling", "relationship advice",
    # Jewish religious subjects are ALLOWED - this service is for Jewish
    # callers. What stays blocked is weighing faiths against each other.
    # Only unambiguous phrases here: single words like "church" or
    # "christian" would block a Brooklyn street name or somebody's name.
    # These must be phrases nobody says innocently. "which religion" is
    # NOT one: "which religion is the name Raizi from" is etymology, and
    # blocking it refused a caller twice.
    "other religions", "compare religions", "religions compared",
    "other faiths", "which religion is right", "which religion is true",
    "which religion is the true", "which religion is better",
    "best religion", "true religion",
    "gossip", "celebrity", "gossip column",
    "addiction", "drugs", "rehab",
    "joke", "jokes", "humor", "funny",
    "news", "headlines", "sports", "score", "game", "movie", "movies",
    "netflix", "tv show", "music video", "entertainment",
}


def blocked_terms_in(text: str) -> set:
    """Which forbidden words actually appear. Counting them matters: one
    stray word in a web snippet is not the same as a page about it."""
    t = " " + (text or "").lower().replace("-", " ") + " "
    return {term for term in BLOCKED_TERMS
            if f" {term} " in t or t.strip() == term}


def is_blocked(text: str) -> bool:
    """For something the CALLER said, or an answer we are about to read
    out. One word is enough here - they chose those words."""
    return bool(blocked_terms_in(text))


# What a tool returns when a blocked topic comes up. The browser path has to
# refuse exactly like web search does - the rule is not the prompt's job.
BLOCKED_REPLY = ("BLOCKED. Say exactly: I am not allowed to talk to you "
                 "about this. Nothing else.")
