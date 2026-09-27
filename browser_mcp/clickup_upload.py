"""Upload a browser screenshot as an attachment to an existing ClickUp task.

Security model (the MCP server itself has no inbound auth):
- Registered only when CLICKUP_UPLOAD_ENABLED=true, CLICKUP_API_TOKEN,
  SCREENSHOT_UPLOAD_KEY and CLICKUP_UPLOAD_ALLOWED_LIST_IDS are all set.
- Every call must carry the X-Screenshot-Upload-Key header of *that* HTTP request
  (checked per call, also inside an existing MCP session) before anything else.
- The task's Home List is read from ClickUp and must be in the allowlist.
- The ClickUp token and the upload key never reach Chromium (capture uses an
  allowlisted env), tool arguments, responses or logs.

Duplicate protection: a bounded on-disk registry keyed by request_id plus a tag in
the attachment filename, so a retry (even after a restart that lost the registry)
first finds an existing attachment on the task instead of uploading again.
No temporary files are written: image bytes are uploaded from memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.clickup.com/api/v2"
HEADER_NAME = "X-Screenshot-Upload-Key"
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,100}$")
REGISTRY_MAX_ENTRIES = 2000
REGISTRY_MAX_AGE_S = 180 * 24 * 3600
HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Tests inject an httpx.MockTransport here; production uses the real network.
_transport: httpx.AsyncBaseTransport | None = None


class UploadRefused(Exception):
    """Refused before capture/upload; message is safe to return."""


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def allowed_list_ids() -> set[str]:
    raw = os.environ.get("CLICKUP_UPLOAD_ALLOWED_LIST_IDS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def upload_gate() -> tuple[bool, str]:
    missing = [n for n in ("CLICKUP_API_TOKEN", "SCREENSHOT_UPLOAD_KEY")
               if not os.environ.get(n, "").strip()]
    if not _truthy("CLICKUP_UPLOAD_ENABLED"):
        return False, "CLICKUP_UPLOAD_ENABLED is niet 'true'"
    if missing:
        return False, "ontbrekende secrets: " + ", ".join(missing)
    if not allowed_list_ids():
        return False, "CLICKUP_UPLOAD_ALLOWED_LIST_IDS is leeg"
    if len(os.environ["SCREENSHOT_UPLOAD_KEY"].strip()) < 24:
        return False, "SCREENSHOT_UPLOAD_KEY is te kort (minimaal 24 tekens)"
    return True, "ingeschakeld"


def check_upload_key(request) -> None:
    """Per-call check of the secret header on the current HTTP request."""
    expected = os.environ.get("SCREENSHOT_UPLOAD_KEY", "").strip()
    if request is None or not expected:
        raise UploadRefused("Upload geweigerd: geen geldige uploadsleutel.")
    given = (request.headers.get(HEADER_NAME) or "").strip()
    if not given or not hmac.compare_digest(given.encode(), expected.encode()):
        raise UploadRefused("Upload geweigerd: geen geldige uploadsleutel.")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def fingerprint(url: str, task_id: str, device: str, full_page: bool) -> str:
    return _sha(json.dumps([url, task_id, device, bool(full_page)]))


def file_tag(request_id: str, fp: str) -> str:
    return f"r{_sha('req:' + request_id)[:10]}-{fp[:8]}"


def make_filename(url: str, device: str, captured: datetime, tag: str) -> str:
    host = re.sub(r"[^a-z0-9.-]", "", (urlsplit(url).hostname or "site").lower()) or "site"
    return f"{host}_{device}_{captured.strftime('%Y%m%dT%H%M%SZ')}_{tag}.jpg"


# ---------------------------------------------------------------- registry

def _registry_path() -> str:
    return os.environ.get("CLICKUP_UPLOAD_REGISTRY_PATH") or os.path.join(
        os.path.expanduser("~"), ".cache", "screenshot-upload", "registry.json"
    )


class Registry:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or _registry_path()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, request_id: str) -> dict | None:
        return self._load().get(request_id)

    def put(self, request_id: str, entry: dict) -> None:
        data = self._load()
        entry = {**entry, "updated": time.time()}
        data[request_id] = entry
        cutoff = time.time() - REGISTRY_MAX_AGE_S
        items = sorted(
            ((k, v) for k, v in data.items() if v.get("updated", 0) >= cutoff),
            key=lambda kv: kv[1].get("updated", 0),
        )[-REGISTRY_MAX_ENTRIES:]
        d = os.path.dirname(self.path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".reg-")  # 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(dict(items), f)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------- ClickUp API

def _client() -> httpx.AsyncClient:
    token = os.environ.get("CLICKUP_API_TOKEN", "").strip()
    return httpx.AsyncClient(
        base_url=API_BASE, timeout=HTTP_TIMEOUT, transport=_transport,
        headers={"Authorization": token, "Accept": "application/json"},
        follow_redirects=False,
    )


async def get_task(client: httpx.AsyncClient, task_id: str) -> dict:
    r = await client.get(f"/task/{task_id}")
    if r.status_code in (401, 403):
        raise UploadRefused(f"ClickUp weigert toegang tot taak {task_id} (HTTP {r.status_code}).")
    if r.status_code == 404:
        raise UploadRefused(f"ClickUp-taak {task_id} niet gevonden.")
    if r.status_code != 200:
        raise UploadRefused(f"ClickUp-taak {task_id} kon niet worden opgehaald (HTTP {r.status_code}).")
    return r.json()


def find_tagged(task: dict, request_tag: str) -> dict | None:
    """Existing attachment whose title carries this request's tag (r<10hex>-...)."""
    for a in task.get("attachments") or []:
        if f"_{request_tag}-" in str(a.get("title", "")):
            return a
    return None


def _attachment_host_ok(url: str) -> bool:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and (
        host == "clickup.com" or host.endswith(".clickup.com")
        or host.endswith(".clickup-attachments.com")
    )


async def verify_content(client: httpx.AsyncClient, url: str, expected: bytes) -> dict:
    """Downloads the attachment and compares bytes.

    First without any credentials (can a client without the token open it?), then
    with the token, which is only ever sent to ClickUp hosts.
    """
    out = {"public_without_token": None, "content": "niet gecontroleerd"}
    if not url or not _attachment_host_ok(url):
        out["content"] = "niet gecontroleerd: attachment-URL is geen ClickUp-host"
        return out
    body = None
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, transport=_transport,
                                     follow_redirects=True) as anon:
            r = await anon.get(url)
            out["public_without_token"] = r.status_code == 200
            if r.status_code == 200:
                body = r.content
            else:
                r = await client.get(url)
                if r.is_redirect and _attachment_host_ok(r.headers.get("location", "")):
                    r = await anon.get(r.headers["location"])
                if r.status_code == 200:
                    body = r.content
                else:
                    out["content"] = f"niet gecontroleerd: download HTTP {r.status_code}"
    except httpx.HTTPError as e:
        out["content"] = f"niet gecontroleerd: downloadfout ({type(e).__name__})"
    if body is not None:
        if body == expected:
            out["content"] = "identiek aan geüploade JPEG"
        elif body[:3] == b"\xff\xd8\xff":
            out["content"] = "JPEG ontvangen, maar niet byte-identiek (mogelijk door ClickUp bewerkt)"
        else:
            out["content"] = "afwijkende inhoud (geen JPEG)"
    return out


# ---------------------------------------------------------------- main flow

_in_flight: set[str] = set()


async def screenshot_to_clickup(*, request, url: str, task_id: str, device: str,
                                full_page: bool, request_id: str, capture_fn,
                                registry: Registry | None = None) -> dict:
    """Returns a result dict. Raises UploadRefused for refusals before any capture."""
    check_upload_key(request)  # always first: nothing happens without the key

    from browser_mcp.netguard import BlockedURLError, validate_input_url

    if device not in ("desktop", "mobile"):
        raise UploadRefused("device moet 'desktop' of 'mobile' zijn.")
    if not _TASK_ID_RE.match(task_id or ""):
        raise UploadRefused("Ongeldige task_id (alleen de gewone ClickUp-taak-ID).")
    if not _REQUEST_ID_RE.match(request_id or ""):
        raise UploadRefused("Ongeldige request_id (8-100 tekens: letters, cijfers, . _ : -).")
    try:
        url = validate_input_url(url)
    except BlockedURLError as e:
        raise UploadRefused(f"URL geweigerd: {e}") from None

    registry = registry or Registry()
    fp = fingerprint(url, task_id, device, full_page)
    tag = file_tag(request_id, fp)
    request_tag = tag.split("-")[0]

    if request_id in _in_flight:
        raise UploadRefused("Deze request_id is al bezig; wacht op het resultaat.")
    prior = registry.get(request_id)
    if prior and prior.get("fingerprint") != fp:
        raise UploadRefused("request_id is al gebruikt met andere invoer; kies een nieuwe request_id.")

    result: dict = {
        "request_id": request_id, "task_id": task_id, "source_url": url,
        "capture_ok": False, "upload_ok": False, "upload_verified": False,
        "content_verified": False, "deduplicated": False,
    }
    _in_flight.add(request_id)
    try:
        async with _client() as client:
            # Destination check via ClickUp itself (never trust a caller-supplied list).
            task = await get_task(client, task_id)
            list_id = str((task.get("list") or {}).get("id", ""))
            if list_id not in allowed_list_ids():
                raise UploadRefused("Taak staat niet in een toegestane ClickUp-List; upload geweigerd.")
            result["task_url"] = task.get("url")
            result["list_id"] = list_id

            # Retry protection: an earlier attempt may already have uploaded.
            existing = find_tagged(task, request_tag)
            if existing is not None:
                if not str(existing.get("title", "")).endswith(f"_{tag}.jpg"):
                    raise UploadRefused("request_id is al gebruikt met andere invoer; kies een nieuwe request_id.")
                prior_meta = (prior or {}).get("result", {})
                result.update({k: v for k, v in prior_meta.items() if k not in result})
                result.update({
                    "capture_ok": True, "upload_ok": True, "upload_verified": True,
                    "deduplicated": True,
                    "attachment_id": existing.get("id"), "attachment_url": existing.get("url"),
                    "attachment_title": existing.get("title"),
                    "note": "Bestaande bijlage voor deze request_id gevonden; niet opnieuw geüpload.",
                })
                registry.put(request_id, {"fingerprint": fp, "status": "verified", "result": result})
                return result

            if prior and prior.get("status") == "upload_uncertain":
                raise UploadRefused(
                    "Vorige upload met deze request_id had een onzekere uitkomst en de bijlage is (nog) "
                    "niet op de taak gevonden; niet opnieuw geüpload. Controleer de taak of gebruik een "
                    "nieuwe request_id.")
            if prior and prior.get("status") in ("uploaded", "verified"):
                result.update(prior.get("result") or {})
                result.update({"deduplicated": True, "upload_verified": False,
                               "content_verified": False, "note": (
                    "Deze request_id is al geüpload maar de bijlage staat niet meer op de taak; "
                    "niet opnieuw geüpload. Gebruik een nieuwe request_id.")})
                return result

            # Capture (existing hardened capture code).
            from browser_mcp.capture import CaptureError
            try:
                shot = await capture_fn(url=url, device=device, full_page=full_page)
            except CaptureError as e:
                result["error"] = f"Capture mislukt: {e}"
                registry.put(request_id, {"fingerprint": fp, "status": "capture_failed"})
                return result
            m = shot.metadata
            captured = datetime.fromisoformat(m["captured_at"]).astimezone(timezone.utc)
            filename = make_filename(m.get("final_url") or url, device, captured, tag)
            result.update({
                "capture_ok": True, "final_url": m.get("final_url"), "captured_at": m.get("captured_at"),
                "device": device, "viewport": m.get("viewport"), "capture_mode": m.get("capture_mode"),
                "image_size_px": m.get("image_size_px"), "scale": m.get("scale"),
                "downscaled": (m.get("scale") or 1.0) < 1.0, "image_bytes": len(shot.image),
                "browser_sandbox": m.get("browser_sandbox"), "warnings": m.get("warnings"),
                "filename": filename,
            })

            # Upload: mark as pending first so an interrupted attempt is checked, not repeated blindly.
            registry.put(request_id, {"fingerprint": fp, "status": "uploading", "result": result})
            uploaded: dict | None = None
            uncertain = False
            try:
                r = await client.post(
                    f"/task/{task_id}/attachment",
                    files={"attachment": (filename, shot.image, "image/jpeg")},
                )
                if r.status_code == 200:
                    uploaded = r.json()
                elif r.status_code >= 500 or r.status_code == 429:
                    uncertain = True
                    result["upload_error"] = f"ClickUp HTTP {r.status_code}"
                else:
                    result["upload_error"] = f"ClickUp HTTP {r.status_code}"
            except httpx.HTTPError as e:
                uncertain = True
                result["upload_error"] = f"netwerkfout ({type(e).__name__})"

            if uploaded is None and uncertain:
                # Unknown outcome: look at the task before anyone retries.
                try:
                    found = find_tagged(await get_task(client, task_id), request_tag)
                except (UploadRefused, httpx.HTTPError):
                    found = None
                if found is not None:
                    uploaded = found
                    result["note"] = "Uploadantwoord onzeker, maar bijlage op de taak gevonden."

            if uploaded is None:
                result["error"] = "Capture geslaagd, upload mislukt: " + result.get("upload_error", "onbekend")
                registry.put(request_id, {"fingerprint": fp,
                                          "status": "upload_uncertain" if uncertain else "upload_failed",
                                          "result": result})
                return result

            result.update({
                "upload_ok": True, "attachment_id": uploaded.get("id"),
                "attachment_url": uploaded.get("url"), "attachment_title": uploaded.get("title"),
            })
            registry.put(request_id, {"fingerprint": fp, "status": "uploaded", "result": result})

            # Verification: attachment is listed on the task, then content check.
            try:
                task_after = await get_task(client, task_id)
                ids = {a.get("id") for a in task_after.get("attachments") or []}
                result["upload_verified"] = uploaded.get("id") in ids
            except (UploadRefused, httpx.HTTPError) as e:
                result["verify_note"] = f"verificatie niet gelukt: {type(e).__name__}"
            result["content_check"] = await verify_content(client, result.get("attachment_url") or "", shot.image)
            result["content_verified"] = result["content_check"]["content"] == "identiek aan geüploade JPEG"
            registry.put(request_id, {"fingerprint": fp,
                                      "status": "verified" if result["upload_verified"] else "uploaded",
                                      "result": result})
            return result
    except httpx.HTTPError as e:
        raise UploadRefused(f"ClickUp niet bereikbaar ({type(e).__name__}); niets geüpload.") from None
    finally:
        _in_flight.discard(request_id)
