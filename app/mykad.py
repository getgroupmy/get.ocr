"""The front of a Malaysian identity card (MyKad), read by where its values stand.

The card has no labels. Two designs are in use. On the one issued until 2026 the number stands at
the top left, the name below the chip, the address under the name, and the citizenship, religion
and gender at the lower right. On the new-generation card (from September 2026) the photo is at
the left, the number, the name and the address stand right of it under one another, and the
citizenship, religion and gender are under the photo, beside the address. On both, the name and
the address start under the number's first digit: that is what the reader goes by. The date and
place of birth are worked out from the number itself, and the number's last digit (odd for a man,
even for a woman) is checked against the printed gender.

Places are measured from the card's own number, not from the picture: a card photographed on a
table, or shown inside a phone's screen, fills only part of it. The name may run on to a second
line ("RAVI A/L" over "MUTHUSAMY"), and with a long address the two blocks nearly touch, so the
space between them does not tell them apart: the address is known by its build (up to three lines,
then the postcode with the town, then the state) and the name's second line by what joins it.

This gives what the card prints, for filling in a form. It does not show that the card is genuine
or that it belongs to the person presenting it: that is identity verification, which this service
does not do."""
from __future__ import annotations

import difflib
import re
import unicodedata
from datetime import date
from typing import Optional

from . import headings

TITLE = re.compile(r"kad\s*pengenalan|mykad|identity\s*card", re.I)
# a line that is the card's heading, not a form's label "No. Kad Pengenalan" (insurance schedules print that)
_HEADING = re.compile(r"\W*(?:kad\s*pengenalan|mykad|(?:malaysia\s*)?identity\s*card)\W*", re.I)
TITLES = ("kadpengenalan", "identitycard", "malaysiaidentitycard", "mykad")


def _is_heading(text: str) -> bool:
    """A line that is the card's heading: whole, or with one letter lost or misread under the hologram that
    lies over it ("IENTITYCARD"). A part of it is no heading: "Pengenalan" alone is a chapter's title
    ("1. PENGENALAN", an introduction) or the second line of a column headed "No. Kad / Pengenalan"."""
    text = text.strip()
    if _HEADING.fullmatch(text):
        return True
    letters = headings.key(text)
    return not headings.is_label(text) and sum(ch.isdigit() for ch in text) <= 1 and any(
        abs(len(letters) - len(title)) <= 1 and difflib.SequenceMatcher(None, letters, title).ratio() >= 0.9
        for title in TITLES if len(title) >= 10)


# another paper that names an identity card, and the child's card, which has the same build and is not this card
_OTHER = re.compile(r"insur|takaful|polis[iy]|schedule|jadual|premium|lesen|licen[cs]e|sijil|permit|\bmykid\b", re.I)
# a label of a form about a person, alone on its line. The card prints no label at all: a page that holds two is a
# form, a slip or an app's page that lists what a card says ("Nama", "Jantina", "No. Telefon")
_LABEL_LINE = re.compile(r"\W*(?:nama(?:\s+penuh)?|(?:full\s+)?name|alamat|address|jantina|gender|sex|tarikh(?:\s+lahir)?|"
                         r"date\s+of\s+birth|no\.?\s*(?:telefon|tel|id|k\.?/?p\.?|ic)|telefon|phone|e-?mel|e-?mail|jenis\s+id|"
                         r"id\s+type|umur|age|bangsa|race|agama|religion|nationality|kewarganegaraan|jawatan|pekerjaan|"
                         r"occupation)\W*", re.I)


def _is_a_form(texts: list) -> bool:
    return sum(1 for t in texts if _LABEL_LINE.fullmatch(t)) >= 2
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


def _row(line: dict) -> float:
    """How far down a line stands, taken at its left end, where the column's lines start: the highest corner of
    a line that slopes is its far end, which lies the higher the longer the line is."""
    (_, y0), (_, y1) = sorted(line["box"])[:2]
    return (y0 + y1) / 2


# a name's line that ends on one of these has the rest of the name under it; a line that starts with one carries it on
_NAME_GOES_ON = re.compile(r"(?:^|\s)(?:A\s?[/.]?\s?[LP]|BIN|BINTI|BINTE|BTE|BT|IBNI|IBNU|ANAK|AK|[SD]\s?/\s?O|@)\W*$"
                           r"|[A-Z']{4,}BINTI\W*$|[A-Z']{5,}BIN\W*$", re.I)  # or BIN, BINTI run into the word before it
_NAME_CARRIED_ON = re.compile(r"^\W*(?:A\s?[/.]\s?[LP]|BIN|BINTI|BINTE|BTE|IBNI|IBNU|ANAK|[SD]\s?/\s?O|@)\s+\S", re.I)
# how a line of an address begins, where it holds no number
_ADDRESS_BEGINS = re.compile(
    r"^\W*(?:NO|LOT|PT|PLOT|BLOK|BLOCK|BLK|JALAN|JLN|LORONG|LRG|KAMPUNG|KAMPONG|KG|KPG|TAMAN|TMN|BANDAR|BDR|DESA|FELDA|FELCRA|"
    r"LADANG|RUMAH|RMH|FLAT|PANGSAPURI|APARTMENT|APT|KONDOMINIUM|KONDO|TINGKAT|TKT|UNIT|BATU|SUNGAI|SG|BUKIT|BKT|PERSIARAN|"
    r"LEBUH|LEBUHRAYA|LINTANG|SOLOK|LENGKOK|TANJUNG|TG|TELUK|KUALA|ULU|HULU|PEKAN|MUKIM|SIMPANG|PARIT|PADANG|PANTAI|PULAU|"
    r"SEKSYEN|KEM|KUARTERS|KUATERS|PEJABAT|PETI|WISMA|MENARA|PLAZA|MEDAN|KOMPLEKS|PERUMAHAN|RANCANGAN|RPT|PPR|PPRT|D/A)\b", re.I)
_POSTCODE = re.compile(r"\W*(\d{5})(?!\d)\s*(.*)")
# what stands beside the address: the citizenship, a letter for a native of Sabah (H) or Sarawak (K), the religion, the gender
_BESIDE = re.compile(r"\s*(?:(BUKAN\s*WARGA\s*NEGARA|WARGA\s*NEGARA|PEMASTAUTIN\s*TETAP|ISLAM|LELAKI|PEREMPUAN)(?P<glued>(?=[A-Z]))?"
                     r"|([HK])(?![A-Z]))", re.I)
_TERRITORY = re.compile(r"W\.?\s?P\b|WILAYAH\s*PERSEKUTUAN", re.I)


def _beside(text: str, glued: bool = False) -> tuple:
    """(the words of that block a line starts with, what follows them). On the new card they stand so
    close to the address that a reader gives both as one line: "ISLAM62551 W.P PUTRAJAYA". `glued`:
    a word may run straight into letters ("ISLAMSELANGOR"), for a line that starts outside the column."""
    words, at = [], 0
    while True:
        m = _BESIDE.match(text, at)
        if not m or (m.group("glued") is not None and not glued):
            return words, text[at:].strip()
        words.append(" ".join((m.group(1) or m.group(3)).upper().split()))
        at = m.end()


def born(number: str, today: Optional[date] = None) -> Optional[str]:
    """The date of birth in a card number's first six digits; None when they are not a date. The
    card is held from the age of twelve, which says which century two digits of a year mean:
    "250101" on a card read in 2026 is 1925, not a child of one."""
    today = today or date.today()
    latest = date(today.year - 12, today.month, min(today.day, 28))
    yy, mm, dd = int(number[:2]), int(number[2:4]), int(number[4:6])
    for century in (2000, 1900):
        try:
            d = date(century + yy, mm, dd)
        except ValueError:
            continue
        if d <= latest:
            return d.isoformat()
    return None


# the number's seventh and eighth digits name the place of birth: a state (STATES), or one of these for a country
# or a part of the world. 82 is an unknown state, 98 a stateless holder, 99 no information: none of them says where.
ABROAD = {"%02d" % n for n in [*range(60, 69), 71, 72, *range(74, 80), *range(83, 94)]}


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
    heads = {re.sub(r"[^a-z]", "", t.lower()) for t in texts if _is_heading(t)}
    if not heads or not any(_ALONE.fullmatch(t) for t in texts) or (_is_a_form(texts) and len(heads) < 2):
        return False  # (a card lying on a form, or shown in an app's screen, has labels around it and its own two headings)
    marks = any(t.upper() in _GENDERS or _CITIZEN.fullmatch(t) for t in texts)
    # and the reader itself must read it one way up or the other: what it refuses, whatever its labels say, is no card
    return (marks or len(heads) >= 2) and any(parse([{"page": 1, "lines": way}]) is not None for way in (
        lines, [{**ln, "box": [[-x, -y] for x, y in ln["box"]]} for ln in lines]))


def _misread(text: str) -> Optional[str]:
    """A word of the block beside the address read with one letter wrong or lost: "ISAM", "LELAK1"."""
    text = " ".join(text.upper().split())
    for word in ("WARGANEGARA", "BUKAN WARGANEGARA", "PEMASTAUTIN TETAP", "PEREMPUAN", "LELAKI", "ISLAM"):
        if abs(len(text) - len(word)) <= 1 and difflib.SequenceMatcher(None, text, word).ratio() >= 0.8:
            return word
    return None


# Where each postcode is used, by its first two digits. A town on a border may use its neighbour's: both are named.
POSTCODES = {}
for _range, _states in (
        ("01 02", "Perlis"), ("05 06 07 08 09", "Kedah"), ("10 11 12 13", "Pulau Pinang"), ("14", "Pulau Pinang, Kedah"),
        ("15 16 17 18", "Kelantan"), ("20 21 22 23 24", "Terengganu"), ("25 26 27 28 39 49 69", "Pahang"),
        ("30 31 32 33 35 36", "Perak"), ("34", "Perak, Kedah"), ("40 41 42 43 44 45 46 47 48 63 64", "Selangor"),
        ("50 51 52 53 54 55 56 57 58 59 60 68", "Kuala Lumpur, Selangor"), ("62", "Putrajaya"),
        ("70 71 72 73", "Negeri Sembilan"), ("75 76 77 78", "Melaka"), ("79 80 81 82 83 84 85 86", "Johor"),
        ("87", "Labuan"), ("88 89 90 91", "Sabah"), ("93 94 95 96 97 98", "Sarawak")):
    for _two in _range.split():
        POSTCODES[_two] = tuple(_states.split(", "))
# A town on a state's border that uses the postcode of the town across it: Pulau Sebang in Melaka (Tampin's), Gemas
# Baharu in Johor (Gemas's), Hulu Bernam in Selangor (Tanjong Malim's); and Pulau Tioman, an island of Pahang whose
# post goes through Mersing in Johor. Held against a public list of 2,933 postcodes, the table above allows every
# one of them in its own state.
ALSO_USED_IN = {"73000": ("Melaka",), "73400": ("Johor",), "35900": ("Selangor",), "86800": ("Pahang",)}
_STATE_WORDS = (("JOHOR", "Johor"), ("KEDAH", "Kedah"), ("KELANTAN", "Kelantan"), ("MELAKA", "Melaka"), ("SEMBILAN", "Negeri Sembilan"),
                ("PAHANG", "Pahang"), ("PINANG", "Pulau Pinang"), ("PERAK", "Perak"), ("PERLIS", "Perlis"), ("SELANGOR", "Selangor"),
                ("TERENGGANU", "Terengganu"), ("TRENGGANU", "Terengganu"), ("SABAH", "Sabah"), ("SARAWAK", "Sarawak"),
                ("LABUAN", "Labuan"), ("PUTRAJAYA", "Putrajaya"), ("KUALA LUMPUR", "Kuala Lumpur"), ("(KL", "Kuala Lumpur"))


def _state_named(text: Optional[str]) -> Optional[str]:
    """The state a card's last address line names ("W. PERSEKUTUAN(KL)", "N. SEMBILAN"); None when it names none."""
    upper = " ".join((text or "").upper().split())
    return next((state for word, state in _STATE_WORDS if word in upper), None)


def _reads_as_a_card(lines: list[dict]) -> bool:
    """A card whose heading could not be read at all, for a caller who says it is one: its number alone on a
    line, and the gender or the citizenship standing alone as the card prints them."""
    texts = [" ".join(ln["text"].split()).upper() for ln in lines]
    return any(_ALONE.fullmatch(t) for t in texts) and any(t in _GENDERS or _CITIZEN.fullmatch(t) for t in texts)


def _plain(text: str) -> str:
    """The card prints plain capitals: an accent over a letter is the reader's own ("GUNSÁLAM")."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _tidy_name(read: str, muslim: bool) -> str:
    """A name as the card prints it, from what a reader made of it: the spaces around "@", the space after
    "A/L", the stroke of A/L or A/P where it was lost, BIN or BINTI where it ran into the word before it."""
    name = " ".join(re.sub(r"\s*@\s*", " @ ", _plain(read)).split())
    name = re.sub(r"\b(A/[LP])(?=[A-Z])", r"\1 ", name)  # "A/LMUTHUSAMY"
    if not muslim and not re.search(r"\b(?:BIN|BINTI|BINTE|BTE|BT)\b", name):
        # "RAVI AL MUTHU", "RAVI A L MUTHU", "RAVI AIL MUTHU": the stroke lost or read as a letter. Only on a card
        # without ISLAM and a name without BIN or BINTI: AL is also a word of Arabic names ("MOHAMAD AL AMIN BIN
        # ISMAIL", "SYED AHMAD AL ATTAS"), and those cards have one or the other.
        name = re.sub(r"(?<=\w) A[ .I1l|]?([LP]) (?=\w)", r" A/\1 ", name)
    if not re.search(r"\b(?:BIN|BINTI|BINTE|BTE|BT|IBNI|IBNU|ANAK|AK)\b|[SD]/O|A/[LP]", name):
        # "SITI NORHALIZABINTI ZULKIFLI", "AHMAD ZULKIFLIBIN ABDULLAH": the space before BINTI or BIN lost, in a name
        # that has no such word of its own
        name = re.sub(r"\b([A-Z']{4,})(BINTI)(?= [A-Z])", r"\1 \2", name)
        name = re.sub(r"\b([A-Z']{5,})(BIN)(?= [A-Z])", r"\1 \2", name)
    return name


def _is_background(text: str) -> bool:
    """The card's background is printed over with KERAJAAN MALAYSIA in tiny letters: a run of it read as a
    line ("AKERAJAANMALAY", "NMALA"), or with a letter wrong ("AKERAJAAI")."""
    letters = re.sub(r"[^A-Z]", "", text.upper())
    print_ = "KERAJAANMALAYSIA" * 4
    if len(letters) >= 5 and letters in print_:
        return True
    return len(letters) >= 8 and any(difflib.SequenceMatcher(None, letters, print_[at:at + len(letters)]).ratio() >= 0.85
                                     for at in range(16))


def _pitch(column: list[dict], n: int) -> float:
    """How far the address's lines stand apart at line n: its distance from the line above, held to twice its own
    height (the line above may be the name, with the space that parts the two blocks between them)."""
    return min(_row(column[n]) - _row(column[n - 1]), 2.0 * max(_height(column[n]), 1.0)) if n else 2.0 * max(_height(column[n]), 1.0)


def _address_end(column: list[dict], texts: list[str]) -> tuple:
    """(the place of the postcode's line or None, where the card's lines end).

    The postcode's line starts with five digits and has under it the state at most, and then the card
    ends: no line, or one that stands clearly further down than the address's lines do from one another
    (a phone's "Shot on ...", the card's back copied under its front, a note under a photocopy). A house
    number of five digits has the address's other lines under it; a line under the card that starts with
    five digits comes after the card has ended."""
    found = [n for n in range(len(texts)) if _POSTCODE.match(texts[n])]
    for post in found:
        pitch, follow = _pitch(column, post), 0
        while post + follow + 1 < len(column) and \
                _row(column[post + follow + 1]) - _row(column[post + follow]) <= 1.6 * pitch:
            follow += 1
        if follow <= 1:
            return post, post + 1 + follow
    if found:
        return found[-1], min(found[-1] + 2, len(texts))
    return None, len(texts)


def _name_lines(column: list[dict], texts: list[str], post: Optional[int]) -> int:
    """How many of the column's first lines are the name; none when the name's line was not read.

    The first line is the name. A following line is the name still when the line above ends on a
    joining word (A/L, BIN, BINTI, ANAK, @) or it starts with one; when it stands more than three
    lines above the postcode, since an address has three lines of its own at most; when it is a
    single word with the address starting under it; or when a clear space parts it from what
    follows. A line that holds a number, or begins as an address does, is the address. `column` and
    `texts` end where the card does."""
    if _POSTCODE.match(texts[0]) or re.match(r"\W*(?:NO\.?\s*)?\d+[A-Z]?(?:[-/]\d+[A-Z]?)*(?:\s|$)", texts[0], re.I) \
            or (_ADDRESS_BEGINS.match(texts[0]) and re.search(r"\d", texts[0])):
        # the address's first line stands where the name should, a house number at its start ("NO 29", "12-3A JALAN
        # ..."): the name's own line was not read. (A name whose first letter was read as a digit, "0NG AH KOW", is a name.)
        return 0
    until = len(texts) if post is None else post  # the lines that may be the name or the address's own lines
    gaps = [_row(b) - _row(a) for a, b in zip(column, column[1:])]
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
        last_own = post is not None and cut >= until - 1  # the last line the address could own
        if _NAME_GOES_ON.search(above) or (_NAME_CARRIED_ON.match(line) and not (last_own and re.match(r"\W*ANAK\b", line, re.I))):
            cut += 1  # ("ANAK BUKIT" over the postcode is the place of that name, not a father's name carried on)
            continue
        if _ADDRESS_BEGINS.match(line) or (apart is not None and apart < cut) or cut >= until - 1:
            break  # an address's line, or past the space that parts the two, or the last line the address could own
        if (post is not None and cut < post - 3) or (apart is not None and cut <= apart) \
                or (" " not in line.strip() and len(line.strip()) >= 2 and re.search(r"\d", texts[cut + 1])):
            cut += 1
            continue
        break
    return cut


def _crosses(a: list, b: list) -> bool:
    """Do two boxes of four corners each overlap? Two such shapes are apart when the whole of one lies beyond
    some side of the other."""
    for box, other in ((a, b), (b, a)):
        for n in range(4):
            (x0, y0), (x1, y1) = box[n], box[(n + 1) % 4]
            mine = [(y1 - y0) * (x - x0) - (x1 - x0) * (y - y0) for x, y in box]
            theirs = [(y1 - y0) * (x - x0) - (x1 - x0) * (y - y0) for x, y in other]
            if max(mine) < min(theirs) or max(theirs) < min(mine):
                return False
    return True


def _printed(text: str) -> bool:
    """Whether a line can be the card's own print, which is capitals throughout: a letter or two read small is
    the reader's slip ("LAND PUBLIc"), a line of small letters is something laid over the card or beside it."""
    small = sum(ch.islower() for ch in text)
    return small <= 2 and 4 * small <= sum(ch.isalpha() for ch in text)


def parse(pages: list[dict], today: Optional[date] = None) -> Optional[dict]:
    """The card's fields and checks, or None when the pages are not a MyKad."""
    for page in pages:
        lines = page.get("lines") or []
        whole = " ".join(ln["text"] for ln in lines)
        if len(lines) > MAX_LINES or _OTHER.search(whole):
            continue
        heads = [ln for ln in lines if _is_heading(ln["text"])]
        if not heads and not _reads_as_a_card(lines):
            continue
        if _is_a_form([" ".join(ln["text"].split()) for ln in lines]) and len({headings.key(ln["text"]) for ln in heads}) < 2:
            continue  # a form that names a card; a card lying on a form has its own two headings
        # the card's number: under the heading, alone on its line (a twelve-digit run in a file's name or a
        # clock above the card is not it). A number that stands only above the heading is a card on its head:
        # nothing is read from it this way up, and the caller reads the page turned over.
        under = min(_top(ln) for ln in heads) if heads else min(_top(ln) for ln in lines) - 1
        numbered = [ln for ln in sorted(lines, key=_top) if _NUMBER.search(ln["text"]) and _top(ln) >= under]
        holder = next((ln for ln in numbered if _ALONE.fullmatch(ln["text"].strip())), None) or (numbered[0] if numbered else None)
        if holder is None:
            continue
        digits = "".join(_NUMBER.search(holder["text"]).groups())
        f: dict = {"id_number": f"{digits[:6]}-{digits[6:8]}-{digits[8:]}"}
        # places are measured from the number: the name and the address start under its first digit, and
        # the citizenship, religion and gender stand well to the right of it
        start, span = _left(holder), max(_right(holder) - _left(holder), 1.0)
        # the card's own lines run as its number does: one laid across the card at another angle is a stamp or a
        # watermark (an app's "Registration" across the photo it keeps), not the card's print. Each line is held
        # against the direction the page's lines have at its place, which changes down a card photographed from one
        # side; where too few lines tell that, or the card lies at another angle than most of the picture, against
        # the number's own slope.
        offs = headings.off_trend(lines)
        if offs.get(id(holder), 90.0) < 2 * headings.AGREE:
            across = {id(ln) for ln in headings.laid_across(lines, offs)}
        else:
            slope = headings.lean(holder)
            across = {id(ln) for ln in lines if slope is not None and headings.lean(ln) is not None
                      and headings.ACROSS <= abs(headings.lean(ln) - slope) < headings.UPRIGHT}
        below = [ln for ln in sorted(lines, key=_top) if _top(ln) > _top(holder) and not _is_background(ln["text"])
                 and id(ln) not in across and not ln.get("across")]
        column = []  # the name and the address: the lines under the number, starting where it starts
        beside = []  # the citizenship, religion and gender: right of the address on the older card, left of it on the new
        # where the column's lines start, followed down the card: photographed at an angle its left edge slants,
        # so each line may start a little further over than the one above, the more so the lower it stands
        edge, level = start, _top(holder)
        for ln in below:
            text = " ".join(ln["text"].split())
            if TITLE.search(text):
                if column:
                    break  # another card's heading, lower in the picture: what stands under it is not this card's
                continue
            if abs(_left(ln) - edge) <= 0.12 * span + 0.15 * max(_top(ln) - level, 0.0):  # the name's or the address's
                lettered = re.match(r"[HK]\s+(?=\d{5}(?!\d))", text)  # the letter of Sabah or Sarawak run into the postcode's line
                if lettered:
                    text = text[lettered.end():]
                # a single character is a speck or the letter of Sabah or Sarawak, unless it is a digit: a house
                # number may stand alone on the address's first line ("5", "12", "8A"), a short name on the name's last ("NG")
                # (always under another line of the column: a scrap above the name is not the name)
                if (len(text) > 2 or (column and (re.search(r"\d", text) or re.fullmatch(r"[A-Z]{2}", text)))) and _printed(text):
                    column.append({**ln, "text": text} if lettered else ln)
                    edge, level = _left(ln), _top(ln)
                continue
            words, rest = _beside(text, glued=True)
            first, _, more = text.partition(" ")
            if not words and more and _misread(first):  # the first of those words read with a letter wrong: "IS'AM SELANGOR"
                others, rest = _beside(more, glued=True)
                words = [_misread(first)] + others
            if words and not rest:
                beside += words
            elif words and _left(ln) < edge:
                # it starts left of the column with those words and runs on into it: the reader gave them and a
                # line of the address as one. What follows them starts about where the column does.
                reaches = _left(ln) + (_right(ln) - _left(ln)) * (1 - len(rest) / max(len(text), 1))
                if abs(reaches - edge) <= 0.3 * span:
                    beside += words
                    if len(rest) > 2:
                        column.append({**ln, "text": rest})
            elif _misread(text):
                beside.append(_misread(text))
        heights = sorted(_height(ln) for ln in lines if ln is not holder)
        if heights and _height(holder) < 0.7 * heights[len(heights) // 2]:
            # the number was taken from the small print under the photo, the large one could not be read: there
            # is then nothing to place the name and the address by, and they are left out rather than misplaced.
            # The citizenship, religion and gender are looked for anywhere on the card.
            column, beside = [], []
            for ln in lines:
                words, rest = _beside(" ".join(ln["text"].split()))
                if words and not rest:
                    beside += words
        for text in beside:
            if text in _GENDERS:
                f["gender"] = _GENDERS[text]
            elif _CITIZEN.fullmatch(text):
                f["citizenship"] = text.replace("WARGA NEGARA", "WARGANEGARA")
            elif _RELIGION.fullmatch(text):
                f["religion"] = text
        if not heads and not column:
            continue  # no heading to tell which way up the card is, and nothing under the number this way up
        read = None
        if column:
            texts = [" ".join(ln["text"].split()) for ln in column]
            # the address ends with its state, the line under the postcode: what stands lower is not the card's
            post, last = _address_end(column, texts)
            if not heads and post is None:
                continue  # no heading, and no address that ends as a card's does: a caller's word alone does not make it one
            column, texts = column[:last], texts[:last]
            if any(t.upper() in _GENDERS or _CITIZEN.fullmatch(t) for t in texts):
                continue  # the gender or the citizenship stands among the name and the address: a list of a person's
                #           details, not the card, which prints them beside the address
            cut = _name_lines(column, texts, post)
            if cut:
                read = " ".join(texts[:cut])
                f["name"] = _tidy_name(read.upper(), muslim=f.get("religion") == "ISLAM")
            address = [re.sub(r"^NO(\.?)(?=\d)", r"NO\1 ", _plain(t)) for t in texts[cut:last]][:6]
            if post is not None and cut <= post < cut + len(address):  # "98000MIRI": the postcode and its town run together
                address[post - cut] = re.sub(r"^(\W*\d{5})(?=[A-Za-z])", r"\1 ", address[post - cut])
            if address:
                f["address"] = ", ".join(address)
            if post is not None and post >= cut:
                m = _POSTCODE.match(texts[post])
                f["postcode"], f["city"] = m.group(1), m.group(2).strip(" ,") or None
                if post + 1 < last:
                    f["state"] = texts[post + 1]
                elif f["city"] and _TERRITORY.match(f["city"]):
                    f["state"] = f["city"]  # a federal territory is its own town: "62551 W.P PUTRAJAYA" and no line under it
        extra = {"name_as_read": read} if read and read != f.get("name") else {}
        birth = born(digits, today)
        f["date_of_birth"] = birth
        f["place_of_birth"] = STATES.get(digits[6:8]) or ("outside Malaysia" if digits[6:8] in ABROAD else None)
        checks = [{"name": "number_holds_a_date", "passed": birth is not None,
                   "detail": "the first six digits of the number are a date of birth" if birth
                   else f"the first six digits, {digits[:6]}, are not a date"}]
        if f.get("gender"):
            by_number = "male" if int(digits[-1]) % 2 else "female"
            checks.append({"name": "gender_matches_number", "passed": by_number == f["gender"],
                           "detail": f"the number's last digit says {by_number}, the card prints {f['gender']}"})
        printed, of_postcode = _state_named(f.get("state")), POSTCODES.get((f.get("postcode") or "")[:2])
        if of_postcode:
            of_postcode = of_postcode + ALSO_USED_IN.get(f["postcode"], ())
        if printed and of_postcode:
            checks.append({"name": "postcode_matches_state", "passed": printed in of_postcode,
                           "detail": f"postcode {f['postcode']} is used in {' and '.join(of_postcode)}, the card prints {f['state']}"})
        # a stamp or a watermark on the card (the reader of the picture marks its lines, and the lines it read
        # with the stamp's ink taken out): where it lies across the number, the name or the address, what was read
        # under it may be wrong, and what could not be read is missing
        stamp = list(page.get("across") or []) + [ln for ln in lines if ln.get("across")]
        if stamp or any(ln.get("under_stamp") for ln in lines):
            block = []
            if column:
                xs, ys = [p[0] for ln in column for p in ln["box"]], [p[1] for ln in column for p in ln["box"]]
                block = [[[min(xs), min(ys)], [max(xs), min(ys)], [max(xs), max(ys)], [min(xs), max(ys)]]]
            over = any(ln.get("under_stamp") for ln in [holder] + column) or (bool(stamp) and "name" not in f) \
                or any(_crosses(st["box"], area) for st in stamp if st.get("over_print") for area in [holder["box"]] + block)
            checks.append({"name": "clear_of_stamp", "passed": not over,
                           "detail": "a stamp or watermark lies across the card's number, name or address: what was read "
                                     "under it may be wrong, and what could not be read is left out" if over
                           else "a stamp or watermark lies on the card, clear of its number, name and address"})
        return {"fields": f, "checks": checks, "extra": extra, "page": page.get("page", 1)}
    return None
