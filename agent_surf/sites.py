"""Site registry: allowed domains, page types and URL templates.

Navigation is only ever to a URL built from a template here, and any URL the
browser ends up on must stay inside the site's domains (subdomains included).
"""

from __future__ import annotations

import ipaddress
import re
import string
from dataclasses import dataclass, field, replace
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


# Fix 33: page types whose --handle is a full URL on one of the site's
# domains (validated, never quoted into a template).
URL_HANDLE_PAGE_TYPES: dict[tuple[str, str], re.Pattern] = {
    ("substack", "post"): re.compile(r"^/p/[A-Za-z0-9._~%-]+/?$"),
}
# --handle values that end up in a host name (a subdomain) must be a label.
HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
HOST_HANDLE_PAGE_TYPES = {("substack", "publication")}
# Sites whose publications may live on custom domains the user adds
# (`sites add-domain`); every other site has only its built-in domains.
CUSTOM_DOMAIN_SITES = {"substack"}


def register_site(site: Site) -> None:
    for page_type, template in site.page_types.items():
        if (site.name, page_type) in URL_HANDLE_PAGE_TYPES:
            continue
        if not is_allowed_url(site, template.replace("{query}", "q").replace("{handle}", "h")):
            raise DomainRefused(f"{site.name}.{page_type}: template is outside {site.domains}")
    SITES[site.name] = site


def unregister_site(name: str) -> None:
    SITES.pop(name, None)


_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def normalize_custom_domain(site_name: str, domain: str) -> str:
    """Validate a custom publication domain for ``site_name``: a plain host
    name with at least two labels; no wildcard, scheme, port, path or IP, and
    not a domain of another registered site."""
    if site_name not in CUSTOM_DOMAIN_SITES:
        raise ValueError(f"{site_name} does not take custom domains (only: {', '.join(sorted(CUSTOM_DOMAIN_SITES))})")
    d = (domain or "").strip().lower().rstrip(".")
    if not d or any(c in d for c in "*/:@ ?#"):
        raise ValueError(f"not a plain domain name: {domain!r} (no wildcard, scheme, port or path)")
    if _is_ip(d):
        raise ValueError(f"an IP address is not a publication domain: {domain!r}")
    labels = d.split(".")
    if len(labels) < 2 or not all(_DOMAIN_LABEL.match(x) for x in labels) or not labels[-1].isalpha():
        raise ValueError(f"not a valid domain name: {domain!r}")
    for other in SITES.values():
        if other.name != site_name and any(d == o or d.endswith("." + o) for o in other.domains):
            raise ValueError(f"{d} belongs to {other.name}; refusing to add it to {site_name}")
    return d


def add_site_domains(site_name: str, domains: list[str] | tuple[str, ...]) -> Site:
    """Extend a registered site's allowed domains (custom publication domains
    loaded from the store). Returns the updated site."""
    site = get_site(site_name)
    extra = tuple(normalize_custom_domain(site_name, d) for d in domains)
    updated = replace(site, domains=tuple(dict.fromkeys(site.domains + extra)))
    SITES[site_name] = updated
    return updated


# Page types the `inbox` command reads (notifications and messages).
INBOX_PAGE_TYPES: dict[str, set[str]] = {
    "x": {"notifications", "messages"},
    "instagram": {"messages"},
    "facebook": {"notifications"},
    "linkedin": {"notifications", "messages"},
    "reddit": {"inbox"},
    "substack": {"notifications"},   # Fix 33: a guess, validate locally
}

# Whether a (site, action) cannot publish without media. Instagram feed posts
# need a photo or video; everything else is text-first. Unknown -> False.
MEDIA_REQUIRED: dict[tuple[str, str], bool] = {
    ("instagram", "post"): True,
    ("x", "post"): False, ("facebook", "post"): False, ("linkedin", "post"): False,
    ("reddit", "post"): False,
    ("substack", "post"): False, ("substack", "comment"): False,
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
    "substack": {"post": "https://substack.com/notes"},   # Substack Notes (not newsletter posts)
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
    "substack": LoggedOutMarkers(("/sign-in", "/account/login"), ("sign in - substack", "sign in | substack")),
}

# Fix 29: an empty result page is not a broken map. Markers that a page says
# "no results": URL path prefixes, title substrings, text in the accessibility
# snapshot and data-testid values, all matched case-insensitively. Guesses to
# be validated locally; data only, extend freely.
@dataclass(frozen=True)
class NoResultsMarkers:
    paths: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()
    texts: tuple[str, ...] = ()
    testids: tuple[str, ...] = ()


NO_RESULTS: dict[str, NoResultsMarkers] = {
    "x": NoResultsMarkers(texts=("no results for",), testids=("emptyState",)),
    "linkedin": NoResultsMarkers(texts=("no results found",)),
    "reddit": NoResultsMarkers(texts=("no results", "couldn't find any results",
                                      "couldn’t find any results")),
    "instagram": NoResultsMarkers(texts=("no results found",)),
    "facebook": NoResultsMarkers(texts=("we didn't find any results", "we didn’t find any results")),
    "substack": NoResultsMarkers(texts=("no results",)),
}


def no_results_markers(site: str) -> NoResultsMarkers:
    return NO_RESULTS.get(site, NoResultsMarkers())


def takes_query(site: str, page_type: str) -> bool:
    """Does this page type's URL take --query (a search)?"""
    return "{query}" in get_site(site).page_types.get(page_type, "")


# The page `doctor` opens to check that the browser profile is logged in.
HOME_URL: dict[str, str] = {
    "x": "https://x.com/home",
    "reddit": "https://www.reddit.com/",
    "instagram": "https://www.instagram.com/",
    "facebook": "https://www.facebook.com/",
    "linkedin": "https://www.linkedin.com/feed/",
    "substack": "https://substack.com/home",
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
    path_rule = URL_HANDLE_PAGE_TYPES.get((site_name, page_type))
    if path_rule is not None:   # the handle is the URL itself, on the site's own domains
        url = handle.strip()
        check_url(site, url)
        parts = urlsplit(url)
        if not path_rule.match(parts.path) or parts.username or parts.password or parts.port:
            raise ValueError(f"{site_name} {page_type}: --handle must be a post URL like "
                             f"https://<publication>.substack.com/p/<slug> (got {url!r})")
        return url
    if (site_name, page_type) in HOST_HANDLE_PAGE_TYPES:
        if not HOST_LABEL_RE.match(handle.strip().lower()):
            raise ValueError(f"{site_name} {page_type}: --handle must be a subdomain name "
                             f"(letters, digits, hyphens), got {handle!r}")
        values["handle"] = handle.strip().lower()
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
    # Fix 33: Substack, reading and Notes, under the same rules. Publications
    # live on <name>.substack.com (and custom domains added with
    # `sites add-domain substack <domain>`). URL templates are guesses to
    # validate locally.
    Site("substack", ("substack.com",), {
        "publication": "https://{handle}.substack.com/archive?sort=new",
        "post": "{handle}",
        "notes": "https://substack.com/notes",
        "search": "https://substack.com/search/{query}?searching=all_posts",
        "profile": "https://substack.com/@{handle}",
        "notifications": "https://substack.com/activity",
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
