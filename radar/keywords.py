"""Ključne riječi za prepoznavanje IT i entry-level oglasa.

Sve se poredi nad tekstom bez dijakritike i malim slovima (č->c, đ->dj...).
Svaki pojam je regex. \\b znači granicu riječi, pa "ict" ne pogađa "district".
Dodaj ili ukloni pojmove ovdje ako ti nešto promiče ili smeta.
"""
import re
import unicodedata

IT_PATTERNS = [
    r"informati\w*",            # informatičar, informatika, informatizacija
    r"informacion\w*", r"informacijsk\w*",
    r"\bikt\b", r"\bict\b",
    r"programer\w*", r"programiranj\w*", r"\bprogramsk\w*",
    r"developer\w*", r"\bdevelopment\b", r"razvoj (softvera|aplikacij\w*|sistema)",
    r"software", r"softver\w*",
    r"sistem(ski)? administrator\w*", r"administrator\w* (sistema|mreze|baza)",
    r"sistemsk\w* inzenjer\w*", r"system (engineer|administrator)", r"\bsysadmin\b",
    r"\bmrezn\w*", r"\bnetwork\w*",
    r"\bdevops\b", r"\bcloud\b", r"data cent(er|re)", r"\bdatacenter\b",
    r"\bdata (analyst|engineer|scien\w*)", r"analiticar\w* podataka",
    r"baz[aeiu] podataka", r"\bdatabase\b", r"\bsql\b",
    r"\bqa\b", r"quality assurance", r"\btester\w*", r"testiranj\w* (softvera|aplikacij\w*)",
    r"test automation",
    r"\bcyber\w*", r"kibernet\w*", r"sigurnost\w* informacij\w*", r"informacijsk\w* sigurnost\w*",
    r"security (engineer|analyst|specialist)",
    r"help ?desk", r"service desk", r"support engineer", r"tehnick\w* podrsk\w*",
    r"technical support", r"\bit (podrsk\w*|support|specijalist\w*|specialist|administrator\w*|menadzer\w*|manager)",
    r"front[- ]?end", r"back[- ]?end", r"full[- ]?stack",
    r"\bweb (dizajn\w*|developer|programer\w*|aplikacij\w*)",
    r"mobile (developer|engineer)", r"\bandroid\b", r"\bios\b",
    r"\berp\b", r"\bsap\b", r"power ?bi\b", r"business intelligence", r"\bbi (analyst|developer|analiticar\w*)",
    r"machine learning", r"\bai (engineer|developer|specialist)", r"umjetn\w* inteligencij\w*",
    r"artificial intelligence",
    r"telekomunikacij\w*", r"\btelecom\w*",
    r"elektrotehni\w*", r"\betf\b",
    r"racunar\w*", r"kompjuter\w*", r"\bcomputer\w*", r"hardver\w*", r"\bhardware\b",
    r"digitaliz\w*", r"digital transformation", r"automatizacij\w*",
    r"\bpython\b", r"\bjava\b", r"\.net\b", r"javascript", r"\bphp\b", r"\breact\b",
    r"\bangular\b", r"\blinux\b", r"\boracle\b",
    r"\bscrum\b", r"product owner", r"\bux\b", r"\bui ?/ ?ux\b", r"\bui designer\b",
]

ENTRY_PATTERNS = [
    r"pripravni\w*", r"\bpraks[aeiou]\w*", r"\bintern(ship|s)?\b", r"\bjunior\w*",
    r"\btrainee\w*", r"\bstudent\w*", r"\bvolonter\w*", r"\bstazist\w*",
    r"\bpocetni\w*", r"\bgraduate\b", r"entry[- ]level", r"\bapsolvent\w*",
    r"stipendij\w*", r"bez (radnog )?iskustva",
]

_IT_RE = [re.compile(p) for p in IT_PATTERNS]
_ENTRY_RE = [re.compile(p) for p in ENTRY_PATTERNS]

# "IT" kao zasebna riječ (velikim slovima) u originalnom tekstu.
_IT_UPPER = re.compile(r"(?<![A-Za-z])IT(?![A-Za-z])")
# "it" kao riječ u naslovu (naslovi su kratki, pa je lažnih pogodaka malo).
_IT_WORD = re.compile(r"\bit\b")

_TRANSLIT = str.maketrans({"đ": "dj", "Đ": "Dj", "ß": "ss"})


def normalize(text: str) -> str:
    """Mala slova, bez dijakritike, jedan razmak."""
    if not text:
        return ""
    text = text.translate(_TRANSLIT)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


def find_it_match(text: str, is_title: bool = False):
    """Vrati pronađeni IT pojam (kako piše u tekstu) ili None."""
    if not text:
        return None
    if _IT_UPPER.search(text):
        return "IT"
    norm = normalize(text)
    if is_title and _IT_WORD.search(norm):
        return "IT"
    for rx in _IT_RE:
        m = rx.search(norm)
        if m:
            return m.group(0)
    return None


def is_entry_level(text: str) -> bool:
    norm = normalize(text)
    return any(rx.search(norm) for rx in _ENTRY_RE)


def snippet_around(text: str, term: str, width: int = 110) -> str:
    """Kratak isječak teksta oko pronađenog pojma, za tijelo notifikacije."""
    if not text or not term:
        return ""
    flat = re.sub(r"\s+", " ", text)
    if term == "IT":
        m = _IT_UPPER.search(flat)
        idx = m.start() if m else -1
    else:
        # normalize() čuva dužinu osim za đ->dj, pa je pozicija dovoljno tačna
        idx = normalize(flat.replace("đ", "d").replace("Đ", "D")).find(term)
    if idx < 0:
        return ""
    start = max(0, idx - width // 2)
    end = min(len(flat), idx + width // 2 + len(term))
    piece = flat[start:end].strip()
    return ("..." if start > 0 else "") + piece + ("..." if end < len(flat) else "")
