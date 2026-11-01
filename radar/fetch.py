"""HTTP i Playwright dohvatanje stranica."""
import io
import logging
import time

import requests
import urllib3

log = logging.getLogger("radar.fetch")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "bs-BA,bs;q=0.9,hr;q=0.8,sr;q=0.7,en;q=0.6",
}

_session = requests.Session()
_session.headers.update(HEADERS)


class FetchError(Exception):
    pass


def get(url: str, *, insecure: bool = False, timeout: int = 30, retries: int = 2,
        accept_json: bool = False) -> requests.Response:
    """GET sa ponovnim pokušajima. Baca FetchError ako ne uspije."""
    last = None
    headers = {"Accept": "application/json"} if accept_json else None
    for attempt in range(retries + 1):
        try:
            r = _session.get(url, timeout=timeout, verify=not insecure,
                             headers=headers, allow_redirects=True)
            if r.status_code == 429 or r.status_code >= 500:
                last = FetchError(f"HTTP {r.status_code}")
            elif r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code}")
            else:
                if not r.encoding or r.encoding.lower() == "iso-8859-1":
                    r.encoding = r.apparent_encoding or "utf-8"
                return r
        except requests.exceptions.SSLError as e:
            if not insecure:
                # Mnogi .gov.ba sajtovi imaju pokvaren certifikat; probaj bez provjere
                log.info("SSL greška na %s, pokušavam bez provjere certifikata", url)
                insecure = True
                last = FetchError(f"SSL: {e.__class__.__name__}")
                continue
            last = FetchError(f"SSL: {e}")
        except requests.exceptions.RequestException as e:
            last = FetchError(f"{e.__class__.__name__}: {e}")
        time.sleep(2 * (attempt + 1))
    raise last or FetchError("nepoznata greška")


def pdf_text(content: bytes, max_pages: int = 15) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        parts = []
        for page in reader.pages[:max_pages]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # pokvarena stranica u PDF-u
                continue
        return "\n".join(parts)
    except Exception as e:
        log.info("PDF se ne može pročitati: %s", e)
        return ""


class Browser:
    """Jedan Chromium za sve JS stranice u jednom pokretanju."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None

    def _ensure(self):
        if self._browser:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(
            user_agent=UA, locale="bs-BA", ignore_https_errors=True,
            viewport={"width": 1366, "height": 2000},
        )

    def html(self, url: str, wait_selector: str = None, timeout_ms: int = 45000) -> str:
        try:
            self._ensure()
        except Exception as e:
            raise FetchError(f"Playwright nije dostupan: {e}")
        page = self._context.new_page()
        try:
            try:
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            except Exception:
                # networkidle zna nikad ne doći (chat widgeti); uzmi šta je učitano
                page.wait_for_timeout(3000)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=15000)
                except Exception:
                    pass
            # Neki sajtovi učitavaju oglase tek na scroll
            for _ in range(3):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(700)
            return page.content()
        except Exception as e:
            raise FetchError(f"Playwright: {e.__class__.__name__}: {e}")
        finally:
            page.close()

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
