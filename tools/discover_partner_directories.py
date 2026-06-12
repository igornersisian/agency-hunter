"""
Vendor partner-directory discovery — free direct scraping, no Apify.

Agencies listed in a vendor's partner directory are pre-qualified on
tool alignment: an n8n expert by definition works with Igor's stack.
The durable signal is `agency_sources.channel`; `specialization` is also
set on insert as a pre-enrichment classifier hint (enrichment overwrites
that column later from site content).

Sources (entry points verified 2026-06-12):
    n8n       https://experts.n8n.io/                       PartnerPage SaaS, SSR
    zapier    https://zapier.com/partnerdirectory           PartnerPage SaaS, SSR
    airtable  https://ecosystem.airtable.com/consultants    PartnerPage SaaS, SSR
    webflow   https://webflow.com/certified-partners/browse Webflow CMS, SSR,
              seeded pagination param `?<seed>_page=N` — ALWAYS parsed from the
              page-1 pagination link (the seed rotates on site republish)
    make      https://www.make.com/en/partners-directory    hidden JSON API;
              Cloudflare rejects httpx at the TLS level, so all requests run
              as in-page fetch() inside headless Chromium; the unfiltered API
              paginates through every tier; the partner website only exists in
              the profile's RSC flight payload (request with header `RSC: 1`)
Dropped sources (verified infeasible 2026-06-12):
    retool    no public directory exists (/agencies and /partners are
              marketing pages, partners.retool.com is an internal login).
              Covered by "retool agency"/"retool developers" SERP v2 templates.
    bubble    bubble.io/agencies renders an experts-directory whose cards are
              opaque Bubble-app divs with no profile links and no external
              websites — leads route through Bubble's internal Hire/Contact
              broker. Covered by "bubble development agency" SERP v2 template.

Usage:
    python tools/discover_partner_directories.py --source n8n --dry-run --max 5
    python tools/discover_partner_directories.py --source all
"""

from __future__ import annotations

import re
import json
import time
import argparse
import logging
from urllib.parse import urlparse, urljoin

import httpx
from selectolax.parser import HTMLParser
from dotenv import load_dotenv

from common.domain_utils import canonical_domain, is_directory_domain
from common.persist import persist_candidates, record_discovery_run

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

TIMEOUT = 30
DEFAULT_DELAY = 0.5

# PartnerPage SaaS powers n8n, Zapier and Airtable directories with
# identical markup: `?page=N` pagination, "Page X of Y" text, profile
# links under a /partner/ prefix, website behind a[data-test-website-button].
PARTNERPAGE_SOURCES = {
    "n8n": {
        "base": "https://experts.n8n.io",
        "listing": "/",
        "profile_prefix": "/partner/",
        "channel": "n8n_partners",
        "specialization": "n8n",
    },
    "zapier": {
        "base": "https://zapier.com",
        "listing": "/partnerdirectory",
        "profile_prefix": "/partnerdirectory/partner/",
        "channel": "zapier_partners",
        "specialization": "zapier",
    },
    "airtable": {
        "base": "https://ecosystem.airtable.com",
        "listing": "/consultants",
        "profile_prefix": "/consultants/partner/",
        "channel": "airtable_partners",
        "specialization": "airtable",
    },
}

WEBFLOW_BROWSE = "https://webflow.com/certified-partners/browse"
MAKE_BASE = "https://www.make.com"
MAKE_LISTING = f"{MAKE_BASE}/en/partners-directory"

# Boilerplate domains that appear in Make RSC payloads / profile pages but
# are never the partner's own site. Social networks double as a filter for
# Bubble/PartnerPage profile links.
_BOILERPLATE_DOMAINS = {
    "make.com", "celonis.com", "facebook.com", "twitter.com", "x.com",
    "linkedin.com", "instagram.com", "youtube.com", "tiktok.com",
    "ctfassets.net", "contentful.com", "rudderstack.com", "cookielaw.org",
    "onetrust.com", "google.com", "gstatic.com", "googleapis.com",
    "googletagmanager.com", "wikipedia.org", "vimeo.com", "calendly.com",
    "hubspot.com", "typeform.com", "apple.com", "schema.org", "w3.org",
}

_COUNTRY_TO_ISO2 = {
    # NB: no bare "us" alias — it would match "contact us" in prose
    "united states": "US", "usa": "US", "america": "US",
    "canada": "CA", "united kingdom": "GB", "uk": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "ireland": "IE", "germany": "DE",
    "deutschland": "DE", "austria": "AT", "österreich": "AT",
    "switzerland": "CH", "schweiz": "CH", "suisse": "CH",
    "netherlands": "NL", "nederland": "NL", "holland": "NL",
    "sweden": "SE", "sverige": "SE", "norway": "NO", "norge": "NO",
    "denmark": "DK", "danmark": "DK", "finland": "FI", "suomi": "FI",
    "australia": "AU", "new zealand": "NZ", "singapore": "SG",
    "israel": "IL", "united arab emirates": "AE", "uae": "AE",
    "dubai": "AE", "south africa": "ZA", "belgium": "BE", "belgique": "BE",
    "belgië": "BE", "luxembourg": "LU", "estonia": "EE", "eesti": "EE",
    "poland": "PL", "polska": "PL", "spain": "ES", "españa": "ES",
    "portugal": "PT", "mexico": "MX", "méxico": "MX", "colombia": "CO",
    "argentina": "AR", "brazil": "BR", "brasil": "BR", "uruguay": "UY",
    "france": "FR", "italy": "IT", "italia": "IT", "czech republic": "CZ",
    "czechia": "CZ", "romania": "RO", "ukraine": "UA", "india": "IN",
    "philippines": "PH", "pakistan": "PK", "indonesia": "ID",
    "nigeria": "NG", "egypt": "EG", "turkey": "TR", "japan": "JP",
    "croatia": "HR", "serbia": "RS", "bulgaria": "BG", "greece": "GR",
    "hungary": "HU", "slovakia": "SK", "slovenia": "SI", "latvia": "LV",
    "lithuania": "LT", "chile": "CL", "peru": "PE", "ecuador": "EC",
}


def _country_from_text(text) -> str | None:
    """Best-effort: find a known country name in free-form location text
    (str or any JSON-ish structure — Make's address field is sometimes a dict).

    Multi-location agencies list HQ first ("Locations United States, ...
    United Kingdom, ...") — take the EARLIEST match in the text, longest
    name winning ties (so "united arab emirates" beats a hypothetical
    shorter overlap at the same position)."""
    if not text:
        return None
    low = str(text).lower()
    best: tuple[int, int, str] | None = None  # (pos, -len, iso)
    for name, iso in _COUNTRY_TO_ISO2.items():
        m = re.search(rf"\b{re.escape(name)}\b", low)
        if m is None:
            continue
        key = (m.start(), -len(name), iso)
        if best is None or key < best:
            best = key
    return best[2] if best else None


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT,
                 "Accept": "text/html,application/xhtml+xml,*/*"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )


def _get(client: httpx.Client, url: str) -> str | None:
    # Local DNS is flaky (intermittent getaddrinfo failures observed) —
    # one short-delay retry rescues most transient misses.
    for attempt in (1, 2):
        try:
            resp = client.get(url)
            if resp.status_code != 200:
                logger.info(f"GET {url} → {resp.status_code}")
                return None
            return resp.text
        except Exception as e:
            logger.info(f"GET {url} failed (attempt {attempt}): {e}")
            if attempt == 1:
                time.sleep(2)
    return None


def _make_candidate(cfg: dict, name: str | None, website_raw: str | None,
                    profile_url: str, location_text: str | None = None,
                    description: str | None = None,
                    extra_payload: dict | None = None) -> dict | None:
    """Normalize one directory entry to the shared CandidateRow shape.

    Returns None when the website is missing, unparseable, or points at a
    blocklisted platform domain (vendor self-links, *.webflow.io sites)."""
    if not website_raw:
        return None
    domain = canonical_domain(website_raw)
    if not domain or is_directory_domain(domain):
        return None

    parsed = urlparse(website_raw if "://" in website_raw else f"https://{website_raw}")
    netloc = parsed.netloc or domain
    scheme = parsed.scheme or "https"
    homepage = f"{scheme}://{netloc}/"

    return {
        "id": domain,
        "name": (name or "").strip() or domain,
        "domain": domain,
        "website_url": homepage,
        "country": _country_from_text(location_text) or "",
        "short_description": (description or "").strip()[:500],
        "source_channel": cfg["channel"],
        "source_url": profile_url,
        "raw_payload": {
            "vendor": cfg["specialization"],
            "profile_url": profile_url,
            "website_raw": website_raw,
            "location_text": location_text,
            **(extra_payload or {}),
        },
        "specialization": [cfg["specialization"]],
    }


def _dedup_by_domain(candidates: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for c in candidates:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        unique.append(c)
    return unique


# ---------------------------------------------------------------------------
# PartnerPage (n8n / zapier / airtable)
# ---------------------------------------------------------------------------

_PAGE_OF_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)


def _pp_meta(tree: HTMLParser, prop: str) -> str | None:
    node = tree.css_first(f'meta[property="{prop}"]') or tree.css_first(f'meta[name="{prop}"]')
    return node.attributes.get("content") if node else None


def scrape_partnerpage(cfg: dict, max_n: int | None = None,
                       delay: float = DEFAULT_DELAY) -> list[dict]:
    base, listing, prefix = cfg["base"], cfg["listing"], cfg["profile_prefix"]
    candidates: list[dict] = []
    skipped_no_website = 0

    with _client() as client:
        # 1. Walk listing pages, collect profile slugs
        profile_urls: list[str] = []
        seen_paths: set[str] = set()
        page, total_pages = 1, 1
        while page <= total_pages:
            html = _get(client, f"{base}{listing}?page={page}")
            if html is None:
                break
            tree = HTMLParser(html)
            m = _PAGE_OF_RE.search(tree.body.text() if tree.body else html)
            if m:
                total_pages = int(m.group(2))
            for a in tree.css("a"):
                href = a.attributes.get("href") or ""
                path = urlparse(href).path if "://" in href else href
                if path.startswith(prefix) and path not in seen_paths:
                    seen_paths.add(path)
                    profile_urls.append(urljoin(base, path))
            logger.info(f"[{cfg['channel']}] listing page {page}/{total_pages} "
                        f"→ {len(profile_urls)} profiles so far")
            page += 1
            time.sleep(delay)

        if max_n is not None:
            profile_urls = profile_urls[:max_n]

        # 2. Visit each profile, extract the agency's own website
        for i, url in enumerate(profile_urls, 1):
            html = _get(client, url)
            if html is None:
                continue
            tree = HTMLParser(html)
            btn = tree.css_first("a[data-test-website-button]")
            website = btn.attributes.get("href") if btn else None
            if not website:
                skipped_no_website += 1
                continue

            h1 = tree.css_first("h1")
            name = (h1.text(strip=True) if h1 else None) or _pp_meta(tree, "og:title")
            description = _pp_meta(tree, "og:description")
            # The profile body has a "Locations <Country, Region, City>"
            # block — a short snippet around it beats scanning the whole
            # page (nav/footer mention countries too) and keeps raw_payload
            # small.
            body_text = tree.body.text(separator=" ", strip=True) if tree.body else ""
            loc_idx = body_text.find("Locations ")
            location_text = body_text[loc_idx:loc_idx + 120] if loc_idx != -1 else None

            cand = _make_candidate(cfg, name, website, url,
                                   location_text=location_text,
                                   description=description)
            if cand:
                candidates.append(cand)
            if i % 25 == 0:
                logger.info(f"[{cfg['channel']}] profiles {i}/{len(profile_urls)}, "
                            f"{len(candidates)} candidates")
            time.sleep(delay)

    logger.info(f"[{cfg['channel']}] done: {len(candidates)} candidates, "
                f"{skipped_no_website} profiles without website button")
    return candidates


# ---------------------------------------------------------------------------
# Webflow certified partners
# ---------------------------------------------------------------------------

_WEBFLOW_CFG = {"channel": "webflow_partners", "specialization": "webflow"}
_WEBFLOW_SEED_RE = re.compile(r"\?(\w+)_page=\d+")
_WEBFLOW_WEBSITE_RE = re.compile(r'"website"\s*:\s*"(https?://[^"]+)"')
_WEBFLOW_PROFILE_RE = re.compile(r"^/@[\w\-.]+$")


def scrape_webflow(max_n: int | None = None, delay: float = DEFAULT_DELAY) -> list[dict]:
    cfg = _WEBFLOW_CFG
    candidates: list[dict] = []
    skipped_no_website = 0

    with _client() as client:
        html = _get(client, WEBFLOW_BROWSE)
        if html is None:
            raise RuntimeError("Webflow browse page unreachable")

        # The pagination query param is seeded (?5b5090bd_page=N) and the
        # seed rotates when the site is republished — parse it from the
        # page-1 pagination links instead of hardcoding.
        seed_match = _WEBFLOW_SEED_RE.search(html)
        if not seed_match:
            raise RuntimeError("Webflow pagination seed not found — markup changed?")
        seed = seed_match.group(1)
        logger.info(f"[{cfg['channel']}] pagination seed={seed}")

        profile_urls: list[str] = []
        seen_paths: set[str] = set()

        def _collect_profiles(page_html: str) -> int:
            added = 0
            tree = HTMLParser(page_html)
            for a in tree.css("a"):
                href = a.attributes.get("href") or ""
                path = urlparse(href).path if "://" in href else href
                if _WEBFLOW_PROFILE_RE.match(path) and path not in seen_paths:
                    seen_paths.add(path)
                    profile_urls.append(f"https://webflow.com{path}")
                    added += 1
            return added

        # Pagination links only expose neighbouring pages, not the total —
        # walk forward until a page yields no new profiles (hard cap as a
        # runaway guard; ~177 pages × 10 partners as of 2026-06).
        _collect_profiles(html)
        page = 2
        while page <= 400:
            if max_n is not None and len(profile_urls) >= max_n:
                break
            page_html = _get(client, f"{WEBFLOW_BROWSE}?{seed}_page={page}")
            if page_html is None:
                break
            if _collect_profiles(page_html) == 0:
                break
            if page % 10 == 0:
                logger.info(f"[{cfg['channel']}] listing page {page} "
                            f"→ {len(profile_urls)} profiles")
            page += 1
            time.sleep(delay)

        if max_n is not None:
            profile_urls = profile_urls[:max_n]

        for i, url in enumerate(profile_urls, 1):
            page_html = _get(client, url)
            if page_html is None:
                continue
            m = _WEBFLOW_WEBSITE_RE.search(page_html)
            website = m.group(1) if m else None
            if not website:
                skipped_no_website += 1
                continue
            tree = HTMLParser(page_html)
            name = _pp_meta(tree, "og:title")
            if name:
                # og:title is usually "Name | Webflow" — keep the name part
                name = name.split("|")[0].strip()
            description = _pp_meta(tree, "og:description")
            loc_m = re.search(r'"location"\s*:\s*"([^"]+)"', page_html)
            # Fallback: og:description starts with
            # "Name (slug) | service provider from Denver, CO, United States on Webflow."
            location_text = loc_m.group(1) if loc_m else None
            if not location_text and description:
                from_m = re.search(r"service provider from ([^.|]+?) on Webflow", description)
                location_text = from_m.group(1) if from_m else None

            cand = _make_candidate(cfg, name, website, url,
                                   location_text=location_text,
                                   description=description)
            if cand:
                candidates.append(cand)
            if i % 50 == 0:
                logger.info(f"[{cfg['channel']}] profiles {i}/{len(profile_urls)}, "
                            f"{len(candidates)} candidates")
            time.sleep(delay)

    logger.info(f"[{cfg['channel']}] done: {len(candidates)} candidates, "
                f"{skipped_no_website} without website")
    return candidates


# ---------------------------------------------------------------------------
# Make.com partners
# ---------------------------------------------------------------------------

_MAKE_CFG = {"channel": "make_partners", "specialization": "make"}
_MAKE_URL_RE = re.compile(r'https?://[^\s"\\\'<>)\]]+')


def _make_extract_website(rsc_text: str) -> str | None:
    """Pull the partner's external website out of an RSC flight payload.

    The payload references dozens of URLs; everything that isn't
    boilerplate (make.com itself, CDNs, socials, analytics) is assumed to
    be the partner site. Returns the most frequent survivor."""
    counts: dict[str, int] = {}
    first_seen: dict[str, str] = {}
    for raw in _MAKE_URL_RE.findall(rsc_text):
        raw = raw.rstrip("\\/.,;")
        dom = canonical_domain(raw)
        if not dom or dom in _BOILERPLATE_DOMAINS or is_directory_domain(dom):
            continue
        counts[dom] = counts.get(dom, 0) + 1
        first_seen.setdefault(dom, raw)
    if not counts:
        return None
    best = max(counts, key=lambda d: counts[d])
    return first_seen[best]


def scrape_make(max_n: int | None = None, delay: float = DEFAULT_DELAY) -> list[dict]:
    """Make blocks plain httpx at the TLS-fingerprint level (Cloudflare 403
    even on page GETs), so every request runs as an in-page fetch() inside
    headless Chromium — browser TLS + cookies pass the challenge."""
    from playwright.sync_api import sync_playwright  # lazy

    cfg = _MAKE_CFG
    candidates: list[dict] = []
    skipped_no_website = 0

    _FETCH_JSON_JS = """
        async (url) => {
            const r = await fetch(url, {headers: {'Accept': 'application/json'}});
            if (!r.ok) return {__status: r.status};
            return await r.json();
        }
    """
    _FETCH_RSC_JS = """
        async (url) => {
            const r = await fetch(url, {headers: {'RSC': '1'}});
            if (!r.ok) return '';
            return await r.text();
        }
    """

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        try:
            page.goto(MAKE_LISTING, timeout=60_000, wait_until="domcontentloaded")
            page.wait_for_timeout(5_000)  # let the Cloudflare clearance cookie land

            def api(params: dict) -> dict | None:
                qs = "&".join(f"{k}={v}" for k, v in params.items())
                try:
                    data = page.evaluate(_FETCH_JSON_JS,
                                         f"{MAKE_LISTING}/api/get-partners?{qs}")
                except Exception as e:
                    logger.info(f"make API evaluate failed: {e}")
                    return None
                if isinstance(data, dict) and data.get("__status"):
                    logger.info(f"make API → {data['__status']}")
                    return None
                return data if isinstance(data, dict) else None

            # The UNFILTERED call returns every tier (533 partners as of
            # 2026-06: 70 paid + 463 certified) with plain offset pagination.
            # (A tiers filter must be repeated params — tiers=gold&tiers=silver,
            # comma-joined values are a 400 — but we don't need it at all.)
            partners: dict[str, dict] = {}
            offset, limit = 0, 50
            while True:
                data = api({"limit": limit, "offset": offset})
                if data is None:
                    raise RuntimeError("make get-partners API blocked even via "
                                       "browser fetch — rerun later")
                batch = data.get("partners") or []
                for partner in batch:
                    if partner.get("slug"):
                        partners.setdefault(partner["slug"], partner)
                total = data.get("totalPartners") or 0
                offset += limit
                if not batch or offset >= total:
                    break
                if max_n is not None and len(partners) >= max_n:
                    break
                time.sleep(delay)
            logger.info(f"[{cfg['channel']}] listing: {len(partners)} partners")

            slugs = list(partners)
            if max_n is not None:
                slugs = slugs[:max_n]

            # 3. Per-partner RSC fetch — the website URL only lives there
            for i, slug in enumerate(slugs, 1):
                meta = partners[slug]
                profile_url = f"{MAKE_LISTING}/{slug}"
                try:
                    rsc_text = page.evaluate(_FETCH_RSC_JS, profile_url) or ""
                except Exception as e:
                    logger.info(f"make RSC fetch failed for {slug}: {e}")
                    rsc_text = ""
                website = _make_extract_website(rsc_text) if rsc_text else None
                if not website:
                    skipped_no_website += 1
                    continue

                cand = _make_candidate(
                    cfg, meta.get("name"), website, profile_url,
                    location_text=meta.get("address"),
                    description=meta.get("description"),
                    extra_payload={"tiers": meta.get("tiers"), "slug": slug},
                )
                if cand:
                    candidates.append(cand)
                if i % 25 == 0:
                    logger.info(f"[{cfg['channel']}] profiles {i}/{len(slugs)}, "
                                f"{len(candidates)} candidates")
                time.sleep(delay)
        finally:
            browser.close()

    logger.info(f"[{cfg['channel']}] done: {len(candidates)} candidates, "
                f"{skipped_no_website} without extractable website")
    return candidates


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

SCRAPERS = {
    "n8n":      lambda max_n, delay: scrape_partnerpage(PARTNERPAGE_SOURCES["n8n"], max_n, delay),
    "airtable": lambda max_n, delay: scrape_partnerpage(PARTNERPAGE_SOURCES["airtable"], max_n, delay),
    "zapier":   lambda max_n, delay: scrape_partnerpage(PARTNERPAGE_SOURCES["zapier"], max_n, delay),
    "make":     scrape_make,
    "webflow":  scrape_webflow,
}

CHANNELS = {
    "n8n": "n8n_partners",
    "airtable": "airtable_partners",
    "zapier": "zapier_partners",
    "make": "make_partners",
    "webflow": "webflow_partners",
}

# Cheap-first order for --source all
SOURCE_ORDER = ["n8n", "airtable", "zapier", "make", "webflow"]


def run_source(source: str, max_n: int | None = None, dry_run: bool = False,
               delay: float = DEFAULT_DELAY) -> tuple[int, int]:
    """Scrape one source and persist. Returns (candidates_found, new_agencies)."""
    channel = CHANNELS[source]
    try:
        candidates = SCRAPERS[source](max_n, delay)
    except Exception as e:
        logger.error(f"[{channel}] scrape failed: {e}")
        record_discovery_run(channel, "error", 0, 0, error=str(e))
        raise

    candidates = _dedup_by_domain(candidates)
    logger.info(f"[{channel}] {len(candidates)} unique candidates")

    if dry_run:
        print(json.dumps(candidates[:20], ensure_ascii=False, indent=2))
        record_discovery_run(channel, "success", len(candidates), 0,
                             metadata={"dry_run": True, "max": max_n})
        return len(candidates), 0

    new_count, source_rows = persist_candidates(candidates)
    logger.info(f"[{channel}] persisted: {new_count} new agencies, {source_rows} source rows")
    record_discovery_run(channel, "success", len(candidates), new_count,
                         metadata={"max": max_n})
    return len(candidates), new_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Vendor partner-directory discovery")
    parser.add_argument("--source", required=True,
                        choices=[*SOURCE_ORDER, "all"])
    parser.add_argument("--max", type=int, help="Cap profiles fetched per source (smoke tests)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print candidates, don't write agencies to DB")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"Politeness sleep between requests (default {DEFAULT_DELAY}s)")
    args = parser.parse_args()

    sources = SOURCE_ORDER if args.source == "all" else [args.source]
    totals = {"found": 0, "new": 0}
    for source in sources:
        try:
            found, new = run_source(source, max_n=args.max,
                                    dry_run=args.dry_run, delay=args.delay)
        except Exception:
            # error already logged + recorded; isolate per-source failures
            continue
        totals["found"] += found
        totals["new"] += new

    logger.info(f"Done. {totals['found']} candidates, {totals['new']} new agencies.")


if __name__ == "__main__":
    main()
