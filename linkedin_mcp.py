#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=1.2.0",
#   "httpx>=0.27.0",
#   "playwright>=1.40",
# ]
# ///
"""LinkedIn MCP server — cookie-harvest auth via persistent Chrome profile.

Auth model:
  - LinkedIn uses httpOnly session cookies (li_at) plus CSRF captured from
    intercepted Voyager API request headers during browser_login.
  - Cookies are stored in ~/.cache/linkedin-mcp/session.json (30-day TTL).
  - All Voyager API calls use httpx.Client with a full cookie jar so cookies
    persist across any redirect hops LinkedIn issues.
  - Feed and per-person post collection use Playwright headless Chrome because
    LinkedIn's feed is server-side rendered (REST endpoint deprecated).

Token resolution chain (first valid path wins):
  1. In-process session cache (valid for SESSION_TTL seconds)
  2. Stored session file  ~/.cache/linkedin-mcp/session.json
  3. Headless Chrome silent refresh (reuses persisted SSO cookies in profile)
  4. Raise → call browser_login for one-time interactive sign-in

Configuration (env vars):
  LI_EMAIL   Your LinkedIn login email — pre-filled in the browser_login window

Covers:
  - Auth (browser_login, token_status, whoami, logout)
  - Search (people, companies, posts, jobs)
  - Profile & network (get_profile, connections)
  - Engagement (create_post, comment, react, send_message, feed, notifications)
  - Person posts (get_person_posts — for agentic profile research)
  - Sales Navigator (lead search, account search, lead lists, save lead, insights)
"""

import json
import os
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
    "csrf":         None,   # captured from Voyager request headers
    "member_id":    None,   # numeric LinkedIn member ID
    "profile_urn":  None,   # urn:li:member:{id}
    "profile_id":   None,   # public slug (e.g. 'johndoe')
    "name":         None,
    "captured_at":  0,
}

# ── Session persistence ─────────────────────────────────────────────────────

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

# ── Browser harvest ────────────────────────────────────────────────────────

def _harvest_session(headless: bool, timeout_sec: int) -> bool:
    """Drive Chrome to LinkedIn and capture session cookies + CSRF.

    headless=False — interactive; pre-fills LOGIN_EMAIL, waits for login.
    headless=True  — silent refresh via persisted SSO cookies in BROWSER_PROFILE.

    CSRF is captured from an intercepted Voyager API request header (same pattern
    as Highspot MCP) because deriving it from JSESSIONID is unreliable across
    LinkedIn's auth variants.
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

                _session.update(
                    cookies=captured["cookies"],
                    csrf=captured["csrf"] or "",
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

# ── HTTP client ────────────────────────────────────────────────────────────────

def _make_client(timeout: int = 20) -> httpx.Client:
    """Build a stateful httpx.Client with all LinkedIn cookies loaded into its jar.

    Using Client (not bare httpx.get) is critical: when LinkedIn returns a 302
    with Set-Cookie headers, the Client persists those cookies for the follow-up
    request, exactly like a browser would.
    """
    client = httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={
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
        },
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
        _session.update(cookies=None, csrf=None, captured_at=0)
        _ensure_session()
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
        _session.update(cookies=None, csrf=None, captured_at=0)
        _ensure_session()
        with _make_client(timeout) as client:
            if extra_headers:
                client.headers.update(extra_headers)
            client.headers["content-type"] = "application/json"
            resp = client.post(url, json=body)
        _check_auth_redirect(resp)
    resp.raise_for_status()
    return resp.json() if resp.content else None

# ── Response helpers ────────────────────────────────────────────────────────────

def _extract_text(obj: dict | str | None) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    return obj.get("text", "") or obj.get("$recipeTypes", [""])[0] or str(obj)


def _profile_from_hit(hit: dict) -> dict:
    """Extract a clean profile summary from a Voyager search hit."""
    target = hit.get("targetUrn", "")
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

# ── FastMCP server ─────────────────────────────────────────────────────────────

mcp = FastMCP("linkedin")

# ── Auth tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def browser_login() -> dict:
    """Open a Chrome browser for interactive LinkedIn login.

    Pre-fills the LI_EMAIL env var if set. After you complete password/MFA,
    the session cookies are harvested and saved to ~/.cache/linkedin-mcp/session.json.
    The browser closes automatically once login is detected.

    Returns { success, name, member_id, profile_id, expires_in_days }.
    """
    if not _harvest_session(headless=False, timeout_sec=300):
        return {"success": False, "error": "Login not detected within timeout."}

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

    return {
        "valid":             True,
        "name":              _session.get("name", ""),
        "member_id":         _session.get("member_id", ""),
        "expires_in_seconds": max(0, int(SESSION_TTL - (time.time() - _session["captured_at"]))),
    }


@mcp.tool()
def whoami() -> dict:
    """Get the profile of the currently authenticated LinkedIn user.

    Attempts a live Voyager API call first; falls back to the session cache
    populated by browser_login if the API call fails (CSRF can go stale after
    the login browser closes).
    """
    _ensure_session()

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

# ── Search tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def search_people(
    keywords: str,
    count: int = 10,
    start: int = 0,
    title_filter: str = "",
    company_filter: str = "",
    location_filter: str = "",
) -> dict:
    """Search LinkedIn for people by keywords.

    Args:
        keywords: Search keywords (name, title, skills, etc.)
        count: Number of results (max 49)
        start: Pagination offset
        title_filter: Optional job title filter
        company_filter: Optional company name filter
        location_filter: Optional location (city, country)

    Returns list of profiles with name, headline, company, location, profile_url.
    """
    filters = []
    if title_filter:
        filters.append(f"(key:title,value:List({urllib.parse.quote(title_filter)}))")
    if company_filter:
        filters.append(f"(key:currentCompany,value:List({urllib.parse.quote(company_filter)}))")
    if location_filter:
        filters.append(f"(key:geoUrn,value:List({urllib.parse.quote(location_filter)}))")

    filter_str = ",".join(filters)
    query_params = "List((key:resultType,value:List(PEOPLE))" + (f",{filter_str}" if filter_str else "") + ")"
    query = (
        f"(keywords:{urllib.parse.quote(keywords)},"
        f"flagshipSearchIntent:SEARCH_SRP,"
        f"queryParameters:{query_params},"
        f"includeFiltersInResponse:false)"
    )

    data = _get(f"{VOYAGER_BASE}/search/blended", params={
        "count":  min(count, 49),
        "origin": "FACETED_SEARCH",
        "q":      "blended",
        "query":  query,
        "start":  start,
    })

    results = []
    for cluster in data.get("elements", []):
        for item in cluster.get("items", []):
            hit = item.get("item", {}).get("entityResult", item.get("item", {}))
            if not hit:
                continue
            results.append({
                "urn":         hit.get("targetUrn", hit.get("entityUrn", "")),
                "name":        _extract_text(hit.get("title", {})),
                "headline":    _extract_text(hit.get("primarySubtitle", hit.get("headline", {}))),
                "company":     _extract_text(hit.get("secondarySubtitle", {})),
                "location":    _extract_text(hit.get("tertiarySubtitle", {})),
                "summary":     _extract_text(hit.get("summary", {})),
                "profile_url": hit.get("navigationUrl", ""),
            })

    return {"count": len(results), "start": start, "results": results}


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
        industry_filter: Optional industry filter

    Returns list with name, description, follower_count, url.
    """
    query_params = "List((key:resultType,value:List(COMPANIES)))"
    if industry_filter:
        query_params = f"List((key:resultType,value:List(COMPANIES)),(key:industry,value:List({urllib.parse.quote(industry_filter)})))"

    query = (
        f"(keywords:{urllib.parse.quote(keywords)},"
        f"flagshipSearchIntent:SEARCH_SRP,"
        f"queryParameters:{query_params},"
        f"includeFiltersInResponse:false)"
    )

    data = _get(f"{VOYAGER_BASE}/search/blended", params={
        "count":  min(count, 49),
        "origin": "FACETED_SEARCH",
        "q":      "blended",
        "query":  query,
        "start":  start,
    })

    results = []
    for cluster in data.get("elements", []):
        for item in cluster.get("items", []):
            hit = item.get("item", {}).get("entityResult", item.get("item", {}))
            if not hit:
                continue
            results.append({
                "urn":          hit.get("targetUrn", hit.get("entityUrn", "")),
                "name":         _extract_text(hit.get("title", {})),
                "description":  _extract_text(hit.get("primarySubtitle", {})),
                "follower_count": _extract_text(hit.get("secondarySubtitle", {})),
                "url":          hit.get("navigationUrl", ""),
            })

    return {"count": len(results), "start": start, "results": results}


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
        date_range: 'past_24h', 'past_week', 'past_month', or 'any_time'

    Returns list of posts with author, text, url.
    """
    date_map = {
        "past_24h":   "1",
        "past_week":  "1W",
        "past_month": "1M",
        "any_time":   "",
    }
    sort_by = date_map.get(date_range, "")
    query_params = "List((key:resultType,value:List(CONTENT)))"
    if sort_by:
        query_params = f"List((key:resultType,value:List(CONTENT)),(key:datePosted,value:List({sort_by})))"

    query = (
        f"(keywords:{urllib.parse.quote(keywords)},"
        f"flagshipSearchIntent:SEARCH_SRP,"
        f"queryParameters:{query_params},"
        f"includeFiltersInResponse:false)"
    )

    data = _get(f"{VOYAGER_BASE}/search/blended", params={
        "count":  min(count, 49),
        "origin": "FACETED_SEARCH",
        "q":      "blended",
        "query":  query,
        "start":  start,
    })

    results = []
    for cluster in data.get("elements", []):
        for item in cluster.get("items", []):
            hit = item.get("item", {}).get("entityResult", item.get("item", {}))
            if not hit:
                continue
            results.append({
                "urn":          hit.get("targetUrn", hit.get("entityUrn", "")),
                "author":       _extract_text(hit.get("title", {})),
                "description":  _extract_text(hit.get("summary", {})),
                "subtitle":     _extract_text(hit.get("primarySubtitle", {})),
                "url":          hit.get("navigationUrl", ""),
            })

    return {"count": len(results), "start": start, "results": results}


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

    Returns list with title, company, location, url.
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

# ── Profile & network tools ─────────────────────────────────────────────────────

@mcp.tool()
def get_profile(profile_id: str) -> dict:
    """Get a LinkedIn profile by public ID (slug) or full profile URL.

    Args:
        profile_id: LinkedIn public ID (e.g. 'johndoe') or URL

    Returns full profile: name, headline, summary, experience, education, skills.
    """
    if "linkedin.com/in/" in profile_id:
        profile_id = profile_id.rstrip("/").split("/in/")[-1].split("/")[0].split("?")[0]

    data = _get(
        f"{VOYAGER_BASE}/identity/profiles/{profile_id}/profileView",
        params={"memberIdentity": profile_id},
    )

    profile   = data.get("profile", data)
    mini      = profile.get("miniProfile", {})
    positions = data.get("positionView", {}).get("elements", [])
    education = data.get("educationView", {}).get("elements", [])
    skills    = data.get("skillView", {}).get("elements", [])

    experience = []
    for pos in positions[:5]:
        company = pos.get("company", {})
        experience.append({
            "title":       pos.get("title", ""),
            "company":     company.get("name", _extract_text(pos.get("companyName", ""))),
            "start":       pos.get("timePeriod", {}).get("startDate", {}),
            "end":         pos.get("timePeriod", {}).get("endDate", {}),
            "description": _extract_text(pos.get("description", "")),
        })

    edu = []
    for e in education[:3]:
        school = e.get("school", {})
        edu.append({
            "school":  school.get("name", _extract_text(e.get("schoolName", ""))),
            "degree":  e.get("degreeName", ""),
            "field":   e.get("fieldOfStudy", ""),
        })

    return {
        "name":         f"{mini.get('firstName', '')} {mini.get('lastName', '')}".strip(),
        "headline":     _extract_text(mini.get("occupation", "")),
        "summary":      _extract_text(profile.get("summary", "")),
        "location":     _extract_text(profile.get("locationName", "")),
        "connections":  profile.get("connectionsCount", {}).get("count", 0),
        "followers":    profile.get("followersCount", 0),
        "profile_url":  f"https://www.linkedin.com/in/{mini.get('publicIdentifier', profile_id)}",
        "experience":   experience,
        "education":    edu,
        "skills":       [s.get("name", "") for s in skills[:15]],
    }


@mcp.tool()
def get_connections(count: int = 40, start: int = 0, keyword_filter: str = "") -> dict:
    """Get your LinkedIn 1st-degree connections.

    Args:
        count: Number of connections to return (max 100)
        start: Pagination offset
        keyword_filter: Optional name/company filter

    Returns list of connections with name, headline, profile_url, connected_at.
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

# ── Engagement tools ──────────────────────────────────────────────────────────────

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
        text: Post text content (supports Unicode, line breaks)
        visibility: 'PUBLIC', 'CONNECTIONS', or 'LOGGED_IN'
        article_url: Optional URL to attach as a link preview
        article_title: Title for the link preview
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
                **({",media": media} if media else {}),
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

    LinkedIn's feed is now SSR'd + React hydrated; the old REST endpoint
    /voyager/api/feed/updatesV2 returns 400.
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

    Uses headless Chrome — takes ~10s. LinkedIn's feed is SSR'd (REST endpoint
    deprecated). Scroll-based pagination.

    Returns list of feed items with author, headline, degree, text, posted_time,
    reactions, comments.
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
        reaction: 'LIKE', 'CELEBRATE', 'SUPPORT', 'FUNNY', 'LOVE', or 'INSIGHTFUL'

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
    """Send a LinkedIn message to a connection.

    Args:
        recipient_profile_id: LinkedIn public ID or URN of the recipient
        message_text: Message body text
        subject: Optional subject (for InMail)

    Returns { success, conversation_id }.
    """
    _ensure_session()

    if not recipient_profile_id.startswith("urn:"):
        profile_data = _get(
            f"{VOYAGER_BASE}/identity/profiles/{recipient_profile_id}/profileView"
        )
        mini = profile_data.get("profile", {}).get("miniProfile", {})
        obj_urn = mini.get("objectUrn", "")
        member_num = obj_urn.split(":")[-1]
        recipient_urn = f"urn:li:member:{member_num}"
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

    Returns list of conversations with participants, unread count, last_message_at, url.
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
        results.append({
            "conversation_id":   conv.get("entityUrn", ""),
            "participants":      participants,
            "unread":            conv.get("unreadCount", 0),
            "last_message_at":   conv.get("lastActivityAt", 0),
            "url":               f"https://www.linkedin.com/messaging/thread/{conv.get('dashEntityUrn', '').split(':')[-1]}/",
        })

    return {"count": len(results), "start": start, "results": results}

# ── Notifications ──────────────────────────────────────────────────────────────

@mcp.tool()
def get_notifications(count: int = 20, start: int = 0) -> dict:
    """Get your LinkedIn notifications (reactions, comments, mentions, follows).

    Returns badge_counts plus a list with type, actor, text, timestamp, read.
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

# ── Person posts ───────────────────────────────────────────────────────────────

@mcp.tool()
def get_person_posts(profile_id: str, count: int = 5) -> dict:
    """Get recent posts from a specific LinkedIn member's profile.

    Uses headless Chrome to navigate to their recent-activity page — works
    regardless of connection degree (as long as their posts are visible).

    The primary tool for agentic research: call this to collect a person's
    content before drafting outreach, preparing for a meeting, or researching
    their interests.

    Args:
        profile_id: LinkedIn public ID (e.g. 'johndoe') or full profile URL
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
        "profile_id":  profile_id,
        "profile_url": f"{LI_BASE}/in/{profile_id}",
        "count":       len(posts),
        "posts":       posts,
    }

# ── Sales Navigator ────────────────────────────────────────────────────────────

def _sn_request(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict | None:
    """Make a Sales Navigator API request using the shared httpx.Client."""
    _ensure_session()
    with _make_client(timeout=30) as client:
        client.headers["x-li-page-instance"] = "urn:li:page:sales_homepage;"
        client.headers["referer"] = "https://www.linkedin.com/sales/"
        if method == "GET":
            resp = client.get(f"{SALES_BASE}{path}", params=params)
        else:
            client.headers["content-type"] = "application/json"
            resp = client.post(f"{SALES_BASE}{path}", json=body)
    if resp.status_code in (403, 404):
        return {"error": "Sales Navigator not available. Requires an active Sales Navigator subscription.", "status": resp.status_code}
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
        title_keywords: Job title filter (e.g. 'VP Engineering')
        company_keywords: Company name filter
        geography: Location filter
        industry: Industry filter (e.g. 'Technology')
        seniority_level: ['VP', 'C_SUITE', 'DIRECTOR', 'MANAGER', 'INDIVIDUAL_CONTRIBUTOR']
        count: Number of results (max 100)
        start: Pagination offset

    Requires an active Sales Navigator subscription.
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

    data = _sn_request("POST", "/salesApiLeadSearch", body={
        "start": start, "count": min(count, 100), "query": query,
    }) or {}

    if "error" in data:
        return data

    results = []
    for hit in data.get("elements", []):
        member = hit.get("member", hit)
        results.append({
            "urn":         member.get("objectUrn", hit.get("memberUrn", "")),
            "name":        f"{member.get('firstName', '')} {member.get('lastName', '')}".strip(),
            "title":       member.get("title", ""),
            "company":     (member.get("currentPositions") or [{}])[0].get("companyName", ""),
            "location":    member.get("geoRegion", member.get("location", "")),
            "profile_url": f"https://www.linkedin.com/sales/people/{member.get('entityUrn', '').split(':')[-1]},NAME_SEARCH",
            "saved":       hit.get("isSaved", False),
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
        industry: Industry filter
        employee_count_min: Minimum employee count
        employee_count_max: Maximum employee count (0 = no limit)
        geography: Location filter
        count: Number of results (max 100)
        start: Pagination offset

    Requires an active Sales Navigator subscription.
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

    data = _sn_request("POST", "/salesApiAccountSearch", body={
        "start": start, "count": min(count, 100), "query": query,
    }) or {}

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
    """Get all Sales Navigator saved lead lists.

    Requires an active Sales Navigator subscription.
    """
    data = _sn_request("GET", "/salesApiSavedLeadLists", params={"count": 100, "start": 0}) or {}

    if "error" in data:
        return data

    results = []
    for item in data.get("elements", []):
        results.append({
            "id":         item.get("id", item.get("entityUrn", "").split(":")[-1]),
            "name":       item.get("name", ""),
            "lead_count": item.get("savedLeadCount", 0),
            "created_at": item.get("createdAt", ""),
        })

    return {"count": len(results), "lead_lists": results}


@mcp.tool()
def sales_navigator_save_lead(profile_urn: str, lead_list_id: str = "") -> dict:
    """Save a person as a lead in Sales Navigator.

    Args:
        profile_urn: LinkedIn member URN or public profile ID slug
        lead_list_id: Optional lead list ID; saves to default if omitted

    Requires an active Sales Navigator subscription.
    """
    if not profile_urn.startswith("urn:"):
        profile_data = _get(
            f"{VOYAGER_BASE}/identity/profiles/{profile_urn}/profileView"
        )
        mini = profile_data.get("profile", {}).get("miniProfile", {})
        member_id = mini.get("objectUrn", "").split(":")[-1]
        profile_urn = f"urn:li:member:{member_id}"

    body: dict = {"memberUrn": profile_urn}
    if lead_list_id:
        body["leadListId"] = lead_list_id

    data = _sn_request("POST", "/salesApiSavedLeads", body=body) or {}

    if isinstance(data, dict) and "error" in data:
        return data

    return {
        "success":    True,
        "lead_id":    (data or {}).get("id", ""),
        "member_urn": profile_urn,
    }


@mcp.tool()
def sales_navigator_get_account_insights(company_id: str) -> dict:
    """Get Sales Navigator insights for a company.

    Args:
        company_id: Numeric company ID or company page URL
                    (e.g. 'https://www.linkedin.com/company/dynatrace/')

    Requires an active Sales Navigator subscription.
    """
    if "linkedin.com/company/" in company_id:
        company_id = company_id.rstrip("/").split("/company/")[-1].split("/")[0]

    data = _sn_request("GET", f"/salesApiCompanies/{company_id}", params={
        "decorationId": "com.linkedin.sales.deco.desktop.searchV2.SalesCompanyResult-17",
    }) or {}

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
        "url":               f"https://www.linkedin.com/sales/company/{company_id}",
    }

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
