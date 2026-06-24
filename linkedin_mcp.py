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
