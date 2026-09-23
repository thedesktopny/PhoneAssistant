"""What of a conversation may be written down.

From the moment the assistant asks for a password or PIN until it has
what it needs, neither side's words are recorded - the caller spelling
it, or the assistant reading it back. Call 67 put a password in the call
log in pieces: scrub() reads one line at a time, and "lowercase e s k t o
p" is not a password on its own. This reads the conversation in order.

Shared on purpose: the voice side uses it as it records, and the backend
uses the very same rule to clean what was recorded before it existed.
No imports beyond re, so both can load it.
"""
import re


SECRET_WORD = re.compile(r"(?i)\b(password|passcode|pass word|pin|p\.i\.n)\b")
SECRET_DONE = re.compile(r"(?i)\b(saved|stored|verified|confirmed|accepted|"
                         r"correct|incorrect|didn't match|doesn't match)\b")
# A read-back or a spelling: single letters or digits one after another,
# "capital", "lowercase", or a run of digits.
SPELLING = re.compile(r"(?i)\b(capital|lowercase|lower case|uppercase|"
                      r"upper case|symbol|underscore|exclamation|hashtag|"
                      r"at sign)\b|\b\w\b(?:[\s,.-]+\b\w\b){2,}|\d{2,}")
BLANKED = "[a password or PIN was being given here - not recorded]"
# Said while they are still giving it. Call 67: "take your time and let
# me know the next characters" has no "password" in it, the old rule
# took it as the end, and the next spelled piece was written down.
STILL_GIVING = re.compile(
    r"(?i)(take your time|no rush|go ahead|when(ever)? you'?re ready|"
    r"ready when|next (part|characters?|letters?|digits?|bit|one)|so far|"
    r"keep going|continue|carry on|still here|i'?m here|one (moment|second)|"
    r"what comes next|the rest)")


def keep_or_blank(state: dict, role: str, text: str) -> str:
    """What may be written down of one turn.

    Call 67: a password spelled a few characters at a time reached the
    call log in pieces, because scrub() reads one line and "lowercase e s k
    t o p" is not a password on its own. This knows the conversation: from
    the moment the assistant asks for a password or PIN until it has what
    it needs, neither side's words are recorded. It fails towards blanking
    - a lost line of transcript costs nothing; a written password does."""
    if role == "user":
        if state["on"]:
            state["turns"] += 1
            if state["turns"] > 40:          # never stuck on for a whole call
                state["on"] = False
            return BLANKED
        # Given before anyone asked - call 57: "...and the password is"
        # straight after the username, spelled out.
        if SECRET_WORD.search(text) and SPELLING.search(text):
            state["on"], state["turns"] = True, 0
            return BLANKED
        return text
    asks = bool(SECRET_WORD.search(text))
    spelled = bool(SPELLING.search(text))
    if spelled and (state["on"] or asks):
        state["on"], state["turns"] = True, 0
        return BLANKED
    if asks and SECRET_DONE.search(text):
        state["on"] = False                  # "your PIN is confirmed"
        return text
    if asks:
        state["on"], state["turns"] = True, 0
        return text                          # the question itself is safe
    if state["on"] and STILL_GIVING.search(text):
        return text                          # "take your time" - still on
    # "Got it." - a short acknowledgement is not the end. Call 68: the
    # caller said the PIN a third time after it and it was written down.
    if state["on"] and len(text.split()) <= 4 and not SECRET_DONE.search(text):
        return text
    state["on"] = False
    return text
