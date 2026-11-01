"""Detektiv: za svaku kandidat-firmu pronađi stranicu sa oglasima.

Čita candidates.yaml, otvara naslovnu stranicu svake firme, traži linkove tipa
"Karijera", "Posao", "Konkursi", "Careers"... i probava uobičajene putanje.
Rezultat: reports/discovery.md (za čitanje) i reports/discovery.json.

Pokretanje: python -m radar.discover [--only ime1,ime2]
"""
import argparse
import concurrent.futures as cf
import json
import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml
from bs4 import BeautifulSoup

from .extract import clean_url, links_from_html
from .fetch import FetchError, get
from .keywords import normalize

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("radar.discover")

CAREER_RE = re.compile(
    r"(karijer|\bposao\b|\bposlovi\b|zaposljav|zaposlen|konkurs|\boglasi?\b|natjecaj|natjeca|"
    r"career|\bjobs?\b|vacanc|join[- ]?us|pridruzi|otvorene pozicije|open positions|"
    r"ljudski resursi|human resources|stipendij|praks[ae]|internship|javni oglas)"
)
PROBE_PATHS = [
    "karijera", "karijere", "karijera/", "posao", "poslovi", "zaposljavanje", "zapošljavanje",
    "konkursi", "oglasi", "javni-oglasi", "natjecaji", "careers", "jobs", "career",
    "o-nama/karijera", "bs/karijera", "ba/karijera", "en/careers", "ljudski-resursi",
]


def career_links(html, base):
    soup = BeautifulSoup(html, "lxml")
    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = clean_url(urljoin(base, href))
        text = re.sub(r"\s+", " ", a.get_text(" ")).strip()
        hay = normalize(text + " " + urlparse(url).path.replace("-", " ").replace("_", " "))
        if CAREER_RE.search(hay) and not re.search(r"(facebook|instagram|linkedin|twitter|youtube)\.com", url):
            found.setdefault(url, text[:80])
    return found


def inspect(cand):
    base = cand["url"]
    out = {"name": cand["name"], "category": cand.get("category", ""), "url": base,
           "home": None, "pages": [], "error": None}
    html = None
    for attempt_url in [base] + _alternatives(base):
        try:
            r = get(attempt_url, timeout=25, retries=1)
            html, base = r.text, r.url
            out["home"] = r.url
            break
        except FetchError as e:
            out["error"] = str(e)
    if html is None:
        return out
    out["error"] = None
    links = career_links(html, base)
    # Probaj i standardne putanje (samo ako ih naslovna ne linkuje)
    root = f"{urlparse(base).scheme}://{urlparse(base).netloc}/"
    probes = [urljoin(root, p) for p in PROBE_PATHS]
    candidates = list(links.items())[:6] + [(p, "(proba)") for p in probes if clean_url(p) not in links]
    checked = 0
    for url, text in candidates:
        if checked >= 10 or len(out["pages"]) >= 4:
            break
        try:
            r = get(url, timeout=20, retries=0)
        except FetchError:
            if text != "(proba)":
                out["pages"].append({"url": url, "text": text, "status": "greška", "jobs": 0, "sample": []})
            continue
        finally:
            checked += 1
        final = clean_url(r.url)
        if text == "(proba)" and (final == clean_url(root) or len(r.text) < 500):
            continue  # preusmjerenje na naslovnu = putanja ne postoji
        if any(p["url"] == final for p in out["pages"]):
            continue
        ctype = r.headers.get("content-type", "")
        if "pdf" in ctype:
            out["pages"].append({"url": final, "text": text, "status": "PDF", "jobs": 0, "sample": []})
            continue
        items = links_from_html(r.text, final, {"id": "discover"})
        body = normalize(BeautifulSoup(r.text, "lxml").get_text(" "))[:20000]
        signal = len(re.findall(r"(oglas|konkurs|pozicij|position|apply|prijav|rok za|deadline)", body))
        out["pages"].append({"url": final, "text": text, "status": "ok", "jobs": len(items),
                             "signal": signal,
                             "sample": [f"{i['title'][:70]} -> {i['url']}" for i in items[:6]]})
    return out


def _alternatives(url):
    p = urlparse(url)
    host = p.netloc
    alts = []
    if host.startswith("www."):
        alts.append(f"{p.scheme}://{host[4:]}{p.path}")
    else:
        alts.append(f"{p.scheme}://www.{host}{p.path}")
    if p.scheme == "https":
        alts.append(f"http://{host}{p.path}")
    return alts


def write_report(results):
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "discovery.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    ok = [r for r in results if r["home"]]
    out = ["# Detektiv: stranice sa oglasima\n",
           f"Kandidata: {len(results)}, dostupnih: {len(ok)}, nedostupnih: {len(results) - len(ok)}\n"]
    for cat in sorted({r["category"] for r in results}):
        out.append(f"\n## {cat or 'ostalo'}\n")
        for r in [x for x in results if x["category"] == cat]:
            if not r["home"]:
                out.append(f"- ❌ **{r['name']}** ({r['url']}): {r['error']}")
                continue
            if not r["pages"]:
                out.append(f"- ⚪ **{r['name']}** ({r['home']}): nema stranice karijera")
                continue
            out.append(f"- ✅ **{r['name']}** ({r['home']})")
            for p in r["pages"]:
                out.append(f"  - [{p['text'] or p['url']}]({p['url']}) [{p['status']}] "
                           f"linkova-oglasa: {p['jobs']}, signal: {p.get('signal', 0)}")
                for s in p["sample"][:4]:
                    out.append(f"    - {s}")
    (reports / "discovery.md").write_text("\n".join(out) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    cands = yaml.safe_load((ROOT / "candidates.yaml").read_text(encoding="utf-8"))["candidates"]
    if args.only:
        wanted = set(args.only.split(","))
        cands = [c for c in cands if c["name"] in wanted]
    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(inspect, cands))
    write_report(results)
    print(f"Gotovo: {len(results)} kandidata, izvještaj u reports/discovery.md")


if __name__ == "__main__":
    main()
