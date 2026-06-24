# LinkedIn MCP

A [Model Context Protocol](https://modelcontextprotocol.io) server for LinkedIn, designed for use with [Claude Code](https://claude.ai/code). Uses browser-harvest authentication (Playwright) — no official API key required.

**23 tools** covering authentication, search, profiles, feed, posting, messaging, per-person post collection, and Sales Navigator.

---

## Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) — `brew install uv`
- Playwright Chromium — installed automatically on first run
- A LinkedIn account

---

## Installation

### 1. Clone

```bash
git clone https://github.com/clabrado/linkedin-mcp.git ~/Projects/linkedin-mcp
```

### 2. Install Playwright browsers (one-time)

```bash
uv run --with playwright playwright install chromium
```

### 3. Register with Claude Code

```bash
claude mcp add-json --scope user linkedin '{
  "command": "uv",
  "args": ["run", "--script", "/path/to/linkedin-mcp/linkedin_mcp.py"],
  "env": {
    "LI_EMAIL": "your@email.com"
  }
}'
```

Replace `/path/to/linkedin-mcp/linkedin_mcp.py` with the actual path and `your@email.com` with your LinkedIn login email.

### 4. Authenticate

In Claude Code, call:

```
mcp__linkedin__browser_login
```

A Chrome window opens, pre-filled with your email. Complete password/MFA. The browser closes automatically once login is detected. Session is saved to `~/.cache/linkedin-mcp/session.json` and persists for 30 days.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `LI_EMAIL` | Recommended | Pre-fills the login email in the browser window. Not required but saves a step. |

---

## Auth Flow

```
browser_login  →  Chrome opens  →  you log in  →  cookies harvested
                                                        ↓
                              ~/.cache/linkedin-mcp/session.json
                                                        ↓
                              all subsequent tool calls use this session
                              (silent headless refresh on expiry)
```

**Session storage:** `~/.cache/linkedin-mcp/`
- `session.json` — cookies + CSRF (30-day trust cap; excluded from git via `.gitignore`)
- `browser-profile/` — persistent Chromium profile (SSO cookies for silent refresh)

> ⚠️ Never commit `~/.cache/linkedin-mcp/` — it contains your session cookies.

---

## Tools Reference

### Authentication

#### `browser_login`
Open a Chrome browser for interactive LinkedIn login. Pre-fills `LI_EMAIL` if set. Browser closes automatically once login is detected.

```
Returns: { success, name, member_id, profile_id, expires_in_days }
```

#### `token_status`
Check whether the current session is valid without making a full API call.

```
Returns: { valid, name, member_id, expires_in_seconds }
```

#### `whoami`
Get the authenticated user's profile. Reads from session cache (populated by `browser_login`); attempts live API call first and falls back to cache.

```
Returns: { name, headline, member_id, profile_urn, profile_url, source }
```

#### `logout`
Clear the session file and in-memory session. Preserves the browser profile so the next `browser_login` is low-friction.

```
Returns: { success }
```

---

### Search

#### `search_people`
Search LinkedIn for people.

```
Args:
  keywords        str   — name, title, skills, company
  count           int   — results to return (default 10, max 49)
  start           int   — pagination offset
  title_filter    str   — optional job title filter
  company_filter  str   — optional company name filter
  location_filter str   — optional location filter

Returns: { count, start, results: [{ urn, name, headline, company, location, profile_url }] }
```

#### `search_companies`
Search LinkedIn for companies.

```
Args:
  keywords        str   — company name or description
  count           int   — default 10, max 49
  start           int   — pagination offset
  industry_filter str   — optional industry filter

Returns: { count, start, results: [{ urn, name, description, follower_count, url }] }
```

#### `search_posts`
Search LinkedIn posts and content.

```
Args:
  keywords    str   — keywords to search for
  count       int   — default 10, max 49
  start       int   — pagination offset
  date_range  str   — 'past_24h' | 'past_week' | 'past_month' | 'any_time'

Returns: { count, start, results: [{ urn, author, description, subtitle, url }] }
```

#### `search_jobs`
Search LinkedIn job postings.

```
Args:
  keywords  str   — job title, skills, or company
  location  str   — city, state, or country
  count     int   — default 10, max 49
  start     int   — pagination offset

Returns: { count, start, results: [{ title, company, location, listed_at, url }] }
```

---

### Profile & Network

#### `get_profile`
Get a LinkedIn profile by public ID (slug) or full URL.

```
Args:
  profile_id  str   — e.g. 'johndoe' or 'https://www.linkedin.com/in/johndoe/'

Returns: { name, headline, summary, location, connections, followers,
           profile_url, experience[], education[], skills[] }
```

#### `get_connections`
Get your 1st-degree connections.

```
Args:
  count           int   — default 40, max 100
  start           int   — pagination offset
  keyword_filter  str   — optional name/company filter

Returns: { total, count, start, results: [{ name, headline, profile_id, profile_url, connected_at }] }
```

---

### Feed & Engagement

#### `get_feed`
Get your LinkedIn feed (recent posts from your network).

> Uses headless Playwright — takes ~10s. LinkedIn's feed is server-side rendered; the old REST endpoint is deprecated.

```
Args:
  count  int   — default 10
  start  int   — pagination offset (triggers scroll to load more)

Returns: { count, start, results: [{ urn, author, degree, headline,
           posted_time, text, reactions, comments }] }
```

#### `get_person_posts`
Get recent posts from any LinkedIn member's profile. **The primary tool for agentic research** — use before outreach, account planning, or meeting prep.

> Uses headless Playwright (~10s). Works for any visible profile regardless of connection degree.

```
Args:
  profile_id  str   — public ID (e.g. 'johndoe') or full profile URL
  count       int   — default 5, max 20

Returns: { profile_id, profile_url, count,
           posts: [{ text, posted_time, post_type, url }] }
```

**Example agentic workflow:**
```
1. search_people(keywords="CTO at Acme Corp")
2. get_person_posts(profile_id="result_slug", count=5)
3. → feed posts to Claude for outreach draft / meeting brief
```

#### `create_post`
Post to LinkedIn on behalf of the authenticated user.

```
Args:
  text                 str   — post text content
  visibility           str   — 'PUBLIC' | 'CONNECTIONS' | 'LOGGED_IN'
  article_url          str   — optional URL to attach as link preview
  article_title        str   — title for link preview
  article_description  str   — description for link preview

Returns: { success, post_id, post_url }
```

#### `react_to_post`
React to a LinkedIn post.

```
Args:
  post_urn  str   — e.g. 'urn:li:activity:1234567890'
  reaction  str   — 'LIKE' | 'CELEBRATE' | 'SUPPORT' | 'FUNNY' | 'LOVE' | 'INSIGHTFUL'

Returns: { success, reaction, post_urn }
```

#### `comment_on_post`
Add a comment to a LinkedIn post.

```
Args:
  post_urn  str   — post URN
  text      str   — comment text

Returns: { success, comment_id }
```

#### `send_message`
Send a LinkedIn message or InMail.

```
Args:
  recipient_profile_id  str   — public ID or member URN
  message_text          str   — message body
  subject               str   — optional subject (for InMail)

Returns: { success, conversation_id }
```

#### `get_messages`
Get recent message conversations.

```
Args:
  count  int   — default 20, max 40
  start  int   — pagination offset

Returns: { count, start, results: [{ conversation_id, participants, unread, last_message_at, url }] }
```

#### `get_notifications`
Get LinkedIn notifications (reactions, comments, mentions, follows).

```
Args:
  count  int   — default 20, max 40
  start  int   — pagination offset

Returns: { badge_counts: { my_network, notifications, jobs, messages },
           count, start, results: [{ type, actor, text, timestamp, read }] }
```

---

### Sales Navigator

> All Sales Navigator tools require an active [Sales Navigator](https://business.linkedin.com/sales-solutions/sales-navigator) subscription. Tools return `{ error: "Sales Navigator not available" }` gracefully if the subscription is missing.

#### `sales_navigator_search_people`
Search for leads (people) in Sales Navigator.

```
Args:
  keywords         str        — general keywords
  title_keywords   str        — job title filter (e.g. 'VP Engineering')
  company_keywords str        — company name filter
  geography        str        — location filter
  industry         str        — industry filter
  seniority_level  list[str]  — ['VP', 'C_SUITE', 'DIRECTOR', 'MANAGER', 'INDIVIDUAL_CONTRIBUTOR']
  count            int        — default 25, max 100
  start            int        — pagination offset

Returns: { count, total, start, results: [{ urn, name, title, company, location, profile_url, saved }] }
```

#### `sales_navigator_search_accounts`
Search for accounts (companies) in Sales Navigator.

```
Args:
  keywords              str   — company name or description
  industry              str   — industry filter
  employee_count_min    int   — minimum headcount
  employee_count_max    int   — maximum headcount (0 = no limit)
  geography             str   — location filter
  count                 int   — default 25, max 100
  start                 int   — pagination offset

Returns: { count, total, start, results: [{ urn, name, industry, employee_count, location, url, saved }] }
```

#### `sales_navigator_get_lead_lists`
Get all your saved lead lists.

```
Returns: { count, lead_lists: [{ id, name, lead_count, created_at }] }
```

#### `sales_navigator_save_lead`
Save a person as a lead in Sales Navigator.

```
Args:
  profile_urn   str   — member URN (e.g. 'urn:li:member:123456789') or public ID slug
  lead_list_id  str   — optional lead list ID; saves to default if omitted

Returns: { success, lead_id, member_urn }
```

#### `sales_navigator_get_account_insights`
Get Sales Navigator intelligence for a company.

```
Args:
  company_id  str   — numeric company ID or LinkedIn company page URL

Returns: { name, industry, employee_count, employee_growth, headquarters,
           description, website, founded, specialties[], hiring_signals, url }
```

---

## Agentic Workflows

### Pre-call research
```
search_people(keywords="John Smith Acme") 
→ get_person_posts(profile_id="johnsmith", count=5)
→ get_profile(profile_id="johnsmith")
→ [Claude drafts meeting brief]
```

### Territory intelligence sweep
```
sales_navigator_search_accounts(industry="Technology", employee_count_min=500)
→ for each account: sales_navigator_get_account_insights(company_id=...)
→ [Claude scores accounts by signals]
```

### Network engagement
```
get_feed(count=20)
→ [Claude identifies posts worth engaging]
→ react_to_post(post_urn=...) or comment_on_post(post_urn=..., text=...)
```

---

## Session Management

| File | Purpose |
|---|---|
| `~/.cache/linkedin-mcp/session.json` | Cookies + CSRF + profile info (30-day TTL) |
| `~/.cache/linkedin-mcp/browser-profile/` | Chromium persistent profile for silent refresh |

**Re-authentication:** Call `browser_login` whenever `token_status` returns `valid: false`. Sessions typically last 30 days unless LinkedIn invalidates them server-side.

**Rate limiting:** LinkedIn enforces rate limits on API calls. If you see 429 errors, wait 5–15 minutes before retrying. The feed and `get_person_posts` tools use browser rendering and are not subject to API rate limits.

---

## Technical Notes

- **Voyager API**: LinkedIn's unofficial internal REST/GraphQL API. Endpoints may drift with LinkedIn releases. If httpx-based tools (search, profile, messaging) start returning 403/400, re-run `browser_login` to refresh the CSRF token.
- **Feed + person posts**: Use Playwright DOM extraction (~10s per call) — the old REST feed endpoint is deprecated.
- **CSRF**: Captured directly from intercepted Voyager API request headers during `browser_login`. Not derived, not guessed.
- **httpx.Client**: Used (not bare `httpx.get`) so cookies persist correctly across any redirect hops LinkedIn issues.
- **Sales Navigator**: Uses `https://www.linkedin.com/sales-api/` — same session, separate endpoint namespace.

---

## .gitignore

Add to your `.gitignore`:

```
# LinkedIn MCP — never commit session credentials
~/.cache/linkedin-mcp/
```

---

## License

MIT
