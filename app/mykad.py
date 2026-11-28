"""The front of a Malaysian identity card (MyKad), read by where its values stand.

The card has no labels: the number at the top left, the name below the chip, the address under
the name, citizenship and gender at the lower right. The date and place of birth are worked out
from the number itself, and the number's last digit (odd for a man, even for a woman) is checked
against the printed gender.

Places are measured from the card's own number, not from the picture: a card photographed on a
table, or shown inside a phone's screen, fills only part of it. The name may run on to a second
line ("RAVI A/L" over "MUTHUSAMY"), and with a long address the two blocks nearly touch, so the
space between them does not tell them apart: the address is known by its build (up to three lines,
then the postcode with the town, then the state) and the name's second line by what joins it.

This gives what the card prints, for filling in a form. It does not show that the card is genuine
or that it belongs to the person presenting it: that is identity verification, which this service
does not do."""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

TITLE = re.compile(r"kad\s*pengenalan|mykad|identity\s*card", re.I)
# a line that is the card's heading, not a form's label "No. Kad Pengenalan" (insurance schedules print that)
_HEADING = re.compile(r"\W*(?:kad\s*pengenalan|mykad|(?:malaysia\s*)?identity\s*card)\W*", re.I)
_OTHER = re.compile(r"insur|takaful|polis[iy]|schedule|jadual|premium|lesen|licen[cs]e|sijil|permit", re.I)
MAX_LINES = 40  # a card holds a dozen lines; a page that names an identity card number holds many more
_NUMBER = re.compile(r"(?<!\d)(\d{6})[\s\-]*(\d{2})[\s\-]*(\d{4})(?!\d)")
_GENDERS = {"LELAKI": "male", "PEREMPUAN": "female"}
_CITIZEN = re.compile(r"WARGA\s*NEGARA|PEMASTAUTIN\s*TETAP|BUKAN\s*WARGANEGARA", re.I)
_RELIGION = re.compile(r"ISLAM")
STATES = {"01": "Johor", "02": "Kedah", "03": "Kelantan", "04": "Melaka", "05": "Negeri Sembilan", "06": "Pahang",
          "07": "Pulau Pinang", "08": "Perak", "09": "Perlis", "10": "Selangor", "11": "Terengganu", "12": "Sabah",
          "13": "Sarawak", "14": "Kuala Lumpur", "15": "Labuan", "16": "Putrajaya"}
for _codes, _state in (("21 22 23 24", "Johor"), ("25 26 27", "Kedah"), ("28 29", "Kelantan"), ("30", "Melaka"),
                       ("31 59", "Negeri Sembilan"), ("32 33", "Pahang"), ("34 35", "Pulau Pinang"),
                       ("36 37 38 39", "Perak"), ("40", "Perlis"), ("41 42 43 44", "Selangor"), ("45 46", "Terengganu"),
                       ("47 48 49", "Sabah"), ("50 51 52 53", "Sarawak"), ("54 55 56 57", "Kuala Lumpur"), ("58", "Labuan")):
    for _c in _codes.split():
        STATES[_c] = _state


def _top(line: dict) -> float:
    return min(p[1] for p in line["box"])


def _left(line: dict) -> float:
    return min(p[0] for p in line["box"])


def _right(line: dict) -> float:
    return max(p[0] for p in line["box"])


def _height(line: dict) -> float:
    return max(p[1] for p in line["box"]) - min(p[1] for p in line["box"])


# a name's line that ends on one of these has the rest of the name under it; a line that starts with one carries it on
_NAME_GOES_ON = re.compile(r"(?:^|\s)(?:A\s?[/.]?\s?[LP]|BIN|BINTI|BINTE|BTE|BT|IBNI|IBNU|ANAK|AK|[SD]\s?/\s?O|@)\W*$", re.I)
_NAME_CARRIED_ON = re.compile(r"^\W*(?:A\s?[/.]\s?[LP]|BIN|BINTI|BINTE|BTE|IBNI|IBNU|ANAK|[SD]\s?/\s?O|@)\s+\S", re.I)
# how a line of an address begins, where it holds no number
_ADDRESS_BEGINS = re.compile(
    r"^\W*(?:NO|LOT|PT|PLOT|BLOK|BLOCK|BLK|JALAN|JLN|LORONG|LRG|KAMPUNG|KAMPONG|KG|KPG|TAMAN|TMN|BANDAR|BDR|DESA|FELDA|FELCRA|"
    r"LADANG|RUMAH|RMH|FLAT|PANGSAPURI|APARTMENT|APT|KONDOMINIUM|KONDO|TINGKAT|TKT|UNIT|BATU|SUNGAI|SG|BUKIT|BKT|PERSIARAN|"
    r"LEBUH|LEBUHRAYA|LINTANG|SOLOK|LENGKOK|TANJUNG|TG|TELUK|KUALA|ULU|HULU|PEKAN|MUKIM|SIMPANG|PARIT|PADANG|PANTAI|PULAU|"
    r"SEKSYEN|KEM|KUARTERS|KUATERS|PEJABAT|PETI|WISMA|MENARA|PLAZA|MEDAN|KOMPLEKS|PERUMAHAN|RANCANGAN|RPT|PPR|PPRT|D/A)\b", re.I)
_POSTCODE = re.compile(r"\W*(\d{5})(?!\d)\s*(.*)")


def born(number: str, today: Optional[date] = None) -> Optional[str]:
    """The date of birth in a card number's first six digits; None when they are not a date."""
    today = today or date.today()
    yy, mm, dd = int(number[:2]), int(number[2:4]), int(number[4:6])
    for century in (2000, 1900):
        try:
            d = date(century + yy, mm, dd)
        except ValueError:
            return None
        if d <= today:
            return d.isoformat()
    return None


_ALONE = re.compile(r"\W*\d{6}[\s\-]*\d{2}[\s\-]*\d{4}\W*")


def is_card(lines: list[dict]) -> bool:
    """Whether a page is the front of a MyKad, for naming it: the card's heading, its number
    standing alone on a line, and what only the card prints beside them, the citizenship or the
    gender, or the heading in its second form. A slip that says "ID Type: MyKad" over a twelve-
    digit number (which may be an account number) is not the card."""
    if not lines or len(lines) > 30:
        return False
    texts = [" ".join(ln["text"].split()) for ln in lines]
    if _OTHER.search(" ".join(texts)):
        return False
    heads = {re.sub(r"[^a-z]", "", t.lower()) for t in texts if _HEADING.fullmatch(t)}
    if not heads or not any(_ALONE.fullmatch(t) for t in texts):
        return False
    marks = any(t.upper() in _GENDERS or _CITIZEN.fullmatch(t) for t in texts)
    return marks or len(heads) >= 2


def _is_background(text: str) -> bool:
    """The card's background is printed over with KERAJAAN MALAYSIA in tiny letters: a run of it read as a line."""
    letters = re.sub(r"[^A-Z]", "", text.upper())
    return len(letters) >= 8 and letters in "KERAJAANMALAYSIA" * 4


def _name_lines(column: list[dict], texts: list[str]) -> tuple:
    """(how many of the column's first lines are the name, the place of the postcode's line or None).

    The first line is the name. A following line is the name still when the line above ends on a
    joining word (A/L, BIN, BINTI, ANAK, @) or it starts with one; when it stands more than three
    lines above the postcode, since an address has three lines of its own at most; when it is a
    single word with the address starting under it; or when a clear space parts it from what
    follows. A line that holds a number, or begins as an address does, is the address."""
    post = next((n for n in range(len(texts) - 1, 0, -1) if _POSTCODE.match(texts[n])), None)
    until = len(texts) if post is None else post  # the lines that may be the name or the address's own lines
    gaps = [_top(b) - _top(a) for a, b in zip(column, column[1:])]
    apart = None  # after which line a clearly wider space parts the name from the address
    if len(gaps) > 1:
        usual = sorted(gaps)[len(gaps) // 2]
        widest = max(range(min(len(gaps), 3)), key=lambda n: gaps[n])
        if gaps[widest] > 1.4 * usual and gaps[widest] == max(gaps):
            apart = widest
    cut = 1
    while cut < min(3, until):
        above, line = texts[cut - 1], texts[cut]
        if re.search(r"\d", line):
            break
        if _NAME_GOES_ON.search(above) or _NAME_CARRIED_ON.match(line):
            cut += 1
            continue
        if _ADDRESS_BEGINS.match(line) or (apart is not None and apart < cut) or cut >= until - 1:
            break  # an address's line, or past the space that parts the two, or the last line the address could own
        if (post is not None and cut < post - 3) or (apart is not None and cut <= apart) \
                or (" " not in line.strip() and len(line.strip()) >= 3 and re.search(r"\d", texts[cut + 1])):
            cut += 1
            continue
        break
    return cut, post


def parse(pages: list[dict], today: Optional[date] = None) -> Optional[dict]:
    """The card's fields and checks, or None when the pages are not a MyKad."""
    for page in pages:
        lines = page.get("lines") or []
        whole = " ".join(ln["text"] for ln in lines)
        if not any(_HEADING.fullmatch(ln["text"].strip()) for ln in lines) or len(lines) > MAX_LINES or _OTHER.search(whole):
            continue
        # the card's number: under the heading, alone on its line (a twelve-digit run in a file's name or a
        # clock above the card is not it)
        under = min(_top(ln) for ln in lines if _HEADING.fullmatch(ln["text"].strip()))
        numbered = [ln for ln in sorted(lines, key=_top) if _NUMBER.search(ln["text"])]
        holder = next((ln for ln in numbered if _top(ln) >= under and _ALONE.fullmatch(ln["text"].strip())), None) \
            or next((ln for ln in numbered if _top(ln) >= under), None) or (numbered[0] if numbered else None)
        if holder is None:
            continue
        digits = "".join(_NUMBER.search(holder["text"]).groups())
        f: dict = {"id_number": f"{digits[:6]}-{digits[6:8]}-{digits[8:]}"}
        # places are measured from the number: the name and the address start under its first digit, and
        # the citizenship, religion and gender stand well to the right of it
        start, span = _left(holder), max(_right(holder) - _left(holder), 1.0)
        below = [ln for ln in sorted(lines, key=_top)
                 if _top(ln) > _top(holder) and not TITLE.search(ln["text"]) and not _is_background(ln["text"])]
        right = [" ".join(ln["text"].split()).upper() for ln in below if _left(ln) > start + 1.2 * span]
        # the name and the address: the lines under the number, at the left
        column = [ln for ln in below if abs(_left(ln) - start) <= 0.3 * span and len(ln["text"].strip()) > 2]
        heights = sorted(_height(ln) for ln in lines if ln is not holder)
        if heights and _height(holder) < 0.7 * heights[len(heights) // 2]:
            # the number was taken from the small print under the photo, the large one could not be read: there
            # is then nothing to place the name and the address by, and they are left out rather than misplaced.
            # The citizenship, religion and gender are looked for anywhere on the card.
            column, right = [], [" ".join(ln["text"].split()).upper() for ln in lines]
        for text in right:
            if text in _GENDERS:
                f["gender"] = _GENDERS[text]
            elif _CITIZEN.fullmatch(text):
                f["citizenship"] = text
            elif _RELIGION.fullmatch(text):
                f["religion"] = text
        read = None
        if column:
            texts = [" ".join(ln["text"].split()) for ln in column]
            cut, post = _name_lines(column, texts)
            read = " ".join(texts[:cut])
            f["name"] = re.sub(r"(?<=\w) (A)[ .]?([LP]) (?=\w)", r" \1/\2 ", read)  # A/L, A/P: the stroke is often lost
            # the address ends with its state, the line under the postcode: what stands lower is not the card's
            last = len(texts) if post is None else min(post + 2, len(texts))
            address = [re.sub(r"^NO(?=\d)", "NO ", t) for t in texts[cut:last]][:6]
            if address:
                f["address"] = ", ".join(address)
            if post is not None and post >= cut:
                m = _POSTCODE.match(texts[post])
                f["postcode"], f["city"] = m.group(1), m.group(2).strip(" ,") or None
                if post + 1 < last:
                    f["state"] = texts[post + 1]
        extra = {"name_as_read": read} if read and read != f.get("name") else {}
        birth = born(digits, today)
        f["date_of_birth"] = birth
        f["place_of_birth"] = STATES.get(digits[6:8]) or ("outside Malaysia" if digits[6:8] >= "60" else None)
        checks = [{"name": "number_holds_a_date", "passed": birth is not None,
                   "detail": "the first six digits of the number are a date of birth" if birth
                   else f"the first six digits, {digits[:6]}, are not a date"}]
        if f.get("gender"):
            by_number = "male" if int(digits[-1]) % 2 else "female"
            checks.append({"name": "gender_matches_number", "passed": by_number == f["gender"],
                           "detail": f"the number's last digit says {by_number}, the card prints {f['gender']}"})
        return {"fields": f, "checks": checks, "extra": extra, "page": page.get("page", 1)}
    return None
