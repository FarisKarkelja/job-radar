"""Izvlačenje oglasa iz različitih vrsta izvora.

Svaki handler vraća listu dict-ova: {key, title, url, company, location}.
`key` je stabilan identifikator oglasa (obično normalizovan URL).
"""
import hashlib
import json
import logging
import re
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode, quote_plus

import feedparser
from bs4 import BeautifulSoup

from .fetch import get, FetchError

log = logging.getLogger("radar.extract")

MAX_ITEMS = 400

_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
             "fbclid", "gclid", "ref", "refid", "trackingid", "trk", "position", "pagenum"}

_SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".css", ".js", ".ico",
             ".zip", ".mp4", ".mp3")

_NAV_TEXT = re.compile(
    r"^(pocetna|naslovna|home|kontakt\w*|contact\w*|o nama|about( us)?|prijava|prijavi se|login|"
    r"log in|registr\w*|sign (in|up)|privatnost|privacy\w*|politika privatnosti|cookie\w*|"
    r"kolacic\w*|uslovi\w*|uvjeti\w*|terms\w*|impressum|faq|pomoc|help|blog|vijesti|novosti|"
    r"news|galerija|gallery|mapa sajta|sitemap|nazad|back|vise|more|procitaj vise|read more|"
    r"detaljnije|saznaj vise|pogledaj|pogledaj sve|sve|svi oglasi|all jobs|next|prev\w*|"
    r"sljedec\w*|prethodn\w*|\d+|\W*)$"
)
_NAV_HREF = re.compile(
    r"(facebook\.com|instagram\.com|twitter\.com|x\.com/|youtube\.com|tiktok\.com|"
    r"linkedin\.com/(company|in|school)/|wa\.me|viber:|/kontakt|/contact|/privacy|/politika|"
    r"/cookie|/login|/prijava$|/register|/registracija|/wp-login|/feed/?$|/tag/|/author/|"
    r"/category/(?!javni-oglasi)|\?share=|/page/\d+|[?&]page=\d+|/search)",
    re.I,
)


def clean_url(url: str) -> str:
    """Ukloni tracking parametre i fragment, da isti oglas uvijek ima isti ključ."""
    try:
        p = urlparse(url)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in _TRACKING]
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", urlencode(q), ""))
    except Exception:
        return url


def _txt(s) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _norm_ascii(s: str) -> str:
    from .keywords import normalize
    return normalize(s)


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _base_domain(host: str) -> str:
    parts = host.split(".")
    # .com.ba / .gov.ba / .co.ba zadrži tri dijela
    if len(parts) >= 3 and parts[-2] in {"com", "gov", "co", "org", "edu", "net"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


_GENERIC_TEXT = re.compile(
    r"^(detaljnije|vise|opsirnije|saznaj vise|procitaj vise|pogledaj( oglas| vise)?|"
    r"read more|more|more info|details|view( job| details)?|apply( now)?|prijavi se|"
    r"apliciraj|otvori|link|ovdje|here|klikni ovdje|preuzmi|download)\W*$"
)


def _anchor_text(a) -> str:
    text = _txt(a.get_text(" "))
    if len(text) < 6:
        text = _txt(a.get("title") or a.get("aria-label") or text)
    if len(text) < 6 or _GENERIC_TEXT.match(_norm_ascii(text)):
        # naslov oglasa je često u kartici oko linka ("Detaljnije" dugme)
        node = a
        for _ in range(4):
            node = node.parent
            if node is None or node.name in ("body", "main", "html"):
                break
            if len(node.find_all("a", href=True)) > 3:
                break
            head = node.find(["h1", "h2", "h3", "h4", "h5", "strong", "b"])
            if head and len(_txt(head.get_text(" "))) >= 6:
                return _txt(head.get_text(" "))
    return text


def links_from_html(html: str, base_url: str, src: dict) -> list:
    """Generički izvlač: linkovi koji liče na pojedinačne oglase.

    Opcije izvora:
      pattern   regex nad apsolutnim URL-om koji oglas MORA zadovoljiti
      exclude   regex nad URL-om ili tekstom koji isključuje link
      selector  CSS selektor koji ograničava dio stranice
      any_domain  dozvoli linkove na druge domene (npr. ATS)
    """
    soup = BeautifulSoup(html, "lxml")
    scope = soup
    if src.get("selector"):
        found = soup.select(src["selector"])
        if found:
            scope = BeautifulSoup("".join(str(x) for x in found), "lxml")
    for bad in scope.select("nav, header, footer, script, style, noscript"):
        if not src.get("keep_nav"):
            bad.decompose()

    pattern = re.compile(src["pattern"], re.I) if src.get("pattern") else None
    exclude = re.compile(src["exclude"], re.I) if src.get("exclude") else None
    listing = clean_url(base_url)
    base_dom = _base_domain(_domain(base_url))
    min_len = src.get("min_title", 6)

    found = {}
    for a in scope.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = clean_url(urljoin(base_url, href))
        if not url.startswith("http"):
            continue
        if url == listing or url.lower().endswith(_SKIP_EXT):
            continue
        text = _anchor_text(a)
        if _GENERIC_TEXT.match(_norm_ascii(text)) or len(text) < min_len:
            slug = _slug_title(url)
            if pattern and slug:
                text = slug
        if exclude and (exclude.search(url) or exclude.search(text)):
            continue
        if pattern:
            if not pattern.search(url):
                continue
        else:
            if not _looks_like_job(url, text, base_dom, src, min_len):
                continue
        if len(text) < min_len and not url.lower().endswith(".pdf"):
            continue
        if url.lower().endswith(".pdf") and len(text) < min_len:
            text = urlparse(url).path.rsplit("/", 1)[-1]
        prev = found.get(url)
        if not prev or len(text) > len(prev["title"]):
            found[url] = {"key": url, "title": text[:240], "url": url}
        if len(found) >= MAX_ITEMS:
            break
    return list(found.values())


def _slug_title(url: str) -> str:
    """'/posao/strucni-saradnik-ikt-99/' -> 'Strucni saradnik ikt'."""
    segs = [s for s in urlparse(url).path.split("/") if s]
    for seg in reversed(segs):
        words = re.sub(r"\.(html?|php|aspx?)$", "", seg)
        words = re.sub(r"[-_+]+", " ", words)
        words = re.sub(r"\b\d+\b", "", words).strip()
        if len(words) >= 6:
            return words[:1].upper() + words[1:]
    return ""


def _looks_like_job(url, text, base_dom, src, min_len) -> bool:
    if _NAV_HREF.search(url):
        return False
    if _NAV_TEXT.match(_norm_ascii(text)):
        return False
    if len(text) < min_len or len(text) > 220:
        return False
    if not src.get("any_domain") and _base_domain(_domain(url)) != base_dom:
        return False
    path = urlparse(url).path
    segs = [s for s in path.split("/") if s]
    if not segs:
        return False
    return (len(segs) >= 2 or bool(re.search(r"\d", path)) or len(segs[-1]) >= 18
            or path.lower().endswith(".pdf"))


def handle_links(src, browser):
    url = src["url"]
    if src.get("js"):
        html = browser.html(url, wait_selector=src.get("wait_for"))
    else:
        html = get(url, insecure=src.get("insecure", False)).text
    return links_from_html(html, url, src)


def handle_rss(src, browser):
    r = get(src.get("feed") or src["url"], insecure=src.get("insecure", False))
    feed = feedparser.parse(r.content)
    if feed.bozo and not feed.entries:
        raise FetchError(f"RSS nije validan: {feed.bozo_exception}")
    items = []
    for e in feed.entries[:MAX_ITEMS]:
        link = clean_url(e.get("link") or "")
        title = _txt(e.get("title"))
        if not link or not title:
            continue
        items.append({"key": link, "title": title, "url": link,
                      "company": _txt(e.get("job_listing_company") or e.get("author") or "")})
    return items


def _walk_jsonld(node, out):
    if isinstance(node, list):
        for n in node:
            _walk_jsonld(n, out)
        return
    if not isinstance(node, dict):
        return
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    if "JobPosting" in types:
        out.append(node)
    for key in ("@graph", "itemListElement", "item", "mainEntity"):
        if key in node:
            _walk_jsonld(node[key], out)
    if "ListItem" in types and "url" in node and "item" not in node:
        out.append({"title": node.get("name"), "url": node.get("url")})


def jsonld_items(html, base_url):
    soup = BeautifulSoup(html, "lxml")
    raw = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            _walk_jsonld(json.loads(s.string or s.get_text() or ""), raw)
        except Exception:
            continue
    items = {}
    for j in raw:
        title = _txt(j.get("title") or j.get("name"))
        link = j.get("url") or j.get("sameAs") or ""
        if not title or not link:
            continue
        link = clean_url(urljoin(base_url, link))
        org = j.get("hiringOrganization") or {}
        company = _txt(org.get("name") if isinstance(org, dict) else str(org))
        loc = j.get("jobLocation") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        addr = loc.get("address", {}) if isinstance(loc, dict) else {}
        city = _txt(addr.get("addressLocality") if isinstance(addr, dict) else "")
        items[link] = {"key": link, "title": title[:240], "url": link,
                       "company": company, "location": city}
    return list(items.values())


def handle_jsonld(src, browser):
    url = src["url"]
    if src.get("js"):
        html = browser.html(url)
    else:
        html = get(url, insecure=src.get("insecure", False)).text
    items = jsonld_items(html, url)
    if not items:
        items = links_from_html(html, url, src)
    return items


def handle_workable(src, browser):
    acc = src["account"]
    r = get(f"https://apply.workable.com/api/v1/widget/accounts/{acc}", accept_json=True)
    data = r.json()
    items = []
    for j in data.get("jobs", []):
        link = clean_url(j.get("url") or j.get("application_url") or
                         f"https://apply.workable.com/{acc}/j/{j.get('shortcode')}")
        loc = ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        items.append({"key": link, "title": _txt(j.get("title")), "url": link,
                      "company": data.get("name") or src["name"], "location": loc})
    return items


def handle_linkedin(src, browser):
    """LinkedIn javna (guest) pretraga, bez prijave. Oglasi iz zadnjih 7 dana."""
    items = {}
    location = src.get("location", "Bosnia and Herzegovina")
    for kw in src["queries"]:
        url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
               f"?keywords={quote_plus(kw)}&location={quote_plus(location)}"
               "&f_TPR=r604800&sortBy=DD&start=0")
        try:
            html = get(url, retries=1).text
        except FetchError as e:
            log.info("LinkedIn '%s' nije uspio: %s", kw, e)
            continue
        soup = BeautifulSoup(html, "lxml")
        for card in soup.select("li"):
            a = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
            if not a:
                continue
            m = re.search(r"(\d{6,})", a["href"])
            if not m:
                continue
            job_id = m.group(1)
            title = _txt((card.select_one("h3") or a).get_text(" "))
            company = _txt((card.select_one("h4") or card).get_text(" ")) if card.select_one("h4") else ""
            loc_el = card.select_one(".job-search-card__location")
            link = f"https://www.linkedin.com/jobs/view/{job_id}"
            items[link] = {"key": link, "title": title, "url": link, "company": company,
                           "location": _txt(loc_el.get_text(" ")) if loc_el else ""}
    if not items:
        raise FetchError("LinkedIn nije vratio nijedan oglas (moguća blokada)")
    return list(items.values())


def page_text(html: str, selector: str = None) -> list:
    soup = BeautifulSoup(html, "lxml")
    for bad in soup.select("script, style, noscript, nav, header, footer, iframe"):
        bad.decompose()
    root = soup.select_one(selector) if selector else None
    root = root or soup.find("main") or soup.body or soup
    lines = [_txt(x) for x in root.get_text("\n").split("\n")]
    # izbaci prazne i očito dinamične linije (datumi, brojači)
    return [l for l in lines if len(l) > 2 and not re.fullmatch(r"[\d\W]+", l)]


def handle_page_hash(src, browser):
    """Za stranice bez liste oglasa: javi kad se tekst stranice promijeni."""
    url = src["url"]
    if src.get("js"):
        html = browser.html(url)
    else:
        html = get(url, insecure=src.get("insecure", False)).text
    lines = page_text(html, src.get("selector"))
    if not lines:
        raise FetchError("stranica je prazna")
    digest = hashlib.sha1("\n".join(lines).encode()).hexdigest()[:16]
    return [{"key": f"hash:{digest}", "title": "Stranica je promijenjena", "url": url,
             "_lines": lines[:400], "_page_hash": True}]


HANDLERS = {
    "links": handle_links,
    "rss": handle_rss,
    "jsonld": handle_jsonld,
    "workable": handle_workable,
    "linkedin": handle_linkedin,
    "page_hash": handle_page_hash,
}


def extract(src: dict, browser) -> list:
    """Pokreni handler; ako ne uspije ili vrati 0, probaj `fallback` konfiguraciju."""
    handler = HANDLERS[src.get("type", "links")]
    try:
        items = handler(src, browser)
        if items or not src.get("fallback"):
            return items
        log.info("%s: 0 oglasa, probam fallback", src["id"])
    except FetchError as e:
        if not src.get("fallback"):
            raise
        log.info("%s: %s, probam fallback", src["id"], e)
    fb = {**src, **src["fallback"]}
    fb.pop("fallback", None)
    return HANDLERS[fb.get("type", "links")](fb, browser)
