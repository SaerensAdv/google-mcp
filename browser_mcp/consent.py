"""Find and click the accept/reject button of a cookie banner (CMP).

Known CMPs are tried first via fixed selectors, then a text match on visible
buttons/links (NL/FR/EN/DE). All frames are searched (some CMPs use iframes);
Playwright CSS selectors also pierce open shadow roots (e.g. Usercentrics).
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# (cmp name, accept selector, reject selector)
CMP_SELECTORS: Tuple[Tuple[str, str, str], ...] = (
    ("Cookiebot", "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll, #CybotCookiebotDialogBodyButtonAccept",
     "#CybotCookiebotDialogBodyButtonDecline"),
    ("OneTrust", "#onetrust-accept-btn-handler", "#onetrust-reject-all-handler"),
    ("Complianz", ".cmplz-btn.cmplz-accept", ".cmplz-btn.cmplz-deny"),
    ("CookieYes", ".cky-btn-accept", ".cky-btn-reject"),
    ("Didomi", "#didomi-notice-agree-button", "#didomi-notice-disagree-button"),
    ("Usercentrics", "[data-testid='uc-accept-all-button']", "[data-testid='uc-deny-all-button']"),
    ("iubenda", ".iubenda-cs-accept-btn", ".iubenda-cs-reject-btn"),
    ("Axeptio", "#axeptio_btn_acceptAll", "#axeptio_btn_dismiss"),
    ("Cookie Notice", "#cn-accept-cookie", "#cn-refuse-cookie"),
    ("Borlabs", "a[data-cookie-accept-all], ._brlbs-btn-accept-all", "a[data-cookie-refuse], ._brlbs-refuse-btn"),
    ("Moove GDPR", ".moove-gdpr-infobar-allow-all", ".moove-gdpr-infobar-reject-btn"),
    ("Osano", ".osano-cm-accept-all", ".osano-cm-denyAll"),
    ("Klaro", ".cm-btn-accept-all, .cm-btn-success", ".cm-btn-decline"),
    ("Termly", "[data-tid='banner-accept']", "[data-tid='banner-decline']"),
    ("CookieFirst", "[data-cookiefirst-action='accept']", "[data-cookiefirst-action='reject']"),
)

ACCEPT_TEXT = re.compile(
    r"^\s*(alle(s)?\s+(cookies\s+)?(accepteren|aanvaarden|toestaan)|(alle\s+cookies\s+)?accepte(er|ren)(\s+alle(s)?)?"
    r"|aanvaard(en)?(\s+alle(s)?)?|akkoord|ik ga akkoord|alles toestaan|toestaan|ok(é|e)?"
    r"|accept( all( cookies)?)?|allow( all( cookies)?)?|agree|i agree|got it"
    r"|tout accepter|accepter( tout)?|j'accepte|alle akzeptieren|akzeptieren|zustimmen|ich stimme zu)\s*[.!]?\s*$",
    re.I,
)
REJECT_TEXT = re.compile(
    r"^\s*(alle(s)?\s+(cookies\s+)?weigeren|weiger(en)?(\s+alle(s)?)?|niet akkoord|afwijzen|alle(s)? afwijzen"
    r"|(alleen|enkel|uitsluitend)\s+(noodzakelijke|functionele|essentiële|strikt noodzakelijke)(\s+cookies)?(\s+(accepteren|toestaan))?"
    r"|reject( all)?|decline( all)?|deny( all)?|only (necessary|essential)( cookies)?|necessary only|refuse"
    r"|tout refuser|refuser|continuer sans accepter|alle ablehnen|ablehnen|nur notwendige)\s*[.!]?\s*$",
    re.I,
)

# Requests that reveal which CMP the site uses.
CMP_HOSTS = (
    ("consent.cookiebot.com", "Cookiebot"), ("consentcdn.cookiebot.com", "Cookiebot"),
    ("cdn.cookielaw.org", "OneTrust"), ("cookie-cdn.cookiepro.com", "OneTrust"),
    ("cdn-cookieyes.com", "CookieYes"), ("sdk.privacy-center.org", "Didomi"),
    ("app.usercentrics.eu", "Usercentrics"), ("web.cmp.usercentrics.eu", "Usercentrics"),
    ("cdn.iubenda.com", "iubenda"), ("static.axept.io", "Axeptio"), ("cmp.osano.com", "Osano"),
    ("app.termly.io", "Termly"), ("consent.cookiefirst.com", "CookieFirst"),
)


def cmp_from_url(url: str) -> Optional[str]:
    host = (urlsplit(url).hostname or "").lower()
    for h, name in CMP_HOSTS:
        if host == h or host.endswith("." + h):
            return name
    if "/complianz" in url:
        return "Complianz"
    if "/borlabs-cookie" in url:
        return "Borlabs"
    return None


async def _first_visible(locator) -> Optional[object]:
    try:
        count = min(await locator.count(), 5)
    except Exception:  # noqa: BLE001 — detached frames etc.
        return None
    for i in range(count):
        el = locator.nth(i)
        with contextlib.suppress(Exception):
            if await el.is_visible():
                return el
    return None


async def find_consent_button(page, action: str) -> Tuple[Optional[object], Optional[str], Optional[str]]:
    """Returns (element, cmp_name, how) for action 'accept' | 'reject'."""
    idx = 1 if action == "accept" else 2
    frames = list(page.frames)[:15]
    for frame in frames:
        for row in CMP_SELECTORS:
            el = await _first_visible(frame.locator(row[idx]))
            if el is not None:
                return el, row[0], f"knop van {row[0]}"
    pattern = ACCEPT_TEXT if action == "accept" else REJECT_TEXT
    for frame in frames:
        for role in ("button", "link"):
            el = await _first_visible(frame.get_by_role(role, name=pattern))
            if el is not None:
                text = ""
                with contextlib.suppress(Exception):
                    text = (await el.inner_text(timeout=1000)).strip()[:60]
                return el, None, f"{role} met tekst '{text}'"
    return None, None, None


async def click_consent(page, action: str, retries: int = 2, wait_ms: int = 1500) -> Dict[str, object]:
    """Tries to click accept/reject; waits for late-loading banners between attempts."""
    for attempt in range(retries + 1):
        el, cmp, how = await find_consent_button(page, action)
        if el is not None:
            try:
                await el.click(timeout=4000)
                return {"clicked": True, "cmp_button": cmp, "how": how}
            except Exception as e:  # noqa: BLE001
                logger.info("consent click failed: %s", type(e).__name__)
                return {"clicked": False, "cmp_button": cmp, "how": how, "error": "klik mislukt"}
        if attempt < retries:
            await page.wait_for_timeout(wait_ms)
    return {"clicked": False, "cmp_button": None, "how": None}


def banner_findings(accept: Optional[Dict], reject: Optional[Dict], cmps: List[str]) -> List[str]:
    out = []
    if accept is not None and not accept.get("clicked"):
        out.append("Geen 'accepteren'-knop gevonden: geen cookiebanner of een onbekende banner"
                   + (f" ({', '.join(cmps)} geladen)" if cmps else "") + ".")
    if reject is not None and not reject.get("clicked") and (accept is None or accept.get("clicked")):
        out.append("Geen 'weigeren'-knop op de eerste laag van de cookiebanner gevonden "
                   "(weigeren moet even makkelijk zijn als accepteren).")
    return out
