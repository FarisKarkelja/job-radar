"""Pravilo lokacije: Sarajevo bilo kako, ostatak BiH samo remote ili hybrid.

Svaki izvor ima `location`:
  sarajevo  izvor objavljuje samo poslove u Sarajevu (SZKS, KJKP, firme iz Sarajeva)
            -> sve prolazi
  bih       portal ili poslodavac iz cijele BiH -> prolazi Sarajevo, remote, hybrid
            ili nepoznata lokacija; odbija se samo kad je jasno navedeno da je posao
            onsite u drugom gradu
  abroad    studentski programi i prakse vani otvorene za BiH -> sve prolazi
            (sužava se `include` filterom u sources.yaml)
"""
import re

from .keywords import normalize

SARAJEVO = re.compile(
    r"\b(sarajev\w*|kanton sarajevo|ilidz\w*|vogosc\w*|hadzic\w*|ilijas\w*|trnov\w*|"
    r"novi grad sarajevo|novo sarajevo|stari grad sarajevo|centar sarajevo|"
    r"istocno sarajevo|lukavica|pale)\b"
)
REMOTE = re.compile(
    r"\b(remote\w*|hybrid\w*|hibrid\w*|work from home|wfh|home ?office|"
    r"rad (od|iz) kuce|rad na daljinu|na daljinu|daljinsk\w*|anywhere|"
    r"fully distributed|distributed team|mjesovit\w* model|kombinovan\w* model)\b"
)
# Ostali gradovi u BiH. Kad se spomene neki od njih, a nema Sarajeva ni remote/hybrid,
# posao je onsite van Sarajeva i ne šalje se.
OTHER_CITY = re.compile(
    r"\b(banj\w* luk\w*|mostar\w*|tuzl\w*|zenic\w*|bihac\w*|bijeljin\w*|brck\w*|doboj\w*|"
    r"prijedor\w*|trebinj\w*|travnik\w*|cazin\w*|gradacac\w*|livn\w*|siroki brijeg|"
    r"gorazd\w*|kakanj\w*|konjic\w*|gracanic\w*|lukavac\w*|zivinic\w*|"
    r"velik\w* kladus\w*|sansk\w* most\w*|zvornik\w*|gradisk\w*|derventa|modric\w*|"
    r"foc[aiue]|jajc[aeu]|bugojn\w*|vitez\w*|busovac\w*|kiseljak\w*|kresev\w*|tesanj\w*|"
    r"maglaj\w*|zavidovic\w*|zepc\w*|srebrenik\w*|kalesij\w*|banovic\w*|capljin\w*|"
    r"stolac\w*|ljubusk\w*|grude|posusj\w*|tomislavgrad\w*|kupres\w*|orasj\w*|"
    r"odzak\w*|laktas\w*|teslic\w*|usora|novi travnik|fojnic\w*|breza|olovo|vares\w*|"
    r"jablanic\w*|prozor[- ]ram\w*|neum\w*|bosanska krupa|bosansk\w* petrovac|"
    r"kotor varos\w*|mrkonjic\w*|sipov\w*|nevesinj\w*|bileca|gacko|visegrad\w*|"
    r"rogatic\w*|srebrenic\w*|bratunac\w*|vlasenic\w*|sokolac\w*|ugljevik\w*|lopar\w*)\b"
)


FOREIGN = re.compile(
    r"\b(zagreb\w*|(?<=- )split|split,? croat\w*|rijek\w*|vodnjan\w*|osijek\w*|zadar\w*|beograd\w*|belgrade|novi sad\w*|"
    r"nis\b|kragujevac\w*|ljubljan\w*|maribor\w*|celje|skopj\w*|skopje|podgoric\w*|tiran\w*|pristin\w*|"
    r"vienna|wien|bec\w*|budapest\w*|prague|prag\w*|warsaw|krakow|bucharest|sofia|munich|berlin|"
    r"london|amsterdam|dublin|paris|madrid|lisbon|milan\w*|zurich|geneva|croatia|hrvatsk\w*|serbia|srbij\w*|"
    r"slovenia|sloven\w*|north macedonia|montenegro|crn\w* gor\w*|germany|njemack\w*|austria|austrij\w*)\b"
)
BIH = re.compile(r"\b(bosn\w*|bih|herceg\w*|federacij\w*|republik\w* srpsk\w*)\b")
VAGUE = re.compile(r"(lokacij|location|multiple|vise|više|\d+ (locations|lokacij))")


def decide(src: dict, item: dict, body: str = "") -> tuple:
    """Vrati (prolazi, razlog)."""
    where = src.get("location", "bih")
    if where in ("sarajevo", "abroad") or item.get("loc_ok"):
        return True, where
    loc = normalize(item.get("location", ""))
    if loc and not (SARAJEVO.search(loc) or REMOTE.search(loc) or BIH.search(loc)
                    or OTHER_CITY.search(loc) or VAGUE.search(loc)):
        return False, "van BiH"
    head = normalize(" ".join(x for x in [item.get("title"), item.get("location"),
                                           item.get("company")] if x))
    # 1) naslov/lokacija sa kartice oglasa su najpouzdaniji
    if SARAJEVO.search(head):
        return True, "sarajevo"
    if REMOTE.search(head):
        return True, "remote/hybrid"
    if OTHER_CITY.search(head):
        return False, "onsite van Sarajeva"
    tl = normalize(" ".join(x for x in [item.get("title"), item.get("location")] if x))
    if FOREIGN.search(tl) and not BIH.search(tl):
        return False, "van BiH"
    # 2) tekst oglasa (prvih ~4000 znakova, tu je obično lokacija)
    text = normalize(body[:4000]) if body else ""
    if text:
        if SARAJEVO.search(text):
            return True, "sarajevo"
        if REMOTE.search(text):
            return True, "remote/hybrid"
        if OTHER_CITY.search(text):
            return False, "onsite van Sarajeva"
    # 3) ne zna se: bolje poslati nego propustiti
    return True, "nepoznato"
