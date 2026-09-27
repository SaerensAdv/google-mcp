"""Hardened headless-Chromium screenshot capture.

Every capture gets a fresh browser process + context (no cookies, no storage, no
login), all traffic is forced through ``GuardProxy``, and everything is closed in
``finally`` blocks even on errors/timeouts. At most one capture runs at a time.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator
from urllib.parse import urlsplit

from browser_mcp.netguard import BlockedURLError, GuardProxy, resolve_public, validate_input_url

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900, "is_mobile": False},
    "mobile": {"width": 390, "height": 844, "is_mobile": True},
}

NAV_TIMEOUT_MS = _env_int("BROWSER_NAV_TIMEOUT_MS", 25_000)
# Chromium start can be slow on a cold (just-published) VM: first read of the binary from disk.
LAUNCH_TIMEOUT_MS = _env_int("BROWSER_LAUNCH_TIMEOUT_MS", 45_000)
TOTAL_TIMEOUT_S = _env_int("BROWSER_TOTAL_TIMEOUT_S", 60)
FONT_WAIT_S = 5
SCROLL_MAX_STEPS = 30
SCROLL_STEP_DELAY_MS = 400
# Response limits: many vision models reject images > 8000 px on a side or > ~5 MB.
MAX_FULL_PAGE_HEIGHT = _env_int("BROWSER_MAX_FULL_PAGE_HEIGHT", 8000)
# Pages up to this height are captured whole and downscaled to MAX_FULL_PAGE_HEIGHT;
# taller pages fall back to an explicitly labelled viewport capture.
MAX_SCALABLE_PAGE_HEIGHT = _env_int("BROWSER_MAX_SCALABLE_PAGE_HEIGHT", 20000)
MAX_IMAGE_BYTES = _env_int("BROWSER_MAX_IMAGE_BYTES", 3_500_000)
JPEG_QUALITIES = (75, 50)
LOCK_WAIT_S = 5

_capture_lock = asyncio.Lock()

# Fixed, non-user-controllable script: scroll down in viewport steps (bounded) to
# trigger lazy-loaded content, then return to the top.
_SCROLL_JS = """
async ([maxSteps, delay]) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const go = (y) => window.scrollTo({top: y, left: 0, behavior: 'instant'});
  const step = Math.max(200, Math.round(window.innerHeight * 0.8));
  let y = 0, steps = 0;
  while (steps < maxSteps) {
    const max = Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0) - window.innerHeight;
    if (y >= max) break;
    y = Math.min(y + step, max);
    go(y);
    steps++;
    await sleep(delay);
  }
  await sleep(delay);
  go(0);
  return steps;
}
"""

_HEIGHT_JS = """
() => Math.max(
  document.documentElement ? document.documentElement.scrollHeight : 0,
  document.body ? document.body.scrollHeight : 0
)
"""


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Returns (width, height) from a JPEG's SOF marker, or None."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + length
    return None


class CaptureError(Exception):
    """Capture failed; message is safe to return to the client."""


@dataclass
class CaptureResult:
    image: bytes
    metadata: dict = field(default_factory=dict)


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Only these variables are passed to Chromium: no server secrets/tokens in its environment.
_BROWSER_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ", "FONTCONFIG_FILE", "FONTCONFIG_PATH",
                     "XDG_DATA_DIRS", "LOCALE_ARCHIVE", "SSL_CERT_FILE")


def _browser_env() -> dict:
    env = {k: os.environ[k] for k in _BROWSER_ENV_KEYS if k in os.environ}
    env["HOME"] = tempfile.gettempdir()
    return env


def chromium_path() -> str:
    path = os.environ.get("BROWSER_CHROMIUM_PATH") or shutil.which("chromium")
    if not path:
        raise CaptureError("Chromium niet gevonden op deze server.")
    return path


# Chromium's own background services (not page content); blocked but not a page issue.
_BROWSER_INTERNAL_HOSTS = ("clients2.google.com", "mtalk.google.com", "update.googleapis.com")


def _is_browser_internal(target: str) -> bool:
    host = target.split("://")[-1].rsplit(":", 1)[0]
    return host in _BROWSER_INTERNAL_HOSTS


# Chromium stderr markers that mean "no usable sandbox" (as opposed to a slow or broken start).
_SANDBOX_FAILURE_MARKERS = (
    "sandbox helper binary", "no usable sandbox", "suid sandbox", "setuid_sandbox",
    "credentials.cc", "zygote_host", "namespace_sandbox", "sandbox_linux",
)


def _launch_diagnostic(exc: Exception) -> str:
    """Short, log-safe excerpt of a launch failure (Chromium's env holds no secrets)."""
    lines = [ln.strip().lstrip("- ") for ln in str(exc).splitlines()]
    keep = list(dict.fromkeys(
        ln for ln in lines if "FATAL" in ln or "ERROR:" in ln or "exitCode" in ln
    ))[:6]
    first = lines[0] if lines else ""
    return (first + " | " + " | ".join(keep))[:1500]


def _classify_launch_error(exc: Exception) -> str:
    from playwright.async_api import TimeoutError as PWTimeout

    if isinstance(exc, PWTimeout):
        return "timeout"
    text = str(exc).lower()
    if any(m in text for m in _SANDBOX_FAILURE_MARKERS):
        return "sandbox"
    return "other"


def _safe_url_for_log(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.hostname}{p.path}"


@contextlib.asynccontextmanager
async def hardened_page(device: str) -> AsyncIterator[tuple]:
    """Yields (page, proxy, browser_version, sandboxed) with guaranteed cleanup."""
    from playwright.async_api import async_playwright

    vp = VIEWPORTS[device]
    proxy = GuardProxy()
    pw = browser = context = None
    sandboxed = True
    try:
        await proxy.start()
        pw = await async_playwright().start()
        launch_kwargs = dict(
            executable_path=chromium_path(),
            headless=True,
            timeout=LAUNCH_TIMEOUT_MS,
            env=_browser_env(),
            args=[
                f"--proxy-server=http://127.0.0.1:{proxy.port}",
                "--proxy-bypass-list=<-loopback>",
                # Chromium itself may not resolve anything except the local proxy.
                "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--dns-prefetch-disable",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-sync",
                "--disable-extensions",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--mute-audio",
                "--no-pings",
                "--disable-default-apps",
                "--disable-domain-reliability",
                "--disable-client-side-phishing-detection",
                "--disable-features=NetworkTimeServiceQuerying,OptimizationHints,"
                "MediaRouter,Translate,PushMessaging,AutofillServerCommunication",
            ],
        )
        launch_started = time.monotonic()
        try:
            browser = await pw.chromium.launch(chromium_sandbox=True, **launch_kwargs)
        except Exception as e:  # noqa: BLE001
            kind = _classify_launch_error(e)
            elapsed = time.monotonic() - launch_started
            logger.warning(
                "Chromium launch failed (kind=%s, %s, after %.1fs): %s",
                kind, type(e).__name__, elapsed, _launch_diagnostic(e),
            )
            if kind == "timeout":
                raise CaptureError(
                    f"Chromium startte niet binnen {LAUNCH_TIMEOUT_MS // 1000}s "
                    "(trage start van de server); probeer het opnieuw."
                ) from e
            if kind != "sandbox":
                raise CaptureError(
                    f"Chromium kon niet starten ({type(e).__name__}); zie serverlog."
                ) from e
            if not _truthy_env("BROWSER_ALLOW_NO_SANDBOX"):
                raise CaptureError(
                    "Chromium-sandbox is niet beschikbaar op deze server; capture geweigerd. "
                    "(Alleen na bewuste keuze: BROWSER_ALLOW_NO_SANDBOX=true.)"
                ) from e
            logger.warning("Chromium sandbox unavailable; running unsandboxed (explicitly allowed).")
            sandboxed = False
            browser = await pw.chromium.launch(chromium_sandbox=False, **launch_kwargs)
        logger.info(
            "Chromium %s started in %.1fs (sandbox=%s)",
            browser.version, time.monotonic() - launch_started, sandboxed,
        )
        major = browser.version.split(".")[0]
        if vp["is_mobile"]:
            ua = (
                f"Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Mobile Safari/537.36"
            )
        else:
            ua = (
                f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
            )
        context = await browser.new_context(
            viewport={"width": vp["width"], "height": vp["height"]},
            device_scale_factor=1,
            is_mobile=vp["is_mobile"],
            has_touch=vp["is_mobile"],
            user_agent=ua,
            locale="nl-BE",
            timezone_id="Europe/Brussels",
            accept_downloads=False,
            service_workers="block",
            java_script_enabled=True,
            permissions=[],
        )
        page = await context.new_page()
        # No popups, no dialogs: any page other than the main one is closed immediately.
        context.on("page", lambda p: asyncio.ensure_future(p.close()) if p is not page else None)
        page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
        yield page, proxy, browser.version, sandboxed
    finally:
        # Teardown runs in its own task and is shielded: a cancelled MCP request can
        # not interrupt it, and the caller (holding the capture lock) waits for it.
        task = asyncio.ensure_future(_teardown(context, browser, pw, proxy))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:  # noqa: BLE001 — _teardown already logs
                break
        if cancelled:
            raise asyncio.CancelledError


async def _teardown(context, browser, pw, proxy) -> None:
    for name, obj, method in (("context", context, "close"), ("browser", browser, "close"), ("playwright", pw, "stop")):
        if obj is None:
            continue
        try:
            await asyncio.wait_for(getattr(obj, method)(), timeout=10)
        except Exception as e:  # noqa: BLE001 — cleanup must continue
            logger.warning("browser cleanup step %s failed: %s", name, type(e).__name__)
    try:
        await proxy.stop()
    except Exception as e:  # noqa: BLE001
        logger.warning("guard proxy stop failed: %s", type(e).__name__)


async def _screenshot(page, full_page: bool, scale: float = 1.0, size: tuple = (0, 0)) -> tuple[bytes, int]:
    """JPEG screenshot; retries once at lower quality if above MAX_IMAGE_BYTES.

    ``scale < 1`` captures the whole page (``size`` = CSS width/height) downscaled by
    Chromium itself via the DevTools protocol — nothing is cropped.
    """
    last = b""
    cdp = await page.context.new_cdp_session(page) if scale < 1.0 else None
    try:
        for q in JPEG_QUALITIES:
            if cdp is not None:
                res = await asyncio.wait_for(cdp.send("Page.captureScreenshot", {
                    "format": "jpeg", "quality": q, "captureBeyondViewport": True,
                    "clip": {"x": 0, "y": 0, "width": size[0], "height": size[1], "scale": scale},
                }), 20)
                last = base64.b64decode(res["data"])
            else:
                last = await page.screenshot(
                    type="jpeg", quality=q, full_page=full_page,
                    animations="disabled", caret="hide", timeout=20_000,
                )
            if len(last) <= MAX_IMAGE_BYTES:
                return last, q
        return last, JPEG_QUALITIES[-1]
    finally:
        if cdp is not None:
            with contextlib.suppress(Exception):
                await cdp.detach()


async def _do_capture(page, proxy, browser_version, url, device, full_page) -> CaptureResult:
    from playwright.async_api import Error as PWError, TimeoutError as PWTimeout

    warnings: list[str] = []  # anything that means the image may be incomplete
    notes: list[str] = []  # informational (e.g. downscaling)
    status = None
    try:
        resp = await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        status = resp.status if resp else None
    except PWTimeout:
        warnings.append(f"Load-event niet binnen {NAV_TIMEOUT_MS // 1000}s; pagina mogelijk onvolledig.")
    except PWError as e:
        msg = str(e).split("\n")[0]
        raise CaptureError(
            f"Navigatie mislukt: {msg}"
            + (f" | Geblokkeerd door netwerkbeveiliging: {proxy.blocked}" if proxy.blocked else "")
        ) from None
    if page.url in ("", "about:blank"):
        raise CaptureError("Pagina kon niet geladen worden (timeout vóór eerste inhoud).")

    try:
        await asyncio.wait_for(page.evaluate("document.fonts.ready.then(() => true)"), FONT_WAIT_S)
    except (asyncio.TimeoutError, PWError):
        warnings.append("Fonts niet binnen 5s geladen.")

    # SPA content may render after the load event: bounded wait for network idle first.
    with contextlib.suppress(PWTimeout, PWError):
        await page.wait_for_load_state("networkidle", timeout=3_000)

    scroll_steps = 0
    if full_page:
        for _attempt in range(2):  # 2nd pass only if the page grew after a no-op pass
            try:
                scroll_steps += await asyncio.wait_for(
                    page.evaluate(_SCROLL_JS, [SCROLL_MAX_STEPS, SCROLL_STEP_DELAY_MS]), 16
                )
            except (asyncio.TimeoutError, PWError):
                warnings.append("Scrollen voor lazy-loaded content onderbroken.")
                break
            if scroll_steps > 0 or await page.evaluate(_HEIGHT_JS) <= VIEWPORTS[device]["height"]:
                break
            await page.wait_for_timeout(1_000)
    with contextlib.suppress(PWTimeout, PWError):
        await page.wait_for_load_state("networkidle", timeout=3_000)
    await page.wait_for_timeout(500)

    page_height = await page.evaluate(_HEIGHT_JS)
    vp = VIEWPORTS[device]
    mode = "full_page" if full_page else "viewport"
    fallback_reason = None
    scale = 1.0
    if full_page and page_height > MAX_SCALABLE_PAGE_HEIGHT:
        mode = "viewport_fallback"
        fallback_reason = (
            f"Volledige pagina is {page_height}px hoog, boven de limiet van "
            f"{MAX_SCALABLE_PAGE_HEIGHT}px. Teruggegeven: ALLEEN het eerste viewport "
            f"({vp['width']}×{vp['height']}), niet de volledige pagina."
        )
    elif full_page and page_height > MAX_FULL_PAGE_HEIGHT:
        scale = round(MAX_FULL_PAGE_HEIGHT / page_height, 4)
        notes.append(
            f"Volledige pagina ({vp['width']}×{page_height}px) verkleind met factor {scale} "
            f"tot max {MAX_FULL_PAGE_HEIGHT}px hoogte; niets weggesneden, wel minder scherp."
        )

    image, quality = await _screenshot(
        page, full_page=(mode == "full_page"), scale=scale, size=(vp["width"], page_height)
    )
    if len(image) > MAX_IMAGE_BYTES and mode == "full_page":
        mode = "viewport_fallback"
        fallback_reason = (
            f"Full-page afbeelding groter dan {MAX_IMAGE_BYTES // 1000} kB. Teruggegeven: "
            f"ALLEEN het eerste viewport ({vp['width']}×{vp['height']}), niet de volledige pagina."
        )
        image, quality = await _screenshot(page, full_page=False)
    if len(image) > MAX_IMAGE_BYTES:
        raise CaptureError("Zelfs de viewport-screenshot overschrijdt de groottelimiet.")

    if fallback_reason:
        warnings.append(fallback_reason)
    page_blocks = [b for b in proxy.blocked if not _is_browser_internal(b["target"])]
    if page_blocks:
        warnings.append(
            f"{len(page_blocks)} verzoek(en) geblokkeerd door netwerkbeveiliging; "
            "delen van de pagina kunnen ontbreken."
        )
    size = jpeg_size(image)

    return CaptureResult(
        image=image,
        metadata={
            "requested_url": url,
            "final_url": page.url,
            "http_status": status,
            "title": await page.title(),
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "device": device,
            "device_note": (
                "Mobiele emulatie in Chromium (viewport + user-agent + touch), geen test op een echt toestel."
                if device == "mobile" else "Desktop-viewport in headless Chromium."
            ),
            "viewport": {"width": vp["width"], "height": vp["height"], "device_scale_factor": 1},
            "capture_mode": mode,
            "page_height_px": page_height,
            "lazy_scroll_steps": scroll_steps,
            "image_format": "jpeg",
            "image_size_px": {"width": size[0], "height": size[1]} if size else None,
            "scale": scale if mode == "full_page" else 1.0,
            "jpeg_quality": quality,
            "image_bytes": len(image),
            "incomplete_rendering": bool(warnings),
            "warnings": warnings,
            "notes": notes,
            "blocked_requests": page_blocks,
            "browser": f"Chromium {browser_version}",
        },
    )


async def capture(url: str, device: str = "desktop", full_page: bool = True) -> CaptureResult:
    if device not in VIEWPORTS:
        raise CaptureError("device moet 'desktop' of 'mobile' zijn.")
    try:
        url = validate_input_url(url)
        await resolve_public(urlsplit(url).hostname or "")
    except BlockedURLError as e:
        raise CaptureError(f"URL geweigerd: {e}") from None

    try:
        await asyncio.wait_for(_capture_lock.acquire(), LOCK_WAIT_S)
    except asyncio.TimeoutError:
        raise CaptureError("Er loopt al een screenshot; probeer het zo opnieuw.") from None
    started = time.monotonic()
    outcome = "error"
    try:
        async with hardened_page(device) as (page, proxy, version, sandboxed):
            try:
                result = await asyncio.wait_for(
                    _do_capture(page, proxy, version, url, device, full_page), TOTAL_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                raise CaptureError(f"Capture afgebroken na {TOTAL_TIMEOUT_S}s (totale timeout).") from None
        result.metadata["browser_sandbox"] = sandboxed
        result.metadata["duration_s"] = round(time.monotonic() - started, 2)
        outcome = "ok"
        return result
    finally:
        _capture_lock.release()
        logger.info(
            "browser_screenshot %s %s device=%s in %.1fs",
            outcome, _safe_url_for_log(url), device, time.monotonic() - started,
        )
