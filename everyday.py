"""The things people ask a secretary every day that need no account.

The weather, the Hebrew date, Shabbos and Yom Tov times, zmanim,
yahrzeits - and "what does my day look like", which puts those together
with their own calendar, to-do list and mail.

Before this, every one of these went to a web search and came back as
somebody's blog post read aloud, for the wrong town, or from the model's
memory. Candle lighting is not something to be roughly right about.

Nothing here needs a key. Hebcal (hebcal.com, CC BY 4.0) and Open-Meteo
are free and public; zippopotam.us turns a zip code into a place. Where
the customer is comes from their saved address; a place they name on the
call wins.

Failures come back as a dict with a `reason` code, never as prose to be
read: code decides, the message is for people.
"""
from core import *                                   # noqa: F401,F403
from core import _re_scrub, _tz, _clock


# ------------------------------------------------------------ fetching

_CACHE = {}


def _fetch_json(url: str, keep_s: int = 600) -> dict:
    """One GET, remembered for a while. Candle lighting does not change
    between the first question and the second, and Hebcal is a free
    service run by volunteers - asking it twice a minute is rude."""
    now = time.time()
    hit = _CACHE.get(url)
    if hit and now - hit[0] < keep_s:
        return hit[1]
    req = urllib.request.Request(
        url, headers={"User-Agent": "PhoneAssistant/1.0 (+hellobuziness.com)"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read().decode("utf-8"))
    if len(_CACHE) > 500:
        _CACHE.clear()
    _CACHE[url] = (now, data)
    return data


def _q(**params) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items()
                                   if v not in (None, "")})


# ------------------------------------------------------------ where

US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut",
    "de": "delaware", "fl": "florida", "ga": "georgia", "hi": "hawaii",
    "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine",
    "md": "maryland", "ma": "massachusetts", "mi": "michigan",
    "mn": "minnesota", "ms": "mississippi", "mo": "missouri",
    "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico",
    "ny": "new york", "nc": "north carolina", "nd": "north dakota",
    "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota",
    "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont",
    "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}


def _by_zip(zip5: str) -> dict:
    try:
        d = _fetch_json(f"https://api.zippopotam.us/us/{zip5}", 86400)
    except Exception:
        return {}
    places = d.get("places") or []
    if not places:
        return {}
    p = places[0]
    return {"label": f"{p.get('place name')}, "
                     f"{p.get('state abbreviation')} {zip5}",
            "lat": float(p["latitude"]), "lon": float(p["longitude"]),
            "country": "US", "zip": zip5, "tz": "",
            "hebcal": {"zip": zip5}}


def _hint_fits(result: dict, hint: str) -> bool:
    h = (hint or "").strip().lower().strip(",. ")
    if not h:
        return True
    h = US_STATES.get(h, h)
    return any(h in (result.get(k) or "").lower()
               for k in ("admin1", "country", "admin2", "country_code"))


def _likeliest(results: list) -> dict:
    """Which of several towns with one name the caller means.

    Nearest first: a caller in Brooklyn who says "Lakewood" means New
    Jersey, not the bigger Lakewood in Colorado, so a town in the caller's
    own time zone wins. But not against a famous city: "Jerusalem" put
    that rule to the test and came back as a hamlet in Virginia. A place
    twenty times bigger than the nearby one is the one they mean."""
    def pop(r):
        return r.get("population") or 0

    near = sorted(results, key=lambda r: (r.get("timezone") != LOCAL_TZ,
                                          r.get("country_code") != "US",
                                          -pop(r)))[0]
    biggest = max(results, key=pop)
    if pop(biggest) > 20 * max(pop(near), 1):
        return biggest
    return near


def _by_name(place: str) -> dict:
    """A town by name. "Monsey NY", "Lakewood, New Jersey", "Yerushalayim".

    The geocoder wants the town on its own, so the words are tried as
    town-then-hint: "Monsey NY" is Monsey, in something matching NY. Without
    the hint, "Lakewood" is Lakewood, Colorado."""
    words = place.replace(",", " ").split()
    # What people here call a place, and what a geocoder calls it.
    aliases = {"yerushalayim": "Jerusalem", "yerushalaim": "Jerusalem",
               "boro park": "Borough Park", "bnei brak": "Bnei Brak",
               "bnai brak": "Bnei Brak", "kiryas joel": "Kiryas Joel"}
    for k in range(len(words), 0, -1):
        name = " ".join(words[:k])
        hint = " ".join(words[k:])
        name = aliases.get(name.lower(), name)
        try:
            d = _fetch_json("https://geocoding-api.open-meteo.com/v1/search?"
                            + _q(name=name, count=10, language="en",
                                 format="json"), 86400)
        except Exception:
            return {}
        results = d.get("results") or []
        fits = [r for r in results if _hint_fits(r, hint)]
        if not fits:
            continue
        r = _likeliest(fits)
        region = (r.get("admin1") if r.get("country_code") == "US"
                  else r.get("country"))
        return {"label": r.get("name", place) + (
                    f", {region}" if region and region != r.get("name")
                    else ""),
                "lat": r["latitude"], "lon": r["longitude"],
                "country": r.get("country_code", ""), "zip": "",
                "tz": r.get("timezone", ""),
                "hebcal": {"latitude": round(r["latitude"], 4),
                           "longitude": round(r["longitude"], 4),
                           "tzid": r.get("timezone", "")}}
    return {}


# A geocoder knows towns, not neighbourhoods, and "Williamsburg" on its own
# came back as Williamsburg, Virginia. For the people who ring this number
# it is Brooklyn. Said with a state ("Williamsburg Virginia") it is looked
# up like anywhere else.
NEIGHBOURHOOD_ZIPS = {
    "williamsburg": "11211", "boro park": "11219", "borough park": "11219",
    "crown heights": "11213", "flatbush": "11230", "midwood": "11230",
    "kensington": "11218", "bensonhurst": "11214", "marine park": "11234",
    "far rockaway": "11691", "kew gardens hills": "11367",
    "five towns": "11516", "flatbush brooklyn": "11230",
}


def where(account_id=None, place: str = "") -> dict:
    """The place a question is about. A place said on the call beats the
    saved address; the saved address beats nothing. Empty if neither."""
    place = (place or "").strip()
    if place:
        digits = place.replace(" ", "")
        if _re_scrub.fullmatch(r"\d{5}(-\d{4})?", digits):
            return _by_zip(digits[:5])
        near = NEIGHBOURHOOD_ZIPS.get(
            " ".join(place.lower().replace(",", " ").split()))
        if near:
            got = _by_zip(near)
            if got:
                got["label"] = place.strip().title() + ", Brooklyn" \
                    if near.startswith("112") else place.strip().title()
                return got
        return _by_name(place)
    if not account_id:
        return {}
    db = Session()
    a = (db.query(Address).filter_by(account_id=account_id)
           .order_by(Address.is_default.desc(), Address.id).first())
    db.close()
    if not a:
        return {}
    z = (a.zip or "").strip()[:5]
    if z.isdigit() and len(z) == 5:
        got = _by_zip(z)
        if got:
            return got
    if a.city:
        return _by_name(f"{a.city} {a.state or ''}")
    return {}


NO_PLACE = {"reason": "no_place",
            "message": "No place to go on - no address is saved for them. "
                       "Ask which town or zip code they mean."}


# ------------------------------------------------------------ saying it

def _ordinal(n: int) -> str:
    n = int(n)
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _today():
    return datetime.now(_tz()).date()


def _spoken_day(d, today=None) -> str:
    """today / tomorrow / Friday, September 25."""
    today = today or _today()
    if isinstance(d, str):
        d = datetime.fromisoformat(d[:10]).date()
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}"


def _time_of(iso: str) -> str:
    """'2026-09-25T18:29:00-04:00' -> '6:29 PM', in the place's own time -
    which is what Hebcal already gives, so nothing is converted."""
    try:
        return _clock(datetime.fromisoformat(iso))
    except Exception:
        return ""


WEATHER_WORDS = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy",
    45: "foggy", 48: "foggy with frost", 51: "light drizzle",
    53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle",
    57: "freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow flurries",
    80: "rain showers", 81: "rain showers", 82: "heavy rain showers",
    85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


# ------------------------------------------------------------ weather

def weather(account_id=None, place: str = "", days: int = 1) -> dict:
    """The weather where they are: now, and the next few days."""
    loc = where(account_id, place)
    if not loc:
        return dict(NO_PLACE)
    days = max(1, min(int(days or 1), 7))
    us = loc.get("country") in ("US", "")
    try:
        d = _fetch_json(
            "https://api.open-meteo.com/v1/forecast?" + _q(
                latitude=loc["lat"], longitude=loc["lon"],
                current="temperature_2m,apparent_temperature,weather_code,"
                        "wind_speed_10m",
                daily="temperature_2m_max,temperature_2m_min,"
                      "precipitation_probability_max,weather_code",
                temperature_unit="fahrenheit" if us else "celsius",
                wind_speed_unit="mph" if us else "kmh",
                timezone="auto", forecast_days=days + 1), 900)
    except Exception as e:
        return {"reason": "unavailable",
                "message": f"The weather service did not answer: "
                           f"{str(e)[:100]}"}
    unit = "degrees" if us else "degrees Celsius"
    cur = d.get("current") or {}
    now = ""
    if cur:
        now = (f"{round(cur.get('temperature_2m', 0))} {unit} and "
               f"{WEATHER_WORDS.get(cur.get('weather_code'), 'mixed')}")
        feels = cur.get("apparent_temperature")
        if feels is not None and abs(feels - cur.get("temperature_2m", 0)) >= 5:
            now += f", feels like {round(feels)}"
    daily = d.get("daily") or {}
    today = datetime.fromisoformat(daily["time"][0]).date() \
        if daily.get("time") else _today()
    out = []
    for i, day in enumerate((daily.get("time") or [])[:days + 1]):
        out.append({
            "day": _spoken_day(day, today),
            "high": round(daily["temperature_2m_max"][i]),
            "low": round(daily["temperature_2m_min"][i]),
            "sky": WEATHER_WORDS.get(daily["weather_code"][i], "mixed"),
            "rain_chance": daily["precipitation_probability_max"][i] or 0,
        })
    return {"place": loc["label"], "now": now, "unit": unit, "days": out}


# ------------------------------------------------------------ Jewish dates

HEB_MONTHS = {
    "nisan": "Nisan", "nissan": "Nisan", "iyar": "Iyyar", "iyyar": "Iyyar",
    "sivan": "Sivan", "tamuz": "Tamuz", "tammuz": "Tamuz", "av": "Av",
    "menachem av": "Av", "elul": "Elul", "tishrei": "Tishrei",
    "tishri": "Tishrei", "cheshvan": "Cheshvan", "heshvan": "Cheshvan",
    "marcheshvan": "Cheshvan", "mar cheshvan": "Cheshvan",
    "kislev": "Kislev", "kislef": "Kislev", "tevet": "Tevet",
    "teves": "Tevet", "tevès": "Tevet", "shvat": "Shvat",
    "shevat": "Shvat", "sh'vat": "Shvat", "shvat'": "Shvat",
    "adar": "Adar", "adar 1": "Adar1", "adar i": "Adar1",
    "adar aleph": "Adar1", "adar alef": "Adar1", "adar rishon": "Adar1",
    "adar 2": "Adar2", "adar ii": "Adar2", "adar bet": "Adar2",
    "adar beis": "Adar2", "adar sheni": "Adar2",
}


def hebrew_leap(hy: int) -> bool:
    """Seven leap years in every nineteen; a leap year has two Adars."""
    return (7 * int(hy) + 1) % 19 < 7


def parse_hebrew_date(text: str):
    """"9 Adar", "the 9th of Adar", "Adar 9", "Adar II 14" -> (9, "Adar").
    None if it can't be read - never a guess."""
    t = (text or "").lower().replace(",", " ").replace("the ", " ")
    t = _re_scrub.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", t)
    t = _re_scrub.sub(r"\bof\b", " ", t)
    t = " ".join(t.split())
    # the day is the number that isn't part of "adar 1/2"
    month = None
    for name in sorted(HEB_MONTHS, key=len, reverse=True):
        if _re_scrub.search(r"(?<![a-z])" + _re_scrub.escape(name)
                            + r"(?![a-z])", t):
            month = HEB_MONTHS[name]
            t = _re_scrub.sub(r"(?<![a-z])" + _re_scrub.escape(name)
                              + r"(?![a-z])", " ", t, count=1)
            break
    nums = _re_scrub.findall(r"\b(\d{1,2})\b", t)
    if not month or not nums:
        return None
    day = int(nums[0])
    if not 1 <= day <= 30:
        return None
    return day, month


def hebrew_date(on=None, after_nightfall: bool = False) -> dict:
    """The Hebrew date for a day, and what falls on it."""
    on = on or _today()
    if isinstance(on, str):
        on = datetime.fromisoformat(on[:10]).date()
    d = _fetch_json("https://www.hebcal.com/converter?" + _q(
        cfg="json", gy=on.year, gm=on.month, gd=on.day, g2h=1,
        gs="on" if after_nightfall else ""), 43200)
    # Hebcal lists the week's parsha among a day's events. It isn't one:
    # "today is Parashat Vezot Haberakhah" is not a thing anybody says.
    events = list(d.get("events") or [])
    return {"day": d.get("hd"), "month": d.get("hm"), "year": d.get("hy"),
            "spoken": f"the {_ordinal(d.get('hd', 0))} of {d.get('hm')}, "
                      f"{d.get('hy')}",
            "events": [x for x in events if not x.startswith("Parash")],
            "parsha": next((x.replace("Parashat ", "") for x in events
                            if x.startswith("Parash")), "")}


# ------------------------------------------------------------ their minhag

def minhag_of(account_id) -> dict:
    """What this customer keeps. Empty values mean "they haven't said"."""
    if not account_id:
        return {}
    db = Session()
    row = db.query(Minhag).filter_by(account_id=account_id).first()
    db.close()
    if not row:
        return {}
    return {"candle_minutes": row.candle_minutes or 0,
            "havdalah": row.havdalah or "", "shema": row.shema or ""}


def _read_havdalah(said: str):
    """"Rabbeinu Tam", "72", "the regular time", "50 minutes" -> a value;
    "both" clears it; None if it can't be read."""
    t = (said or "").lower().strip()
    if not t:
        return None
    if _re_scrub.search(r"rabbeinu|rabenu|rabeinu|\brt\b|\br\.?\s?t\b", t) \
            or _re_scrub.fullmatch(r"72( min(ute)?s?)?", t):
        return "rabbeinu_tam"
    if _re_scrub.search(r"both|either|not sure|don.?t know|clear|reset", t):
        return ""
    if _re_scrub.search(r"tzeis|tzeit|regular|usual|standard|normal|8\.5|"
                        r"nightfall|stars", t):
        return "tzeis"
    n = _re_scrub.search(r"\b(\d{2})\b", t)
    if n and 30 <= int(n.group(1)) <= 90:
        return "72" if int(n.group(1)) == 72 else n.group(1)
    return None


def _read_shema(said: str):
    t = (said or "").lower().strip()
    if not t:
        return None
    if _re_scrub.search(r"magen|mga|m\.g\.a|avraham", t):
        return "mga"
    if _re_scrub.search(r"tanya|chabad|alter rebbe|lubavitch", t):
        return "tanya"
    if _re_scrub.search(r"\bgra\b|g\.r\.a|gaon|vilna", t):
        return "gra"
    if _re_scrub.search(r"both|either|clear|reset|don.?t know", t):
        return ""
    return None


def set_minhag(account_id: int, candle_minutes: int = 0, havdalah: str = "",
               shema: str = "", call_id=None) -> dict:
    """Remember what they keep. Only what was actually said changes;
    everything else stays as it was."""
    changes, problems = [], []
    db = Session()
    row = db.query(Minhag).filter_by(account_id=account_id).first()
    if not row:
        row = Minhag(account_id=account_id)
        db.add(row)
    if candle_minutes:
        if 15 <= int(candle_minutes) <= 60:
            row.candle_minutes = int(candle_minutes)
            changes.append(f"lights candles {int(candle_minutes)} minutes "
                           f"before sunset")
        else:
            problems.append(f"{candle_minutes} minutes before sunset isn't "
                            f"a time anyone keeps - ask again")
    if havdalah:
        h = _read_havdalah(havdalah)
        if h is None:
            problems.append(f"couldn't tell which havdalah {havdalah!r} "
                            f"means - ask whether they keep Rabbeinu Tam")
        else:
            row.havdalah = "rabbeinu_tam" if h == "72" else h
            changes.append({"rabbeinu_tam": "keeps Rabbeinu Tam for "
                                            "havdalah (72 minutes)",
                            "tzeis": "makes havdalah at nightfall, not "
                                     "Rabbeinu Tam",
                            "": "hears both havdalah times"}.get(
                row.havdalah, f"makes havdalah {row.havdalah} minutes "
                              f"after sunset"))
    if shema:
        s = _read_shema(shema)
        if s is None:
            problems.append(f"couldn't tell whether {shema!r} means the "
                            f"Magen Avraham, the Gra or the Baal HaTanya")
        else:
            row.shema = s
            changes.append({"mga": "follows the Magen Avraham for Shema "
                                   "and Shacharis",
                            "gra": "follows the Gra for Shema and "
                                   "Shacharis",
                            "tanya": "follows the Baal HaTanya for Shema "
                                     "and Shacharis",
                            "": "hears both opinions for Shema"}[s])
    row.updated = datetime.utcnow()
    db.commit()
    db.close()
    if changes:
        record_change(account_id, "minhag", "minhag saved",
                      "; ".join(changes), call_id=call_id)
    return {"saved": changes, "problems": problems,
            "now": minhag_of(account_id)}


def candle_minutes(loc: dict, minhag: dict = None) -> int:
    """How long before sunset candles are lit. Forty in Jerusalem, which
    is the custom of the place and everyone there keeps it; otherwise what
    this customer told us, or the usual eighteen."""
    if loc.get("country") == "IL" and \
            (loc.get("label") or "").lower().startswith("jerusalem"):
        return 40
    return int((minhag or {}).get("candle_minutes") or 18)


def _havdalah_params(minhag: dict) -> dict:
    """What Hebcal is asked for. Rabbeinu Tam is a fixed 72 minutes after
    sunset, which Hebcal takes as m=72 - and then the second night of Yom
    Tov is lit at that time too, which is what someone who keeps it
    does."""
    h = (minhag or {}).get("havdalah") or ""
    if h == "rabbeinu_tam":
        return {"m": 72}
    if h.isdigit():
        return {"m": int(h)}
    return {"M": "on"}


def _havdalah_label(minhag: dict) -> str:
    h = (minhag or {}).get("havdalah") or ""
    if h == "rabbeinu_tam":
        return "Havdalah (Rabbeinu Tam, 72 minutes)"
    if h.isdigit():
        return f"Havdalah ({h} minutes)"
    return "Havdalah"


def _loc_params(loc: dict, israel: bool = False) -> dict:
    p = dict(loc.get("hebcal") or {})
    if israel or loc.get("country") == "IL":
        p["i"] = "on"
    return p


ZMANIM_SPOKEN = [
    ("alotHaShachar", "Alos hashachar (dawn)", ""),
    ("misheyakir", "Misheyakir (earliest tallis and tefillin)", ""),
    ("sunrise", "Netz (sunrise)", ""),
    ("sofZmanShmaMGA", "Latest Shema, Magen Avraham", "mga"),
    ("sofZmanShma", "Latest Shema, Gra", "gra"),
    ("sofZmanTfillaMGA", "Latest Shacharis, Magen Avraham", "mga"),
    ("sofZmanTfilla", "Latest Shacharis, Gra", "gra"),
    ("sofZmanShmaBaalHatanya", "Latest Shema, Baal HaTanya", "tanya"),
    ("sofZmanTfilaBaalHatanya", "Latest Shacharis, Baal HaTanya", "tanya"),
    ("chatzot", "Chatzos (midday)", ""),
    ("minchaGedola", "Mincha gedola (earliest Mincha)", ""),
    ("minchaKetana", "Mincha ketana", ""),
    ("plagHaMincha", "Plag hamincha", ""),
    ("sunset", "Shkiah (sunset)", ""),
    ("tzeit85deg", "Tzeis hakochavim (nightfall)", ""),
    ("tzeit72min", "Rabbeinu Tam (72 minutes)", ""),
]


def zmanim(account_id=None, place: str = "", on=None) -> dict:
    loc = where(account_id, place)
    if not loc:
        return dict(NO_PLACE)
    m = minhag_of(account_id)
    shita = m.get("shema") or ""
    on = on or _today()
    if isinstance(on, str):
        on = datetime.fromisoformat(on[:10]).date()
    d = _fetch_json("https://www.hebcal.com/zmanim?" + _q(
        cfg="json", date=on.isoformat(), **_loc_params(loc)), 43200)
    times = d.get("times") or {}
    if shita:
        note = ("By their minhag: Shema and Shacharis by the "
                + {"mga": "Magen Avraham", "gra": "Gra",
                   "tanya": "Baal HaTanya"}.get(shita, shita)
                + ". Only that opinion is listed.")
    else:
        note = ("Shema and Shacharis are given by both the Magen Avraham "
                "and the Gra. Read both unless they say which they keep - "
                "and if they do, call remember_minhag.")
    return {"place": loc["label"], "day": _spoken_day(on),
            "times": [{"name": label, "time": _time_of(times[key])}
                      for key, label, who in ZMANIM_SPOKEN
                      if times.get(key) and who in (
                          ("", shita) if shita else ("", "mga", "gra"))],
            "minhag": m, "note": note}


def shabbos(account_id=None, place: str = "", on=None) -> dict:
    """Candle lighting, havdalah and the parsha for the coming Shabbos,
    and any Yom Tov that falls with it - by their minhag if we know it."""
    loc = where(account_id, place)
    if not loc:
        return dict(NO_PLACE)
    m = minhag_of(account_id)
    on = on or _today()
    if isinstance(on, str):
        on = datetime.fromisoformat(on[:10]).date()
    mins = candle_minutes(loc, m)
    d = _fetch_json("https://www.hebcal.com/shabbat?" + _q(
        cfg="json", b=mins, gy=on.year, gm=on.month, gd=on.day,
        **_havdalah_params(m), **_loc_params(loc)), 21600)
    today = _today()
    items = []
    last_havdalah = ""
    for it in d.get("items") or []:
        cat = it.get("category", "")
        when = it.get("date", "")
        if cat in ("candles", "havdalah"):
            items.append({"what": "Candle lighting" if cat == "candles"
                          else _havdalah_label(m),
                          "day": _spoken_day(when, today),
                          "time": _time_of(when)})
            if cat == "havdalah":
                last_havdalah = when[:10]
        elif cat in ("holiday", "parashat", "roshchodesh"):
            items.append({"what": it.get("title", ""),
                          "day": _spoken_day(when, today), "time": ""})
    # Nobody has said what they keep: give Rabbeinu Tam alongside the
    # ordinary nightfall, because many here wait 72 minutes. Once they
    # have said, give theirs and only theirs.
    rt = ""
    if last_havdalah and not m.get("havdalah"):
        try:
            z = _fetch_json("https://www.hebcal.com/zmanim?" + _q(
                cfg="json", date=last_havdalah, **_loc_params(loc)), 43200)
            rt = _time_of((z.get("times") or {}).get("tzeit72min", ""))
        except Exception:
            rt = ""
    told = bool(m.get("havdalah") or m.get("candle_minutes"))
    return {"place": loc["label"], "items": items,
            "rabbeinu_tam": rt, "minhag": m,
            "note": (f"These are by their minhag: candles {mins} minutes "
                     f"before sunset" + ({"rabbeinu_tam": ", havdalah by "
                                          "Rabbeinu Tam",
                                          "tzeis": ", havdalah at "
                                          "nightfall"}.get(
                         m.get("havdalah") or "",
                         f", havdalah {m.get('havdalah')} minutes after "
                         f"sunset" if m.get("havdalah") else ""))
                     + ". Don't offer other opinions unless they ask.")
            if told else
            (f"Candle lighting is {mins} minutes before sunset. If they say "
             f"what they keep - Rabbeinu Tam, a different number of "
             f"minutes - call remember_minhag so it is theirs from now on.")}


def holidays(account_id=None, place: str = "", on=None,
             days: int = 60) -> dict:
    """What's coming: Yomim Tovim, fasts, Rosh Chodesh, special Shabbosim,
    with candle lighting and fast times where they are."""
    loc = where(account_id, place)
    on = on or _today()
    if isinstance(on, str):
        on = datetime.fromisoformat(on[:10]).date()
    end = on + timedelta(days=max(7, min(int(days or 60), 400)))
    params = dict(cfg="json", v=1, maj="on", min="on", mod="on", nx="on",
                  ss="on", mf="on", start=on.isoformat(),
                  end=end.isoformat())
    if loc:
        m = minhag_of(account_id)
        params.update(c="on", b=candle_minutes(loc, m),
                      **_havdalah_params(m), **_loc_params(loc))
    d = _fetch_json("https://www.hebcal.com/hebcal?" + _q(**params), 21600)
    today = _today()
    out = []
    for it in d.get("items") or []:
        cat = it.get("category", "")
        if cat not in ("holiday", "roshchodesh", "candles", "havdalah",
                       "zmanim"):
            continue
        title = it.get("title", "")
        when = it.get("date", "")
        timed = cat in ("candles", "havdalah", "zmanim") and "T" in when
        out.append({"what": title.split(":")[0] if timed else title,
                    "day": _spoken_day(when, today),
                    "time": _time_of(when) if timed else ""})
        if len(out) >= 24:
            break
    return {"place": loc.get("label", "") if loc else "",
            "items": out,
            "note": "" if loc else "No place known, so no candle lighting "
                                   "or fast times - dates only."}


def jewish_calendar(account_id=None, what: str = "", place: str = "",
                    on: str = "") -> dict:
    """One door for "when is candle lighting", "what are the zmanim",
    "what's the Hebrew date" and "when is Chanukah"."""
    what = (what or "").lower().strip()
    try:
        if what in ("zmanim", "times", "zman"):
            return zmanim(account_id, place, on or None)
        if what in ("holidays", "yom tov", "yomim tovim", "coming",
                    "fasts", "fast"):
            return holidays(account_id, place, on or None)
        if what in ("hebrew_date", "date", "hebrew date", "today"):
            day = on or None
            now = hebrew_date(day)
            tonight = hebrew_date(day, after_nightfall=True)
            return {"today": now, "tonight": tonight,
                    "note": "The Hebrew date changes at nightfall. After "
                            "dark it is " + tonight["spoken"] + "."}
        return shabbos(account_id, place, on or None)
    except Exception as e:
        return {"reason": "unavailable",
                "message": f"The Jewish calendar service did not answer: "
                           f"{str(e)[:100]}"}


def yahrzeit(died_on: str = "", after_sunset: bool = False,
             hebrew: str = "", years: int = 2) -> dict:
    """When a yahrzeit falls in the coming years.

    Either the English date they passed away (and whether it was after
    sunset, which moves it a whole Hebrew day), or the Hebrew date if the
    family already knows it. The candle is lit the evening BEFORE the date
    given - a date read out on its own sends people a day late."""
    years = max(1, min(int(years or 2), 5))
    today = _today()
    day = month = None
    if hebrew.strip():
        got = parse_hebrew_date(hebrew)
        if not got:
            return {"reason": "unclear_date",
                    "message": f"Could not read {hebrew!r} as a Hebrew date. "
                               f"Ask for the day and the month, like "
                               f"'the ninth of Adar'."}
        day, month = got
    elif died_on.strip():
        try:
            passed = datetime.fromisoformat(died_on.strip()[:10]).date()
        except ValueError:
            return {"reason": "unclear_date",
                    "message": "The date of passing needs a year, month and "
                               "day."}
        h = _fetch_json("https://www.hebcal.com/converter?" + _q(
            cfg="json", gy=passed.year, gm=passed.month, gd=passed.day,
            g2h=1, gs="on" if after_sunset else ""), 86400)
        day, month = int(h["hd"]), h["hm"].replace(" ", "")
        month = {"AdarI": "Adar1", "AdarII": "Adar2"}.get(month, month)
    else:
        return {"reason": "unclear_date",
                "message": "Ask for the date they passed away, or the Hebrew "
                           "date of the yahrzeit."}

    def g_date(hy, hm):
        g = _fetch_json("https://www.hebcal.com/converter?" + _q(
            cfg="json", hy=hy, hm=hm, hd=day, h2g=1), 86400)
        return datetime(int(g["gy"]), int(g["gm"]), int(g["gd"])).date()

    def spoken_month(hm):
        return {"Adar1": "Adar I", "Adar2": "Adar II"}.get(hm, hm)

    now_hy = int(hebrew_date(today)["year"])
    found = []
    for hy in range(now_hy, now_hy + years + 2):
        use = month
        if month in ("Adar1", "Adar2") and not hebrew_leap(hy):
            use = "Adar"
        try:
            on = g_date(hy, "Adar1" if month == "Adar"
                        and hebrew_leap(hy) else use)
        except Exception:
            continue
        if on < today:
            continue
        entry = {"hebrew": f"the {_ordinal(day)} of {spoken_month(use)}, {hy}",
                 "day": _spoken_day(on, today), "date": on.isoformat(),
                 "candle_evening": _spoken_day(on - timedelta(days=1), today),
                 "note": ""}
        # Someone who passed in Adar of an ordinary year, in a year with
        # two Adars: the minhag is not one thing, so neither is the date.
        if month == "Adar" and hebrew_leap(hy):
            try:
                second = g_date(hy, "Adar2")
            except Exception:
                second = None
            entry["hebrew"] = f"the {_ordinal(day)} of Adar I, {hy}"
            if second:
                entry["or_in_adar_ii"] = {
                    "day": _spoken_day(second, today),
                    "date": second.isoformat(),
                    "candle_evening": _spoken_day(second - timedelta(days=1),
                                                  today)}
            entry["note"] = ("This year has two Adars. Customs differ: some "
                             "keep the yahrzeit in Adar I, some in Adar II, "
                             "and some keep both. Give both dates and say "
                             "they should ask their rav which to keep.")
        found.append(entry)
        if len(found) >= years:
            break
    if not found:
        return {"reason": "unavailable",
                "message": "The Jewish calendar service did not answer."}
    return {"hebrew_date": f"the {_ordinal(day)} of {spoken_month(month)}",
            "coming": found,
            "note": "The yahrzeit begins the evening before, at nightfall - "
                    "that is when the candle is lit. Always say the evening "
                    "as well as the day."}


# ------------------------------------------------------------ the day

def my_day(account_id: int, which: str = "") -> dict:
    """"What does my day look like?" answered in one go.

    Each part is fetched on its own and a part that fails is named in
    `missing` rather than failing the whole answer: an expired Google
    connection should not stop them hearing the weather and candle
    lighting."""
    from google_tools import (tool_list_events, tool_tasks_list,
                              tool_unread_summary)
    today = _today()
    out = {"date": f"{today.strftime('%A')}, {today.strftime('%B')} "
                   f"{today.day}",
           "missing": []}

    try:
        h = hebrew_date(today)
        out["hebrew_date"] = h["spoken"]
        if h["events"]:
            out["today_is"] = h["events"]
    except Exception:
        out["missing"].append("the Hebrew date")

    try:
        w = weather(account_id, "", 1)
        if w.get("reason"):
            out["missing"].append("the weather - no address saved")
        else:
            first = (w.get("days") or [{}])[0]
            out["weather"] = {"now": w.get("now"), "high": first.get("high"),
                              "low": first.get("low"),
                              "sky": first.get("sky"),
                              "rain_chance": first.get("rain_chance")}
    except Exception:
        out["missing"].append("the weather")

    # Shabbos or Yom Tov today: candle lighting is the one time of day
    # that must not be missed.
    try:
        s = shabbos(account_id, "", today)
        todays = [i for i in s.get("items") or []
                  if i.get("day") == "today" and i.get("time")]
        if todays:
            out["tonight"] = todays
    except Exception:
        pass

    try:
        ev = tool_list_events(account_id, 1, which)
        tz = _tz()
        mine = []
        for e in ev.get("events") or []:
            start = e.get("start") or ""
            if e.get("all_day"):
                if start[:10] == today.isoformat():
                    mine.append({"title": e["title"], "time": "all day"})
                continue
            t = datetime.fromisoformat(start.replace("Z", "+00:00"))
            t = t.astimezone(tz)
            if t.date() == today:
                mine.append({"title": e["title"], "time": _clock(t),
                             "where": e.get("location", "")})
        out["appointments"] = mine
    except Exception as e:
        out["missing"].append("their calendar" + (
            " - the Google connection has expired"
            if "expired" in str(e).lower() or "invalid_grant" in str(e)
            else ""))

    try:
        t = tool_tasks_list(account_id, which)
        due = [x for x in t.get("tasks") or []
               if x.get("due") and x["due"] <= today.isoformat()]
        out["to_do"] = {"due_or_overdue": [
            {"title": x["title"], "overdue": x["due"] < today.isoformat()}
            for x in due[:6]],
            "others": max(0, (t.get("count") or 0) - len(due))}
    except Exception:
        out["missing"].append("their to-do list")

    try:
        u = tool_unread_summary(account_id, 3, which, primary_only=True)
        out["mail"] = {"unread": u.get("unread_count", 0),
                       "newest_from": [m.get("from", "")
                                       for m in (u.get("messages") or [])[:3]]}
    except Exception:
        out["missing"].append("their email")
    return out
