"""Which model answers texts best? Real texts that went wrong, replayed
through /text/try against each model. Nothing is sent, saved or
scheduled. Needs SERVICE_TOKEN in the environment.

    python textbench.py                      # the default models
    python textbench.py gpt-4o-mini gpt-5.6-luna:low
    python textbench.py gpt-5.6-luna:none x3     # each case three times

A model may carry a thinking effort after a colon (GPT-5 family only).
Each case is scored by plain rules, not by another model's opinion; the
cost is OpenAI's own price for the tokens it used.
"""
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BACKEND = os.environ.get("BACKEND_URL",
                         "https://web-production-13961.up.railway.app")
TOKEN = os.environ.get("SERVICE_TOKEN", "")

# dollars per million tokens, in / out - developers.openai.com, 9 Oct 2026
PRICES = {"gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00),
          "gpt-4.1-mini": (0.40, 1.60), "gpt-4.1": (2.00, 8.00),
          "gpt-5-mini": (0.25, 2.00), "gpt-5-nano": (0.05, 0.40),
          "gpt-5.4-mini": (0.75, 4.50), "gpt-5.4-nano": (0.20, 1.25),
          "gpt-5.6-luna": (0.20, 1.20), "gpt-5.6-terra": (2.00, 12.00),
          "gpt-5.1": (1.25, 10.00)}

REPEAT = 1
DEFAULT_MODELS = ["gpt-4o-mini", "gpt-4.1-mini", "gpt-5-mini:low",
                  "gpt-5.4-mini", "gpt-5.6-luna:none", "gpt-5.6-terra:none"]

NOT_ALLOWED = "I am not allowed to talk to you about this."
OTHER_PARSHAS = re.compile(r"(?i)\b(noach|noah|lech|lecha|vayera|chayei)\b")
CANT = re.compile(r"(?i)\b(can.?t|cannot|unable to|not able to)\s+"
                  r"(learn|remember|change|send|listen|hear|see|view)\b")
LINK = re.compile(r"(?i)https?://|www\.|\]\(")
TRACTATE_PAGE = re.compile(
    r"(?i)\b(berachos|shabbos|shabbat|eruvin|pesachim|yoma|sukkah|beitzah|"
    r"megillah|chagigah|yevamos|kesubos|ketubot|nedarim|sotah|gittin|"
    r"kiddushin|bava \w+|sanhedrin|makkos|shevuos|zevachim|menachos|"
    r"menachot|chullin|tamid|shekalim)\s+(daf\s+)?\d{1,3}\s*[ab]?\b")
UNSURE = re.compile(r"(?i)not (sure|certain)|don.?t know|(couldn.?t|could "
                    r"not) (find|confirm|verify)|can.?t confirm|unsure")
# "not found" turned into "it doesn't exist" - of a Mishnah (9 Oct)
DENIES = re.compile(r"(?i)legend|no basis|not in the (talmud|gemara|"
                    r"sources)|(does not|doesn.?t) (exist|appear)|not found "
                    r"in (the )?(talmud|gemara|classic)|no such")


def post(path, body, timeout=180):
    req = urllib.request.Request(
        BACKEND + path, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(path, timeout=60):
    req = urllib.request.Request(BACKEND + path,
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def used(out, name):
    return any(t["name"] == name for t in out.get("tools") or [])


def hebrew_days():
    """Today's and tonight's Hebrew day number, from the calendar."""
    try:
        d = get("/everyday/jewish?account_id=1&what=hebrew_date")
        return {str(d["today"]["day"]), str(d["tonight"]["day"])}
    except Exception:
        return set()


DAYS = hebrew_days()


def _parsha(out):
    r = out["reply"]
    ok = re.search(r"(?i)bere(i)?sh(i)?(s|t)", r) and not OTHER_PARSHAS.search(r)
    return bool(ok), "names Bereishis, no other parsha"


def _date(out):
    r = out["reply"]
    ok = "Tishrei" in r and any(re.search(rf"\b{d}(st|nd|rd|th)?\b", r)
                                for d in DAYS)
    return bool(ok), f"Hebrew day is one of {sorted(DAYS)} Tishrei"


def _gemara(out):
    r = out["reply"]
    pages = TRACTATE_PAGE.findall(r)
    named_other = [p for p in pages if p[0].lower() != "pesachim"]
    ok = (not named_other) and not DENIES.search(r) and (
        re.search(r"(?i)pesachim", r) or UNSURE.search(r))
    return bool(ok), ("Pesachim, or honestly unsure - never another page, "
                      "never 'it isn't in the sources'")


def _schedule(out):
    # "730" late in the day may mean tomorrow - asking which day is fair
    asked = "?" in out["reply"] and re.search(
        r"(?i)\b(date|day|tomorrow|today|morning)\b", out["reply"])
    return bool(used(out, "send_text_later") or asked), \
        "scheduled, or asked which day"


def _learn(out):
    return not CANT.search(out["reply"]), "no false 'I can't'"


def _pictures(out):
    r = out["reply"]
    ok = (not LINK.search(r) and out.get("pictures", 0) > 0
          and not CANT.search(r))
    return bool(ok), "a picture sent, no links"


def _boss(out):
    return used(out, "leave_note_for_office"), "passed on with a note"


def _any_picture(out):
    ok = NOT_ALLOWED not in out["reply"] and (
        out.get("pictures", 0) > 0 or used(out, "send_picture_of"))
    return bool(ok), "sends a picture, not the blocked line"


def _yiddish(out):
    return bool(re.search(r"[֐-׿]", out["reply"])), \
        "answers in Yiddish"


def _candles(out):
    r = out["reply"]
    gave_time = re.search(r"\b\d{1,2}:\d{2}\b", r)
    ok = used(out, "jewish_calendar") or not gave_time
    return bool(ok), "no candle time from memory"


def _price(out):
    r = out["reply"]
    ok = used(out, "find_best_price") or not re.search(r"\$\d", r)
    return bool(ok), "no price without looking"


CASES = [
    ("parsha", "Tell me some Torah for this Shabbos.", [], _parsha),
    ("parsha after a correction", "You're not right, it's not Noach this "
     "Shabbos.", [
         {"who": "user", "text": "Tell me some Torah for this Shabbos."},
         {"who": "assistant", "text": "This Shabbos, we read Parshas Noach. "
          "It discusses the flood and the Ark."}], _parsha),
    ("Hebrew date", "Also, what time is now, and what day in the month, and "
     "the Jewish, and the general.", [], _date),
    ("Gemara source", "In which Gemara do we find that they used to put "
     "bread outside in the Temple as a sign?", [], _gemara),
    ("scheduling", "And e me a nice good morning message 730", [], _schedule),
    ("scheduling after a refusal", "And e me a nice good morning message 730",
     [{"who": "user", "text": "Send me a message today at 7.30 in the "
       "morning, a good morning message."},
      {"who": "assistant", "text": "I can't send messages or reminders at "
       "specific times."}], _schedule),
    ("learning", "You may also learn the stuff to be human on text", [],
     _learn),
    ("shop pictures", "Please send me now a few pictures from the Amazon 4x6 "
     "thermal label, MUNBYN.", [], _pictures),
    ("pass it on", "Give over for your boss that this AI is in very early "
     "phases as you're not accommodating on what I'm asking you.", [], _boss),
    ("any picture", "Can you send me any picture", [], _any_picture),
    ("Yiddish", "דו פארשטייסט אויך אידיש?", [], _yiddish),
    ("candle lighting", "What time is candle lighting this Friday?", [],
     _candles),
    ("price", "How much are the MUNBYN 4x6 thermal labels on Amazon?", [],
     _price),
]


def run(model_spec, case):
    model, _, effort = model_spec.partition(":")
    name, text, history, judge = case
    try:
        out = post("/text/try", {"account_id": 1, "text": text,
                                 "model": model, "effort": effort,
                                 "history": history})
    except Exception as e:
        out = {"reply": "", "error": str(e)[:200]}
    if out.get("error"):
        return model_spec, name, False, f"ERROR {out['error']}", out, 0.0
    ok, rule = judge(out)
    u = out.get("usage") or {}
    pin, pout = PRICES.get(model, (0, 0))
    cost = (u.get("in", 0) * pin + u.get("out", 0) * pout) / 1e6
    return model_spec, name, ok, rule, out, cost


def main(models):
    if not TOKEN:
        sys.exit("SERVICE_TOKEN is not set")
    jobs = [(m, c) for m in models for c in CASES for _ in range(REPEAT)]
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda j: run(*j), jobs))
    stamp = time.strftime("%Y%m%d-%H%M")
    with open(f"textbench-{stamp}.json", "w", encoding="utf-8") as f:
        json.dump([{"model": m, "case": n, "passed": ok, "rule": rule,
                    "reply": o.get("reply", ""), "tools": o.get("tools"),
                    "seconds": o.get("seconds"), "cost": cost}
                   for m, n, ok, rule, o, cost in results], f,
                  ensure_ascii=False, indent=1)
    print(f"{'model':22} passed  avg secs  cost per 1,000 texts")
    for m in models:
        mine = [r for r in results if r[0] == m]
        passed = sum(1 for r in mine if r[2])
        secs = [r[4].get("seconds") or 0 for r in mine]
        per1000 = sum(r[5] for r in mine) / len(mine) * 1000
        print(f"{m:22} {passed:2}/{len(mine):<3}  {sum(secs) / len(secs):6.1f}"
              f"    ${per1000:.2f}")
    print()
    for m, n, ok, rule, o, _ in results:
        if not ok:
            print(f"FAIL {m:20} {n:28} ({rule}): "
                  f"{(o.get('reply') or o.get('error', ''))[:140]!r}")
    print(f"\nEvery reply is in textbench-{stamp}.json")


if __name__ == "__main__":
    # "x3" runs every case three times: one run of a model is a coin toss
    args = [a for a in sys.argv[1:] if not (a[:1] == "x" and a[1:].isdigit())]
    REPEAT = max([int(a[1:]) for a in sys.argv[1:]
                  if a[:1] == "x" and a[1:].isdigit()] or [1])
    main(args or DEFAULT_MODELS)
