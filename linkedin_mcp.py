#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=1.2.0",
#   "httpx>=0.27.0",
#   "playwright>=1.60,<1.61",
# ]
# ///
"""LinkedIn MCP server — cookie-harvest auth via persistent Chrome profile.

Auth model:
  - LinkedIn uses httpOnly session cookies (li_at) plus CSRF derived from JSESSIONID.
  - CSRF token = JSESSIONID value after the "ajax:" prefix.
  - A 401 from Voyager triggers a headless re-harvest from the persisted
    Chrome profile (SSO cookies) before falling back to browser_login.
  - Cookies are harvested via Playwright during browser_login, then used by httpx
    for all subsequent Voyager API calls.

Token resolution chain (first valid path wins):
  1. In-process session cache (valid for SESSION_TTL seconds)
  2. Stored session file  ~/.cache/linkedin-mcp/session.json
  3. Headless Chrome silent refresh (reuses persisted SSO cookies in profile)
  4. Raise → call browser_login for one-time interactive sign-in

Covers:
  - Auth (browser_login, token_status, whoami, logout)
  - Search (people, companies, posts, jobs)
  - Profile & network (get_profile, connections)
  - Engagement (create_post, comment, react, send_message)
  - Sales Navigator (lead search, account search, lead lists, save lead, insights)
"""

import json
import os
import re
import queue as _queue
import threading
import time
import urllib.parse
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

# ── Constants ─────────────────────────────────────────────────────────────────

LI_BASE         = "https://www.linkedin.com"
VOYAGER_BASE    = f"{LI_BASE}/voyager/api"
SALES_BASE      = f"{LI_BASE}/sales-api"
LOGIN_EMAIL     = os.environ.get("LI_EMAIL", "")   # set via env var — never hardcode

CACHE_DIR       = Path.home() / ".cache" / "linkedin-mcp"
BROWSER_PROFILE = CACHE_DIR / "browser-profile"
SESSION_FILE    = CACHE_DIR / "session.json"
SESSION_TTL     = 30 * 24 * 3600  # 30 days — li_at tokens are long-lived

CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── In-process session state ──────────────────────────────────────────────────

_session: dict = {
    "cookies":      None,   # list[dict] from context.cookies()
    "csrf":         None,   # derived from JSESSIONID
    "member_id":    None,   # numeric LinkedIn member ID
    "profile_urn":  None,   # urn:li:member:{id}
    "profile_id":   None,   # public slug (e.g. 'johndoe')
    "name":         None,
    "captured_at":  0,
}

# ── Session persistence ───────────────────────────────────────────────────────

def _load_session_file() -> bool:
    if not SESSION_FILE.exists():
        return False
    try:
        data = json.loads(SESSION_FILE.read_text())
        if time.time() - data.get("captured_at", 0) > SESSION_TTL:
            return False
        _session.update(
            cookies=data["cookies"],
            csrf=data.get("csrf"),
            member_id=data.get("member_id"),
            profile_urn=data.get("profile_urn"),
            profile_id=data.get("profile_id"),
            name=data.get("name"),
            captured_at=data["captured_at"],
        )
        return bool(_session["cookies"] and _session["csrf"])
    except Exception:
        return False


def _save_session_file() -> None:
    SESSION_FILE.write_text(json.dumps({
        "cookies":      _session["cookies"],
        "csrf":         _session["csrf"],
        "member_id":    _session["member_id"],
        "profile_urn":  _session["profile_urn"],
        "profile_id":   _session["profile_id"],
        "name":         _session["name"],
        "captured_at":  _session["captured_at"],
    }, indent=2))
    SESSION_FILE.chmod(0o600)


def _session_valid() -> bool:
    return bool(
        _session["cookies"]
        and _session["csrf"]
        and time.time() - _session["captured_at"] < SESSION_TTL
    )

# ── CSRF extraction ───────────────────────────────────────────────────────────

def _derive_csrf(cookies_list: list[dict]) -> str:
    """Extract CSRF token from JSESSIONID cookie (format: 'ajax:XXXXXXXXX')."""
    cookies_dict = {c["name"]: c["value"] for c in cookies_list}
    jsessionid = cookies_dict.get("JSESSIONID", "")
    if "ajax:" in jsessionid:
        return jsessionid.split("ajax:")[-1]
    return jsessionid

# ── Browser harvest ───────────────────────────────────────────────────────────

def _harvest_session(headless: bool, timeout_sec: int) -> bool:
    """Drive Chrome to LinkedIn and capture session cookies + CSRF.

    headless=False — interactive; pre-fills LOGIN_EMAIL, waits for login.
    headless=True  — silent refresh via persisted SSO cookies in BROWSER_PROFILE.

    CSRF is captured from an intercepted Voyager API request header (same pattern
    as Highspot MCP) because deriving it from JSESSIONID is unreliable across
    LinkedIn's auth variants. Falls back to JSESSIONID derivation if no Voyager
    request is seen within the timeout.
    """
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    out: _queue.Queue = _queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            import asyncio
            asyncio.set_event_loop(None)
        except Exception:
            pass

        try:
            from playwright.sync_api import sync_playwright

            captured: dict = {"cookies": None, "csrf": None, "done": False}

            def _on_request(req) -> None:
                if captured["csrf"]:
                    return
                if "/voyager/api/" not in req.url:
                    return
                h = req.headers or {}
                csrf = h.get("csrf-token")
                if csrf:
                    captured["csrf"] = csrf

            with sync_playwright() as p:
                browser = p.chromium.launch_persistent_context(
                    user_data_dir=str(BROWSER_PROFILE),
                    headless=headless,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                    ignore_https_errors=True,
                    viewport={"width": 1280, "height": 900},
                )
                browser.on("request", _on_request)

                page = browser.new_page()
                deadline = time.time() + timeout_sec

                if headless:
                    page.goto(f"{LI_BASE}/feed/", wait_until="domcontentloaded", timeout=30_000)
                    # Wait briefly for Voyager requests to fire
                    wait_end = time.time() + 8
                    while not captured["csrf"] and time.time() < wait_end:
                        time.sleep(0.5)
                    if "/login" in page.url or "/checkpoint" in page.url:
                        browser.close()
                        out.put(False)
                        return
                    captured["cookies"] = browser.cookies()
                    captured["done"] = True
                else:
                    page.goto(f"{LI_BASE}/login", wait_until="domcontentloaded", timeout=30_000)
                    try:
                        page.fill("#username", LOGIN_EMAIL, timeout=5_000)
                    except Exception:
                        pass

                    while time.time() < deadline:
                        url = page.url
                        if "/feed" in url or url == f"{LI_BASE}/" or url == f"{LI_BASE}":
                            # Wait for Voyager requests to start firing after feed load
                            wait_end = time.time() + 10
                            while not captured["csrf"] and time.time() < wait_end:
                                time.sleep(0.5)
                            captured["cookies"] = browser.cookies()
                            captured["done"] = True
                            break
                        time.sleep(0.8)

                browser.close()

            if captured["done"] and captured["cookies"]:
                names = {c["name"] for c in captured["cookies"]}
                if "li_at" not in names:
                    out.put(False)
                    return

                # Prefer intercepted CSRF; fall back to JSESSIONID derivation
                csrf = captured["csrf"] or _derive_csrf(captured["cookies"])
                _session.update(
                    cookies=captured["cookies"],
                    csrf=csrf,
                    captured_at=time.time(),
                )
                out.put(True)
            else:
                out.put(False)

        except Exception as e:
            out.put(RuntimeError(str(e)))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    try:
        result = out.get(timeout=timeout_sec + 10)
    except _queue.Empty:
        return False

    if isinstance(result, Exception):
        raise result
    return bool(result)


def _ensure_session() -> None:
    """Load session from cache or attempt headless refresh. Raise if unavailable."""
    if _session_valid():
        return
    if _load_session_file():
        return
    if _harvest_session(headless=True, timeout_sec=25):
        _save_session_file()
        return
    raise RuntimeError(
        "LinkedIn session missing or expired — call browser_login to authenticate."
    )


def _refresh_session() -> bool:
    """Force a headless re-harvest from the persisted Chrome profile.

    Called when LinkedIn answers 401 even though the cached session is inside
    its TTL (li_at rotated / invalidated server-side). Drops the stale cache
    first so _ensure_session cannot reload it, then re-mints via the SSO
    cookies in BROWSER_PROFILE. Returns True when a fresh session was saved.
    """
    _session.update(cookies=None, csrf=None, captured_at=0)
    try:
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
    except OSError:
        pass
    try:
        ok = _harvest_session(headless=True, timeout_sec=30)
    except Exception:
        ok = False
    if ok:
        _save_session_file()
    return ok


def _live_check(timeout: int = 15) -> bool:
    """True when LinkedIn currently accepts the session (GET /me == 200)."""
    try:
        with _make_client(timeout) as client:
            return client.get(f"{VOYAGER_BASE}/me").status_code == 200
    except Exception:
        return False

# ── HTTP client ───────────────────────────────────────────────────────────────

def _headers() -> dict:
    """Standard Voyager request headers for the current session."""
    return {
        "csrf-token":                   _session["csrf"] or "",
        "x-restli-protocol-version":    "2.0.0",
        "x-li-lang":                    "en_US",
        "x-li-track":                   '{"clientVersion":"1.13.3","osName":"web_ios","timezoneOffset":-7,"timezone":"America/Los_Angeles","deviceFormFactor":"DESKTOP","mpName":"voyager-web"}',
        "x-li-page-instance":           "urn:li:page:d_flagship3_feed;",
        "accept":                       "application/vnd.linkedin.normalized+json+2.1",
        "accept-language":              "en-US,en;q=0.9",
        "sec-fetch-dest":               "empty",
        "sec-fetch-mode":               "cors",
        "sec-fetch-site":               "same-origin",
        "user-agent":                   (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "referer":                      "https://www.linkedin.com/feed/",
        "origin":                       "https://www.linkedin.com",
    }


def _cookies_dict() -> dict:
    return {c["name"]: c["value"] for c in (_session["cookies"] or [])}


def _make_client(timeout: int = 20) -> httpx.Client:
    """Build a stateful httpx.Client with all LinkedIn cookies loaded into its jar.

    Using Client (not bare httpx.get) is critical: when LinkedIn returns a 302
    with Set-Cookie headers, the Client persists those cookies for the follow-up
    request, exactly like a browser would.
    """
    client = httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers=_headers(),
    )
    for c in (_session["cookies"] or []):
        client.cookies.set(
            c["name"], c["value"],
            domain=c.get("domain", ".linkedin.com"),
            path=c.get("path", "/"),
        )
    return client


def _check_auth_redirect(resp: httpx.Response) -> None:
    final = str(resp.url)
    if "/login" in final or "/checkpoint" in final or "/authwall" in final:
        _session.update(cookies=None, csrf=None, captured_at=0)
        raise RuntimeError(
            "LinkedIn session rejected (redirected to login). Call browser_login to re-authenticate."
        )
    if resp.status_code == 429:
        raise RuntimeError(
            "LinkedIn rate limit hit (429). Wait a few minutes before retrying."
        )


def _get(url: str, params: dict | None = None, timeout: int = 20) -> dict:
    _ensure_session()
    try:
        with _make_client(timeout) as client:
            resp = client.get(url, params=params)
    except httpx.TooManyRedirects:
        _session.update(cookies=None, csrf=None, captured_at=0)
        raise RuntimeError(
            "LinkedIn rejected the session (redirect loop). Call browser_login to re-authenticate."
        )
    _check_auth_redirect(resp)
    if resp.status_code == 401:
        if not _refresh_session():
            raise RuntimeError(
                "LinkedIn rejected the session (401) and headless refresh failed — "
                "call browser_login to re-authenticate."
            )
        with _make_client(timeout) as client:
            resp = client.get(url, params=params)
        _check_auth_redirect(resp)
    resp.raise_for_status()
    return resp.json()


def _post(url: str, body: dict, extra_headers: dict | None = None, timeout: int = 20) -> dict | None:
    _ensure_session()
    try:
        with _make_client(timeout) as client:
            if extra_headers:
                client.headers.update(extra_headers)
            client.headers["content-type"] = "application/json"
            resp = client.post(url, json=body)
    except httpx.TooManyRedirects:
        _session.update(cookies=None, csrf=None, captured_at=0)
        raise RuntimeError(
            "LinkedIn rejected the session (redirect loop). Call browser_login to re-authenticate."
        )
    _check_auth_redirect(resp)
    if resp.status_code == 401:
        if not _refresh_session():
            raise RuntimeError(
                "LinkedIn rejected the session (401) and headless refresh failed — "
                "call browser_login to re-authenticate."
            )
        with _make_client(timeout) as client:
            if extra_headers:
                client.headers.update(extra_headers)
            client.headers["content-type"] = "application/json"
            resp = client.post(url, json=body)
        _check_auth_redirect(resp)
    resp.raise_for_status()
    return resp.json() if resp.content else None

# ── Response helpers ──────────────────────────────────────────────────────────

def _extract_text(obj: dict | str | None) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    return obj.get("text", "") or obj.get("$recipeTypes", [""])[0] or str(obj)


def _profile_from_hit(hit: dict) -> dict:
    """Extract a clean profile summary from a Voyager search hit."""
    target = hit.get("targetUrn", "")
    data   = hit.get("*dashEntityUrn", hit.get("title", {}))
    title  = _extract_text(hit.get("headline", hit.get("title", {})))
    sub    = _extract_text(hit.get("subline", hit.get("primarySubtitle", {})))
    loc    = _extract_text(hit.get("secondarySubtitle", {}))
    return {
        "urn":        target or hit.get("entityUrn", ""),
        "name":       _extract_text(hit.get("title", {})),
        "headline":   title,
        "company":    sub,
        "location":   loc,
        "profile_url": f"https://www.linkedin.com/in/{hit.get('publicIdentifier', '')}",
    }

# ── FastMCP server ────────────────────────────────────────────────────────────

mcp = FastMCP("linkedin")

# ── Auth tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def browser_login() -> dict:
    """Open a Chrome browser for interactive LinkedIn login.

    Pre-fills the LI_EMAIL env var if set. After you complete password/MFA,
    the session cookies are harvested and saved to ~/.cache/linkedin-mcp/session.json.
    The browser closes automatically once login is detected.

    Returns { success, name, member_id } on success.
    """
    if not _harvest_session(headless=False, timeout_sec=300):
        return {"success": False, "error": "Login not detected within timeout."}

    # Fetch profile to get member info
    try:
        me = _get(f"{VOYAGER_BASE}/me")
        mini = me.get("miniProfile", {})
        member_id  = mini.get("objectUrn", "").split(":")[-1]
        profile_id = mini.get("publicIdentifier", "")
        name       = f"{mini.get('firstName', '')} {mini.get('lastName', '')}".strip()
        _session["member_id"]   = member_id
        _session["profile_urn"] = f"urn:li:member:{member_id}"
        _session["profile_id"]  = profile_id
        _session["name"]        = name
    except Exception:
        member_id  = ""
        profile_id = ""
        name       = ""

    _save_session_file()
    return {
        "success":    True,
        "name":       name or _session.get("name", ""),
        "member_id":  member_id or _session.get("member_id", ""),
        "profile_id": profile_id or _session.get("profile_id", ""),
        "expires_in_days": int((SESSION_TTL - (time.time() - _session["captured_at"])) / 86400),
    }


@mcp.tool()
def token_status() -> dict:
    """Check whether the current LinkedIn session is valid.

    Returns { valid, name, member_id, expires_in_seconds }.
    valid=false means you should call browser_login.
    """
    loaded = _session_valid() or _load_session_file()
    if not loaded:
        try:
            loaded = _harvest_session(headless=True, timeout_sec=25)
            if loaded:
                _save_session_file()
        except Exception:
            loaded = False

    if not loaded:
        return {"valid": False, "error": "No valid session. Call browser_login."}

    # TTL says valid — confirm LinkedIn agrees, and headless-refresh if not.
    refreshed = False
    live = _live_check()
    if not live:
        refreshed = _refresh_session()
        live = refreshed and _live_check()
    if not live:
        return {
            "valid":     False,
            "refreshed": refreshed,
            "error":     "LinkedIn rejected the cached session and headless refresh failed. Call browser_login.",
        }

    return {
        "valid":             True,
        "live":              True,
        "refreshed":         refreshed,
        "name":              _session.get("name", ""),
        "member_id":         _session.get("member_id", ""),
        "expires_in_seconds": max(0, int(SESSION_TTL - (time.time() - _session["captured_at"]))),
    }


@mcp.tool()
def whoami() -> dict:
    """Get the profile of the currently authenticated LinkedIn user.

    Returns cached profile from session (populated by browser_login) plus
    a live Voyager API call if possible. Falls back to cache-only if the API
    call fails — the CSRF token can become stale after the login browser closes.
    """
    _ensure_session()

    # Try the API first; fall back to session cache on any auth error
    try:
        data = _get(f"{VOYAGER_BASE}/me")
        mini = data.get("miniProfile", data.get("included", [{}])[0] if data.get("included") else {})
        member_id = mini.get("objectUrn", "").split(":")[-1] or _session.get("member_id", "")
        pub_id    = mini.get("publicIdentifier", "")
        name      = f"{mini.get('firstName', '')} {mini.get('lastName', '')}".strip()
        if name:
            _session["name"]        = name
            _session["member_id"]   = member_id
            _session["profile_urn"] = f"urn:li:member:{member_id}"
            _save_session_file()
        return {
            "name":        name,
            "headline":    _extract_text(mini.get("occupation", mini.get("headline", ""))),
            "member_id":   member_id,
            "profile_urn": f"urn:li:member:{member_id}",
            "profile_url": f"https://www.linkedin.com/in/{pub_id}" if pub_id else "",
            "picture_url": (mini.get("picture", {}) or {}).get("rootUrl", ""),
            "source":      "api",
        }
    except Exception:
        pass

    # Cache fallback — browser_login always populates these
    member_id = _session.get("member_id", "")
    name      = _session.get("name", "")
    return {
        "name":        name,
        "headline":    "",
        "member_id":   member_id,
        "profile_urn": _session.get("profile_urn", f"urn:li:member:{member_id}"),
        "profile_url": f"https://www.linkedin.com/in/{_session.get('profile_id', '')}" if member_id else "",
        "picture_url": "",
        "source":      "session_cache",
    }


@mcp.tool()
def logout() -> dict:
    """Clear the LinkedIn session from disk and memory.

    The persistent browser profile (SSO cookies) is preserved so the next
    browser_login is low-friction. Returns { success }.
    """
    _session.update(cookies=None, csrf=None, member_id=None,
                    profile_urn=None, name=None, captured_at=0)
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
    return {"success": True}

# ── Search tools ──────────────────────────────────────────────────────────────

SEARCH_QUERY_ID = "voyagerSearchDashClusters.b0928897b71bd00a5a7291755dcd64f0"
_ENTITY_RESULT  = "com.linkedin.voyager.dash.search.EntityResultViewModel"
_PAGE           = 10   # GraphQL search pages are fixed at 10 hits


def _li_quote(text: str) -> str:
    return urllib.parse.quote(text, safe="")


def _graphql_search(keywords: str, result_type: str, filters: list[str] | None = None,
                    start: int = 0) -> tuple[int | None, list[dict]]:
    """Voyager GraphQL search (replacement for the retired /search/blended).

    result_type: PEOPLE | COMPANIES | CONTENT
    filters: extra queryParameters entries, e.g. '(key:currentCompany,value:List(165222))'
    Returns (totalResultCount, [EntityResultViewModel, ...]).
    """
    qp = [f"(key:resultType,value:List({result_type}))"] + list(filters or [])
    variables = (
        f"(start:{start},origin:FACETED_SEARCH,query:(keywords:{_li_quote(keywords)},"
        f"flagshipSearchIntent:SEARCH_SRP,queryParameters:List({','.join(qp)}),"
        f"includeFiltersInResponse:false))"
    )
    data = _get(f"{VOYAGER_BASE}/graphql?variables={variables}&queryId={SEARCH_QUERY_ID}")
    meta  = ((((data.get("data") or {}).get("data") or {}).get("searchDashClustersByAll") or {}).get("metadata") or {})
    total = meta.get("totalResultCount")
    hits  = [x for x in data.get("included", []) if x.get("$type") == _ENTITY_RESULT]
    return total, hits


def _paged_search(keywords: str, result_type: str, filters: list[str] | None,
                  count: int, start: int) -> tuple[int | None, list[dict]]:
    count = max(1, min(count, 49))
    hits: list[dict] = []
    total = None
    offset = start
    while len(hits) < count:
        total, page = _graphql_search(keywords, result_type, filters, offset)
        if not page:
            break
        hits.extend(page)
        offset += _PAGE
        if total is not None and offset >= total:
            break
    return total, hits[:count]


def _urn_id(urn: str, kind: str) -> str:
    m = re.search(rf"{kind}:([^,)]+)", urn or "")
    return m.group(1) if m else ""


def _company_filter(key: str, names: str) -> str | None:
    """Build a company facet from a comma-separated list of names or numeric IDs."""
    ids = []
    for raw in (n.strip() for n in names.split(",")):
        if not raw:
            continue
        ids.append(raw if raw.isdigit() else _resolve_company_id(raw))
    return f"(key:{key},value:List({','.join(ids)}))" if ids else None


def _resolve_company_id(name: str) -> str:
    """Resolve a company name to its numeric LinkedIn company ID (first search hit)."""
    _, hits = _graphql_search(name, "COMPANIES")
    for h in hits:
        cid = _urn_id(h.get("entityUrn", ""), "fsd_company")
        if cid:
            return cid
    raise RuntimeError(f"Could not resolve LinkedIn company '{name}' — pass the numeric company ID instead.")


def _person_from_hit(h: dict) -> dict:
    return {
        "urn":         f"urn:li:fsd_profile:{_urn_id(h.get('entityUrn', ''), 'fsd_profile')}",
        "name":        _extract_text(h.get("title")),
        "headline":    _extract_text(h.get("primarySubtitle")),
        "location":    _extract_text(h.get("secondarySubtitle")),
        "summary":     _extract_text(h.get("summary")),
        "profile_url": (h.get("navigationUrl") or "").split("?")[0],
    }


@mcp.tool()
def search_people(
    keywords: str,
    count: int = 10,
    start: int = 0,
    title_filter: str = "",
    company_filter: str = "",
    past_company_filter: str = "",
    location_filter: str = "",
) -> dict:
    """Search LinkedIn for people by keywords.

    Args:
        keywords: Search keywords (name, title, skills, free text matched against the whole profile)
        count: Number of results (max 49)
        start: Pagination offset (multiples of 10)
        title_filter: Current job-title keywords (LinkedIn's title facet)
        company_filter: Current company — name(s) or numeric LinkedIn company ID(s), comma-separated
        past_company_filter: Past company — name(s) or numeric ID(s), comma-separated
        location_filter: Location words (folded into keywords; LinkedIn's geo facet needs IDs)

    Returns { total, count, start, results[] } with name, headline, location, summary, profile_url, urn.
    """
    filters: list[str] = []
    if title_filter:
        filters.append(f"(key:title,value:List({_li_quote(title_filter)}))")
    for key, val in (("currentCompany", company_filter), ("pastCompany", past_company_filter)):
        f = _company_filter(key, val) if val else None
        if f:
            filters.append(f)
    kw = f"{keywords} {location_filter}".strip() if location_filter else keywords

    total, hits = _paged_search(kw, "PEOPLE", filters, count, start)
    results = [_person_from_hit(h) for h in hits]
    return {"total": total, "count": len(results), "start": start, "results": results}


@mcp.tool()
def search_companies(
    keywords: str,
    count: int = 10,
    start: int = 0,
    industry_filter: str = "",
) -> dict:
    """Search LinkedIn for companies.

    Args:
        keywords: Company name or description keywords
        count: Number of results (max 49)
        start: Pagination offset
        industry_filter: Industry words (folded into keywords)

    Returns list of companies with company_id, name, industry_location, followers, url.
    """
    kw = f"{keywords} {industry_filter}".strip() if industry_filter else keywords
    total, hits = _paged_search(kw, "COMPANIES", None, count, start)
    results = []
    for h in hits:
        results.append({
            "company_id":        _urn_id(h.get("entityUrn", ""), "fsd_company"),
            "name":              _extract_text(h.get("title")),
            "industry_location": _extract_text(h.get("primarySubtitle")),
            "followers":         _extract_text(h.get("secondarySubtitle")),
            "description":       _extract_text(h.get("summary")),
            "url":               (h.get("navigationUrl") or "").split("?")[0],
        })
    return {"total": total, "count": len(results), "start": start, "results": results}


@mcp.tool()
def search_posts(
    keywords: str,
    count: int = 10,
    start: int = 0,
    date_range: str = "past_week",
) -> dict:
    """Search LinkedIn posts and content.

    Args:
        keywords: Keywords to search for in posts
        count: Number of results (max 49)
        start: Pagination offset
        date_range: One of 'past_24h', 'past_week', 'past_month', 'any_time'

    Returns list of posts with author, subtitle, description, url.
    """
    date_map = {"past_24h": "past-24h", "past_week": "past-week", "past_month": "past-month"}
    filters = []
    if date_map.get(date_range):
        filters.append(f"(key:datePosted,value:List({date_map[date_range]}))")
    total, hits = _paged_search(keywords, "CONTENT", filters, count, start)
    results = []
    for h in hits:
        results.append({
            "urn":         h.get("entityUrn", ""),
            "author":      _extract_text(h.get("title")),
            "subtitle":    _extract_text(h.get("primarySubtitle")),
            "description": _extract_text(h.get("summary")),
            "url":         (h.get("navigationUrl") or "").split("?")[0],
        })
    return {"total": total, "count": len(results), "start": start, "results": results}


@mcp.tool()
def search_jobs(
    keywords: str,
    location: str = "",
    count: int = 10,
    start: int = 0,
) -> dict:
    """Search LinkedIn job postings.

    Args:
        keywords: Job title, skills, or company keywords
        location: City, state, or country
        count: Number of results (max 49)
        start: Pagination offset

    Returns list of jobs with title, company, location, url.
    """
    data = _get(f"{VOYAGER_BASE}/jobsDashJobCards", params={
        "decorationId": "com.linkedin.voyager.dash.deco.jobs.norelay.JobPostingCard-102",
        "count":        min(count, 49),
        "q":            "jobSearch",
        "query":        f"(origin:JOB_SEARCH_PAGE_JOB_FILTER,keywords:{urllib.parse.quote(keywords)},locationUnion:(freeText:{urllib.parse.quote(location)}),spellCorrectionEnabled:true)",
        "start":        start,
    })

    results = []
    for item in data.get("elements", []):
        job = item.get("jobPosting", item)
        results.append({
            "title":         job.get("title", ""),
            "company":       _extract_text(job.get("primaryDescription", {})),
            "location":      _extract_text(job.get("secondaryDescription", {})),
            "listed_at":     job.get("listedAt", ""),
            "url":           f"https://www.linkedin.com/jobs/view/{job.get('jobPostingId', '')}",
        })

    return {"count": len(results), "start": start, "results": results}

# ── Profile & network tools ───────────────────────────────────────────────────

@mcp.tool()
def get_profile(profile_id: str) -> dict:
    """Get a LinkedIn profile by public ID (slug) or full profile URL.

    Args:
        profile_id: LinkedIn public ID (e.g. 'johndoe') or URL
                    (e.g. 'https://www.linkedin.com/in/johndoe/')

    Returns full profile: name, headline, summary, location, experience (all
    positions, newest first), education, skills. Uses the dash profile endpoint
    (identity/profiles/{id}/profileView was retired — 410 Gone).
    """
    if "linkedin.com/in/" in profile_id:
        profile_id = profile_id.rstrip("/").split("/in/")[-1].split("/")[0].split("?")[0]

    data = _get(
        f"{VOYAGER_BASE}/identity/dash/profiles",
        params={
            "q":            "memberIdentity",
            "memberIdentity": profile_id,
            "decorationId": "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-101",
        },
    )
    inc = data.get("included", [])
    T = "com.linkedin.voyager.dash.identity.profile."
    profiles = [x for x in inc if x.get("$type") == T + "Profile"]
    prof = next((x for x in profiles if (x.get("publicIdentifier") or "").lower() == profile_id.lower()),
                profiles[0] if profiles else {})

    def _date(d: dict | None) -> dict:
        d = d or {}
        return {k: d[k] for k in ("year", "month") if k in d}

    def _sort_key(x: dict):
        st = ((x.get("dateRange") or {}).get("start") or {})
        return (st.get("year", 0), st.get("month", 0))

    experience = []
    for pos in sorted((x for x in inc if x.get("$type") == T + "Position"), key=_sort_key, reverse=True):
        dr = pos.get("dateRange") or {}
        experience.append({
            "title":       pos.get("title", ""),
            "company":     pos.get("companyName", ""),
            "location":    pos.get("locationName", ""),
            "start":       _date(dr.get("start")),
            "end":         _date(dr.get("end")),      # {} == current role
            "description": pos.get("description", "") or "",
        })

    edu = []
    for e in sorted((x for x in inc if x.get("$type") == T + "Education"), key=_sort_key, reverse=True):
        edu.append({
            "school": e.get("schoolName", ""),
            "degree": e.get("degreeName", ""),
            "field":  e.get("fieldOfStudy", ""),
            "end":    _date((e.get("dateRange") or {}).get("end")),
        })

    skills = [x.get("name", "") for x in inc if x.get("$type") == T + "Skill"]
    geo = prof.get("geoLocation") or {}

    return {
        "name":         f"{prof.get('firstName', '')} {prof.get('lastName', '')}".strip(),
        "headline":     prof.get("headline", ""),
        "summary":      prof.get("summary", "") or "",
        "location":     prof.get("locationName") or geo.get("geo", {}).get("defaultLocalizedName", "") if isinstance(geo.get("geo"), dict) else prof.get("locationName", ""),
        "profile_url":  f"https://www.linkedin.com/in/{prof.get('publicIdentifier', profile_id)}",
        "urn":          prof.get("entityUrn", ""),
        "experience":   experience,
        "education":    edu,
        "skills":       skills[:25],
    }


def _member_urn(profile_id: str) -> str:
    """Resolve a public slug (or /in/ URL) to urn:li:member:{id} via the dash profile endpoint."""
    if "linkedin.com/in/" in profile_id:
        profile_id = profile_id.rstrip("/").split("/in/")[-1].split("/")[0].split("?")[0]
    data = _get(
        f"{VOYAGER_BASE}/identity/dash/profiles",
        params={"q": "memberIdentity", "memberIdentity": profile_id,
                "decorationId": "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-101"},
    )
    for x in data.get("included", []):
        if x.get("$type") == "com.linkedin.voyager.dash.identity.profile.Profile" and x.get("objectUrn"):
            if (x.get("publicIdentifier") or "").lower() == profile_id.lower():
                return x["objectUrn"]
    for x in data.get("included", []):
        if x.get("$type") == "com.linkedin.voyager.dash.identity.profile.Profile" and x.get("objectUrn"):
            return x["objectUrn"]
    raise RuntimeError(f"Could not resolve LinkedIn member URN for '{profile_id}'")


@mcp.tool()
def get_connections(count: int = 40, start: int = 0, keyword_filter: str = "") -> dict:
    """Get your LinkedIn 1st-degree connections.

    Args:
        count: Number of connections to return (max 100)
        start: Pagination offset
        keyword_filter: Optional name/company filter

    Returns list of connections with name, headline, company, connected_at.
    """
    params: dict = {
        "count":        min(count, 100),
        "decorationId": "com.linkedin.voyager.dash.deco.identity.connectionspage.ConnectionCard-4",
        "q":            "search",
        "start":        start,
    }
    if keyword_filter:
        params["keyword"] = keyword_filter

    data = _get(f"{VOYAGER_BASE}/relationships/dash/connections", params=params)

    results = []
    for item in data.get("elements", []):
        conn = item.get("connectedMember", item)
        mini = conn.get("miniProfile", conn)
        results.append({
            "name":         f"{mini.get('firstName', '')} {mini.get('lastName', '')}".strip(),
            "headline":     _extract_text(mini.get("occupation", "")),
            "profile_id":   mini.get("publicIdentifier", ""),
            "profile_url":  f"https://www.linkedin.com/in/{mini.get('publicIdentifier', '')}",
            "connected_at": item.get("connectedAt", ""),
        })

    return {
        "total":   data.get("paging", {}).get("total", len(results)),
        "count":   len(results),
        "start":   start,
        "results": results,
    }

# ── Engagement tools ──────────────────────────────────────────────────────────

@mcp.tool()
def create_post(
    text: str,
    visibility: str = "PUBLIC",
    article_url: str = "",
    article_title: str = "",
    article_description: str = "",
) -> dict:
    """Create a post on LinkedIn on your behalf.

    Args:
        text: The post text content (supports Unicode, line breaks)
        visibility: 'PUBLIC' (everyone), 'CONNECTIONS' (1st degree), or 'LOGGED_IN' (LinkedIn members)
        article_url: Optional URL to attach as an article/link preview
        article_title: Title for the link preview (required if article_url set)
        article_description: Description for the link preview

    Returns { success, post_id, post_url }.
    """
    _ensure_session()
    member_id = _session.get("member_id")
    if not member_id:
        me = _get(f"{VOYAGER_BASE}/me")
        mini = me.get("miniProfile", {})
        member_id = mini.get("objectUrn", "").split(":")[-1]
        _session["member_id"]   = member_id
        _session["profile_urn"] = f"urn:li:member:{member_id}"

    vis_map = {
        "PUBLIC":      "PUBLIC",
        "CONNECTIONS": "CONNECTIONS_ONLY",
        "LOGGED_IN":   "LOGGED_IN",
    }
    vis = vis_map.get(visibility.upper(), "PUBLIC")

    if article_url:
        share_media_category = "ARTICLE"
        media = [{
            "status": "READY",
            "originalUrl": article_url,
            "title":       {"text": article_title or article_url},
            "description": {"text": article_description or ""},
        }]
    else:
        share_media_category = "NONE"
        media = []

    body: dict = {
        "author":         f"urn:li:member:{member_id}",
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary":   {"text": text},
                "shareMediaCategory": share_media_category,
                **({"media": media} if media else {}),
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": vis
        },
    }

    data = _post(f"{VOYAGER_BASE}/ugcPosts", body)
    post_id = (data or {}).get("id", "") if data else ""

    return {
        "success":  True,
        "post_id":  post_id,
        "post_url": f"https://www.linkedin.com/feed/update/{post_id}" if post_id else "",
    }


def _playwright_get_feed(count: int, scroll_pages: int) -> list[dict]:
    """Load the LinkedIn feed page in headless Chrome and extract posts via DOM.

    LinkedIn's feed is now server-side rendered + React hydrated; the old
    /voyager/api/feed/updatesV2 REST endpoint returns 400. Playwright is the
    only reliable approach.
    """
    result_q: _queue.Queue = _queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            import asyncio
            asyncio.set_event_loop(None)
        except Exception:
            pass
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=str(BROWSER_PROFILE),
                    headless=True,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
                page = ctx.new_page()
                page.goto(f"{LI_BASE}/feed/", wait_until="domcontentloaded", timeout=30_000)
                time.sleep(6)

                # Scroll to load more posts
                for _ in range(scroll_pages):
                    page.evaluate("window.scrollBy(0, 1400)")
                    time.sleep(2)

                posts = page.evaluate("""
                    () => {
                        const section = document.querySelector('main section');
                        const wrapper = section && section.children[0];
                        if (!wrapper) return [];
                        const results = [];
                        for (const div of wrapper.children) {
                            const text = (div.innerText || '').trim();
                            if (!text.startsWith('Feed post') || text.length < 80) continue;
                            const body = text.replace(/^Feed post\\s*/, '');
                            const lines = body.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
                            const author = lines[0] || '';
                            const degreeMatch = (lines[1] || '').match(/•\\s*(1st|2nd|3rd\\+?)/);
                            const degree = degreeMatch ? degreeMatch[1] : '';
                            const headlineIdx = degree ? 2 : 1;
                            const headline = lines[headlineIdx] || '';
                            const timeMatch = body.match(/(\\d+[mhdw]|\\d+\\s+(?:hour|day|week|minute|month)s?\\s+ago|Just now)/i);
                            const posted_time = timeMatch ? timeMatch[0].trim() : '';
                            const postText = lines.slice(headlineIdx + 2).join(' ');
                            const urnEl = div.querySelector('[data-urn]');
                            const urn = urnEl ? urnEl.dataset.urn : '';
                            const reactMatch = body.match(/(\\d[,\\d]*)\\s+(?:reaction|like)/i);
                            const commentMatch = body.match(/(\\d[,\\d]*)\\s+comment/i);
                            results.push({
                                urn, author, degree, headline,
                                posted_time,
                                text: postText.substring(0, 500),
                                reactions: reactMatch ? reactMatch[1] : '0',
                                comments: commentMatch ? commentMatch[1] : '0',
                            });
                        }
                        return results;
                    }
                """)
                ctx.close()
                result_q.put(posts)
        except Exception as e:
            result_q.put(RuntimeError(str(e)))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    result = result_q.get(timeout=90)
    if isinstance(result, Exception):
        raise result
    return result


@mcp.tool()
def get_feed(count: int = 10, start: int = 0) -> dict:
    """Get your LinkedIn feed (recent posts from your network).

    Uses headless Chrome to load the feed page — takes ~10s but is reliable
    since LinkedIn's feed is now server-side rendered (REST endpoint deprecated).

    Args:
        count: Number of posts to return
        start: Pagination offset (scrolls to load more before slicing)

    Returns list of feed items with author, headline, degree, text, posted_time, reactions, comments.
    """
    scroll_pages = max(1, (start + count) // 6)
    posts = _playwright_get_feed(count=count + start, scroll_pages=scroll_pages)
    sliced = posts[start: start + count]
    return {"count": len(sliced), "start": start, "results": sliced}


@mcp.tool()
def react_to_post(post_urn: str, reaction: str = "LIKE") -> dict:
    """React to a LinkedIn post.

    Args:
        post_urn: The post URN (e.g. 'urn:li:activity:1234567890')
        reaction: One of 'LIKE', 'CELEBRATE', 'SUPPORT', 'FUNNY', 'LOVE', 'INSIGHTFUL'

    Returns { success }.
    """
    _ensure_session()
    member_id = _session.get("member_id", "")
    reaction_map = {
        "LIKE":       "LIKE",
        "CELEBRATE":  "PRAISE",
        "SUPPORT":    "EMPATHY",
        "FUNNY":      "ENTERTAINMENT",
        "LOVE":       "APPRECIATION",
        "INSIGHTFUL": "INTEREST",
    }
    reaction_type = reaction_map.get(reaction.upper(), "LIKE")

    body = {
        "actor":        f"urn:li:member:{member_id}",
        "object":       post_urn,
        "reactionType": reaction_type,
    }

    _post(f"{VOYAGER_BASE}/reactions", body)
    return {"success": True, "reaction": reaction_type, "post_urn": post_urn}


@mcp.tool()
def comment_on_post(post_urn: str, text: str) -> dict:
    """Add a comment to a LinkedIn post.

    Args:
        post_urn: The post URN (e.g. 'urn:li:activity:1234567890')
        text: Comment text

    Returns { success, comment_id }.
    """
    _ensure_session()
    member_id = _session.get("member_id", "")

    body = {
        "actor":   f"urn:li:member:{member_id}",
        "object":  post_urn,
        "message": {"text": text},
    }

    data = _post(f"{VOYAGER_BASE}/socialActions/{urllib.parse.quote(post_urn, safe='')}/comments", body)
    comment_id = (data or {}).get("id", "") if data else ""

    return {"success": True, "comment_id": comment_id}


@mcp.tool()
def send_message(
    recipient_profile_id: str,
    message_text: str,
    subject: str = "",
) -> dict:
    """Send a LinkedIn message or InMail to a connection.

    Args:
        recipient_profile_id: LinkedIn public ID or URN of the recipient
        message_text: Message body text
        subject: Optional message subject (for InMail)

    Returns { success, conversation_id }.
    """
    _ensure_session()

    # Resolve to URN if public ID given
    if not recipient_profile_id.startswith("urn:"):
        recipient_urn = _member_urn(recipient_profile_id)
    else:
        recipient_urn = recipient_profile_id

    body: dict = {
        "keyVersion": "LEGACY_INBOX",
        "conversationCreate": {
            "eventCreate": {
                "value": {
                    "com.linkedin.voyager.messaging.create.MessageCreate": {
                        "attributedBody": {
                            "text":       message_text,
                            "attributes": [],
                        },
                        "attachments": [],
                    }
                }
            },
            "recipients": [recipient_urn],
            "subtype":    "MEMBER_TO_MEMBER",
            **({"subject": subject} if subject else {}),
        },
    }

    data = _post(f"{VOYAGER_BASE}/messaging/conversations", body)
    conv_id = (data or {}).get("id", "") if data else ""

    return {"success": True, "conversation_id": conv_id}


@mcp.tool()
def get_messages(count: int = 20, start: int = 0) -> dict:
    """Get recent LinkedIn message conversations.

    Returns list of conversations with participant name, last message snippet, url.
    """
    data = _get(f"{VOYAGER_BASE}/messaging/conversations", params={
        "keyVersion": "LEGACY_INBOX",
        "count":      min(count, 40),
        "start":      start,
    })

    results = []
    for conv in data.get("elements", []):
        participants = []
        for p in conv.get("participants", [])[:3]:
            member = p.get("com.linkedin.voyager.messaging.MessagingMember", {})
            mini   = member.get("miniProfile", {})
            participants.append(
                f"{mini.get('firstName', '')} {mini.get('lastName', '')}".strip()
            )
        last_activity = conv.get("lastActivityAt", 0)
        results.append({
            "conversation_id":   conv.get("entityUrn", ""),
            "participants":      participants,
            "unread":            conv.get("unreadCount", 0),
            "last_message_at":   last_activity,
            "url":               f"https://www.linkedin.com/messaging/thread/{conv.get('dashEntityUrn', '').split(':')[-1]}/",
        })

    return {"count": len(results), "start": start, "results": results}

# ── Sales Navigator tools ─────────────────────────────────────────────────────

def _sn_get(path: str, params: dict | None = None) -> dict:
    """GET request to the Sales Navigator API endpoint."""
    _ensure_session()
    h = {
        **_headers(),
        "x-li-page-instance":  "urn:li:page:sales_homepage;",
        "x-li-track":          '{"clientVersion":"1.2.2","osName":"web_ios"}',
        "referer":             "https://www.linkedin.com/sales/",
    }
    resp = httpx.get(
        f"{SALES_BASE}{path}",
        params=params,
        cookies=_cookies_dict(),
        headers=h,
        timeout=30,
    )
    if resp.status_code in (403, 404):
        return {"error": "Sales Navigator not available. Requires an active Sales Navigator subscription.", "status": resp.status_code}
    resp.raise_for_status()
    return resp.json()


def _sn_post(path: str, body: dict) -> dict | None:
    """POST request to the Sales Navigator API endpoint."""
    _ensure_session()
    h = {
        **_headers(),
        "content-type":        "application/json",
        "x-li-page-instance":  "urn:li:page:sales_homepage;",
        "referer":             "https://www.linkedin.com/sales/",
    }
    resp = httpx.post(
        f"{SALES_BASE}{path}",
        json=body,
        cookies=_cookies_dict(),
        headers=h,
        timeout=30,
    )
    if resp.status_code in (403, 404):
        return {"error": "Sales Navigator not available.", "status": resp.status_code}
    resp.raise_for_status()
    return resp.json() if resp.content else None


@mcp.tool()
def sales_navigator_search_people(
    keywords: str = "",
    title_keywords: str = "",
    company_keywords: str = "",
    geography: str = "",
    industry: str = "",
    seniority_level: list[str] | None = None,
    count: int = 25,
    start: int = 0,
) -> dict:
    """Search LinkedIn Sales Navigator for leads (people).

    Args:
        keywords: General keyword search
        title_keywords: Job title keywords (e.g. 'VP Engineering')
        company_keywords: Company name keywords
        geography: Location filter (city, state, or country name)
        industry: Industry filter (e.g. 'Technology', 'Finance')
        seniority_level: List of levels: ['VP', 'C_SUITE', 'DIRECTOR', 'MANAGER', 'INDIVIDUAL_CONTRIBUTOR']
        count: Number of results (max 100)
        start: Pagination offset

    Requires an active Sales Navigator subscription.
    Returns list of leads with name, title, company, location, profile_url.
    """
    query: dict = {}
    if keywords:
        query["keywords"] = keywords
    filters: dict = {}
    if title_keywords:
        filters["titleSearchFilters"] = [{"text": title_keywords, "negated": False}]
    if company_keywords:
        filters["currentCompanyKeywords"] = company_keywords
    if seniority_level:
        filters["seniorityLevelFilter"] = seniority_level
    if filters:
        query["filters"] = filters

    body = {
        "start": start,
        "count": min(count, 100),
        "query": query,
    }

    data = _sn_post("/salesApiLeadSearch", body) or {}

    if "error" in data:
        return data

    results = []
    for hit in data.get("elements", []):
        member = hit.get("member", hit)
        results.append({
            "urn":           member.get("objectUrn", hit.get("memberUrn", "")),
            "name":          f"{member.get('firstName', '')} {member.get('lastName', '')}".strip(),
            "title":         member.get("title", ""),
            "company":       member.get("currentPositions", [{}])[0].get("companyName", "") if member.get("currentPositions") else "",
            "location":      member.get("geoRegion", member.get("location", "")),
            "profile_url":   f"https://www.linkedin.com/sales/people/{member.get('entityUrn', '').split(':')[-1]},NAME_SEARCH",
            "saved":         hit.get("isSaved", False),
            "in_crmlist":    hit.get("isInCrm", False),
        })

    return {
        "count":   len(results),
        "total":   data.get("paging", {}).get("total", len(results)),
        "start":   start,
        "results": results,
    }


@mcp.tool()
def sales_navigator_search_accounts(
    keywords: str = "",
    industry: str = "",
    employee_count_min: int = 0,
    employee_count_max: int = 0,
    geography: str = "",
    count: int = 25,
    start: int = 0,
) -> dict:
    """Search LinkedIn Sales Navigator for accounts (companies).

    Args:
        keywords: Company name or description keywords
        industry: Industry filter (e.g. 'Technology')
        employee_count_min: Minimum employee count filter
        employee_count_max: Maximum employee count filter (0 = no limit)
        geography: Location filter
        count: Number of results (max 100)
        start: Pagination offset

    Requires an active Sales Navigator subscription.
    Returns list of accounts with name, industry, employee_count, location, url.
    """
    query: dict = {}
    if keywords:
        query["keywords"] = keywords
    filters: dict = {}
    if industry:
        filters["industryFilter"] = [{"text": industry, "negated": False}]
    if employee_count_min or employee_count_max:
        filters["headcountFilter"] = {
            "min": employee_count_min or 1,
            **({"max": employee_count_max} if employee_count_max else {}),
        }
    if filters:
        query["filters"] = filters

    body = {
        "start": start,
        "count": min(count, 100),
        "query": query,
    }

    data = _sn_post("/salesApiAccountSearch", body) or {}

    if "error" in data:
        return data

    results = []
    for hit in data.get("elements", []):
        company = hit.get("company", hit)
        results.append({
            "urn":            company.get("objectUrn", hit.get("companyUrn", "")),
            "name":           company.get("name", ""),
            "industry":       company.get("industry", ""),
            "employee_count": company.get("headcount", 0),
            "location":       company.get("headquarter", {}).get("city", ""),
            "description":    (company.get("description", "") or "")[:200],
            "url":            f"https://www.linkedin.com/sales/company/{company.get('entityUrn', '').split(':')[-1]}",
            "saved":          hit.get("isSaved", False),
        })

    return {
        "count":   len(results),
        "total":   data.get("paging", {}).get("total", len(results)),
        "start":   start,
        "results": results,
    }


@mcp.tool()
def sales_navigator_get_lead_lists() -> dict:
    """Get all Sales Navigator lead lists (saved lead collections).

    Requires an active Sales Navigator subscription.
    Returns list of lead lists with id, name, lead_count.
    """
    data = _sn_get("/salesApiSavedLeadLists", params={"count": 100, "start": 0})

    if "error" in data:
        return data

    results = []
    for item in data.get("elements", []):
        results.append({
            "id":          item.get("id", item.get("entityUrn", "").split(":")[-1]),
            "name":        item.get("name", ""),
            "lead_count":  item.get("savedLeadCount", 0),
            "created_at":  item.get("createdAt", ""),
        })

    return {"count": len(results), "lead_lists": results}


@mcp.tool()
def sales_navigator_save_lead(
    profile_urn: str,
    lead_list_id: str = "",
) -> dict:
    """Save a person as a lead in Sales Navigator.

    Args:
        profile_urn: LinkedIn member URN (e.g. 'urn:li:member:123456789')
                     or profile ID (e.g. 'johndoe' → will be resolved)
        lead_list_id: Optional lead list ID to add to. Saves to default if omitted.

    Requires an active Sales Navigator subscription.
    Returns { success, lead_id }.
    """
    if not profile_urn.startswith("urn:"):
        profile_urn = _member_urn(profile_urn)

    body: dict = {"memberUrn": profile_urn}
    if lead_list_id:
        body["leadListId"] = lead_list_id

    data = _sn_post("/salesApiSavedLeads", body) or {}

    if isinstance(data, dict) and "error" in data:
        return data

    return {
        "success":    True,
        "lead_id":    (data or {}).get("id", ""),
        "member_urn": profile_urn,
    }


@mcp.tool()
def sales_navigator_get_account_insights(company_id: str) -> dict:
    """Get Sales Navigator insights for a company account.

    Args:
        company_id: LinkedIn company ID (numeric) or company page URL
                    (e.g. 'https://www.linkedin.com/company/dynatrace/' → extracts 'dynatrace')

    Requires an active Sales Navigator subscription.
    Returns company insights: headcount, growth, hiring trends, key signals.
    """
    if "linkedin.com/company/" in company_id:
        company_id = company_id.rstrip("/").split("/company/")[-1].split("/")[0]

    data = _sn_get(f"/salesApiCompanies/{company_id}", params={
        "decorationId": "com.linkedin.sales.deco.desktop.searchV2.SalesCompanyResult-17",
    })

    if "error" in data:
        return data

    company = data.get("company", data)
    return {
        "name":              company.get("name", ""),
        "industry":          company.get("industry", ""),
        "employee_count":    company.get("headcount", 0),
        "employee_growth":   company.get("headcountInsight", {}).get("percentageChange", 0),
        "headquarters":      company.get("headquarter", {}),
        "description":       (company.get("description", "") or "")[:500],
        "website":           company.get("websiteUrl", ""),
        "founded":           company.get("foundedOn", {}).get("year", ""),
        "specialties":       company.get("specialties", [])[:10],
        "hiring_signals":    company.get("hiringInsight", {}),
        "recent_updates_count": company.get("statusUpdates", {}).get("total", 0),
        "url":               f"https://www.linkedin.com/sales/company/{company_id}",
    }

# ── Notifications tool ────────────────────────────────────────────────────────

@mcp.tool()
def get_notifications(count: int = 20, start: int = 0) -> dict:
    """Get your LinkedIn notifications (reactions, comments, mentions, follows).

    Returns list of notifications with type, actor, text, timestamp, url.
    """
    data = _get(f"{VOYAGER_BASE}/notifications/tabBadgeCounts")
    counts = {
        "my_network":    data.get("invitationCount", 0),
        "notifications": data.get("newNotificationsCount", 0),
        "jobs":          data.get("jobCount", 0),
        "messages":      data.get("messageCount", 0),
    }

    notifs = _get(f"{VOYAGER_BASE}/notifications/notifications", params={
        "count": min(count, 40),
        "start": start,
        "q":     "type",
        "type":  "ALL",
    })

    results = []
    for n in notifs.get("elements", []):
        actor_text = ""
        actors = n.get("headlineActors", [])
        if actors:
            actor_text = _extract_text(actors[0].get("name", {}))
        results.append({
            "type":       n.get("notificationType", ""),
            "actor":      actor_text,
            "text":       _extract_text(n.get("body", {})),
            "timestamp":  n.get("publishedAt", 0),
            "read":       n.get("seen", True),
            "url":        n.get("entityUrn", ""),
        })

    return {
        "badge_counts": counts,
        "count":        len(results),
        "start":        start,
        "results":      results,
    }


# ── Person posts tool ─────────────────────────────────────────────────────────

@mcp.tool()
def get_person_posts(profile_id: str, count: int = 5) -> dict:
    """Get recent posts from a specific LinkedIn member's profile.

    Uses headless Chrome to navigate to their recent-activity page — works
    regardless of connection degree (as long as their posts are public/visible).
    Great for agentic analysis: call this to collect a person's content before
    drafting outreach, preparing for a meeting, or researching their interests.

    Args:
        profile_id: LinkedIn public ID (e.g. 'troydclark') or full profile URL
        count: Number of recent posts to return (max 20)

    Returns list of posts with text, posted_time, post_type, url.
    """
    import re as _re

    if "linkedin.com/in/" in profile_id:
        profile_id = profile_id.rstrip("/").split("/in/")[-1].split("/")[0].split("?")[0]

    result_q: _queue.Queue = _queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            import asyncio
            asyncio.set_event_loop(None)
        except Exception:
            pass
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=str(BROWSER_PROFILE),
                    headless=True,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
                page = ctx.new_page()
                page.goto(
                    f"{LI_BASE}/in/{profile_id}/recent-activity/all/",
                    wait_until="domcontentloaded", timeout=30_000,
                )
                time.sleep(6)

                raw_text = page.evaluate("""
                    () => {
                        const main = document.querySelector('main section, main');
                        return main ? main.innerText : document.body.innerText;
                    }
                """)
                ctx.close()
                result_q.put(raw_text)
        except Exception as e:
            result_q.put(RuntimeError(str(e)))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    raw = result_q.get(timeout=90)
    if isinstance(raw, Exception):
        raise raw

    # Parse into individual posts
    chunks = _re.split(r'Feed post(?:\s+number\s+\d+)?', raw)
    posts = []
    header_skip = {
        profile_id, "1st", "2nd", "3rd+", "Premium • 1st",
        "Like", "Comment", "Share", "Send", "Repost", "Follow",
    }

    for chunk in chunks[1: count + 1]:
        lines = [l.strip() for l in chunk.split("\n") if l.strip()]
        time_match = _re.search(r"(\d+[mhdw]|\d+\s+(?:hour|day|week|minute|month)s?\s*(?:ago)?)", chunk, _re.I)
        posted_time = time_match.group(0).strip() if time_match else ""

        type_match = _re.search(r"(commented|reposted|shared)", chunk[:200], _re.I)
        post_type = type_match.group(0).lower() if type_match else "post"

        content_lines = []
        past_header = False
        for line in lines:
            if _re.match(r"^\d+mo|^\d+[dwhm]\b|^Visible to|^Like$|^Comment$|^Share$|^Send$|^Repost$|^Follow$|^Connect$", line):
                past_header = True
                continue
            if not past_header:
                continue
            if line in header_skip or len(line) < 3:
                continue
            # Skip reaction/engagement counts
            if _re.match(r"^\d[\d,]*$", line):
                continue
            content_lines.append(line)

        post_text = " ".join(content_lines).strip()
        if len(post_text) < 20:
            continue

        posts.append({
            "text":        post_text[:1000],
            "posted_time": posted_time,
            "post_type":   post_type,
            "url":         f"{LI_BASE}/in/{profile_id}/recent-activity/all/",
        })

    return {
        "profile_id": profile_id,
        "profile_url": f"{LI_BASE}/in/{profile_id}",
        "count": len(posts),
        "posts": posts,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
