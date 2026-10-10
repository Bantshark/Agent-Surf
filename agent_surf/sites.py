"""Site registry: allowed domains, page types and URL templates.

Navigation is only ever to a URL built from a template here, and any URL the
browser ends up on must stay inside the site's domains (subdomains included).
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit


class DomainRefused(ValueError):
    pass


class UnknownSite(ValueError):
    pass


@dataclass(frozen=True)
class Site:
    name: str
    domains: tuple[str, ...]
    page_types: dict[str, str] = field(default_factory=dict)  # page_type -> URL template


SITES: dict[str, Site] = {}


def register_site(site: Site) -> None:
    for page_type, template in site.page_types.items():
        if not is_allowed_url(site, template.replace("{query}", "q").replace("{handle}", "h")):
            raise DomainRefused(f"{site.name}.{page_type}: template is outside {site.domains}")
    SITES[site.name] = site


def unregister_site(name: str) -> None:
    SITES.pop(name, None)


# Page types the `inbox` command reads (notifications and messages).
INBOX_PAGE_TYPES: dict[str, set[str]] = {
    "x": {"notifications", "messages"},
    "instagram": {"messages"},
    "facebook": {"notifications"},
    "linkedin": {"notifications", "messages"},
    "reddit": {"inbox"},
}

# Whether a (site, action) cannot publish without media. Instagram feed posts
# need a photo or video; everything else is text-first. Unknown -> False.
MEDIA_REQUIRED: dict[tuple[str, str], bool] = {
    ("instagram", "post"): True,
    ("x", "post"): False, ("facebook", "post"): False, ("linkedin", "post"): False,
    ("reddit", "post"): False,
}


def media_required(site: str, action: str) -> bool:
    return MEDIA_REQUIRED.get((site, action), False)


# Where learn-action opens a composer for a new post. Replies, comments and DMs
# start from the queue item's / learn-action's --target or --thread URL.
ACTION_START: dict[str, dict[str, str]] = {
    "x": {"post": "https://x.com/compose/post"},
    "reddit": {"post": "https://www.reddit.com/submit"},
    "instagram": {"post": "https://www.instagram.com/"},
    "facebook": {"post": "https://www.facebook.com/"},
    "linkedin": {"post": "https://www.linkedin.com/feed/"},
}


# Fix 22: a login wall is not a broken map. Path patterns match the URL path
# exactly or as a prefix followed by "/"; title markers match the start of the
# lower-cased page title. Data only; challenge.detect_logged_out applies them.
@dataclass(frozen=True)
class LoggedOutMarkers:
    paths: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()


LOGGED_OUT: dict[str, LoggedOutMarkers] = {
    "x": LoggedOutMarkers(("/login", "/i/flow/login", "/i/flow/signup"),
                          ("log in to x", "sign up for x")),
    "instagram": LoggedOutMarkers(("/accounts/login", "/accounts/emailsignup"),
                                  ("login • instagram",)),
    "facebook": LoggedOutMarkers(("/login", "/login.php"),
                                 ("log in to facebook", "facebook - log in or sign up",
                                  "facebook – log in or sign up")),
    "linkedin": LoggedOutMarkers(("/login", "/authwall", "/uas/login", "/checkpoint/lg"),
                                 ("linkedin login", "linkedin: log in or sign up")),
    "reddit": LoggedOutMarkers(("/login", "/account/login", "/register"),
                               ("log in to reddit",)),
}

# The page `doctor` opens to check that the browser profile is logged in.
HOME_URL: dict[str, str] = {
    "x": "https://x.com/home",
    "reddit": "https://www.reddit.com/",
    "instagram": "https://www.instagram.com/",
    "facebook": "https://www.facebook.com/",
    "linkedin": "https://www.linkedin.com/feed/",
}


def logged_out_markers(site: str) -> LoggedOutMarkers:
    return LOGGED_OUT.get(site, LoggedOutMarkers())


def get_site(name: str) -> Site:
    try:
        return SITES[name]
    except KeyError:
        raise UnknownSite(f"unknown site {name!r}; known: {', '.join(sorted(SITES))}") from None


def placeholders(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


def build_url(site_name: str, page_type: str, *, query: str | None = None,
              handle: str | None = None) -> str:
    site = get_site(site_name)
    if page_type not in site.page_types:
        raise UnknownSite(f"{site_name} has no page type {page_type!r}; "
                          f"known: {', '.join(sorted(site.page_types))}")
    template = site.page_types[page_type]
    needed = placeholders(template)
    given = {k: v for k, v in (("query", query), ("handle", handle)) if v is not None}
    for k in sorted(needed - given.keys()):
        raise ValueError(f"{site_name} {page_type} needs --{k}")
    for k in sorted(given.keys() - needed):
        raise ValueError(f"{site_name} {page_type} does not take --{k}")
    values = {}
    for k, v in given.items():
        if not v.strip():
            raise ValueError(f"--{k} is empty")
        values[k] = quote(v.strip(), safe="")
    url = template.format(**values)
    check_url(site, url)
    return url


def is_allowed_url(site: Site, url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host:
        return False
    return any(host == d or host.endswith("." + d) for d in site.domains)


def check_url(site: Site | str, url: str) -> None:
    if isinstance(site, str):
        site = get_site(site)
    if not is_allowed_url(site, url):
        raise DomainRefused(f"refusing to navigate outside {', '.join(site.domains)}: {url}")


for _site in (
    Site("x", ("x.com",), {
        "home": "https://x.com/home",
        "search": "https://x.com/search?q={query}&f=live",
        "profile": "https://x.com/{handle}",
        "notifications": "https://x.com/notifications",
        "messages": "https://x.com/messages",
    }),
    Site("reddit", ("reddit.com",), {
        "subreddit": "https://www.reddit.com/r/{handle}/new/",
        "post": "https://www.reddit.com/comments/{handle}/",
        "search": "https://www.reddit.com/search/?q={query}&sort=new",
        "inbox": "https://www.reddit.com/message/inbox/",
    }),
    Site("instagram", ("instagram.com",), {
        "profile": "https://www.instagram.com/{handle}/",
        "feed": "https://www.instagram.com/",
        "messages": "https://www.instagram.com/direct/inbox/",
    }),
    Site("facebook", ("facebook.com",), {
        "page": "https://www.facebook.com/{handle}",
        "feed": "https://www.facebook.com/",
        "notifications": "https://www.facebook.com/notifications",
    }),
    Site("linkedin", ("linkedin.com",), {
        "profile": "https://www.linkedin.com/in/{handle}/recent-activity/all/",
        "feed": "https://www.linkedin.com/feed/",
        "jobs": "https://www.linkedin.com/jobs/search/?keywords={query}",
        "notifications": "https://www.linkedin.com/notifications/",
        "messages": "https://www.linkedin.com/messaging/",
    }),
):
    register_site(_site)
