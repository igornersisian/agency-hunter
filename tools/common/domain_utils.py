"""
Domain and name canonicalisation helpers.

`canonical_domain(url)` reduces any URL to its registrable root domain
using `tldextract` (handles `.co.uk`, `.com.au`, etc.). That root domain
is the primary key of `agency_agencies` and the unit of dedup.

`same_agency(a, b)` compares two candidate dicts and returns True when
they almost-certainly refer to the same agency. Uses domain equality as
the strong signal and normalised-name equality as a weak secondary.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import tldextract

# Reuse the suffix-stripping convention from
# Job-search-automation/tools/process_jobs.py._normalise_company
_COMPANY_SUFFIXES = [
    ", inc", " inc",
    ", inc.", " inc.",
    ", llc", " llc",
    ", ltd", " ltd",
    ", ltd.", " ltd.",
    ", gmbh", " gmbh",
    ", ag", " ag",
    ", pty", " pty",
    ", bv", " bv",
    ", oy", " oy",
    ", srl", " srl",
    ", sas", " sas",
    ", sarl", " sarl",
    ", limited", " limited",
    ", corporation", " corporation",
    ", corp", " corp",
    ", co", " co",
]

#
# The blacklist is a PRE-classifier filter: its job is to skip enrichment
# for domains that are OBVIOUSLY never agencies-we-might-hire, so we don't
# waste LLM/HTTP budget on them. Anything that could plausibly be an
# agency (even a wrong-fit one) should be left to the classifier.
#
# Curated from the first real pipeline run (see .tmp/audit_directory_domains.py).
#
_NON_AGENCY_DOMAINS = {
    # --- Classic directories / review aggregators ---
    "clutch.co", "goodfirms.co", "sortlist.com", "designrush.com",
    "crunchbase.com", "glassdoor.com", "g2.com", "capterra.com",
    "producthunt.com", "builtin.com",

    # --- Social / content / forum platforms ---
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "wikipedia.org", "medium.com", "reddit.com", "quora.com",
    "github.com", "tiktok.com", "pinterest.com", "dev.to",

    # --- Search engines ---
    "google.com", "bing.com", "duckduckgo.com",

    # --- Freelancer / job marketplaces ---
    # Note: all `freelancer.*` TLDs are caught by _NON_AGENCY_DOMAIN_PREFIXES below.
    "upwork.com", "fiverr.com", "toptal.com",
    "truelancer.com", "indeed.com",
    "peopleperhour.com", "malt.com", "malt.de", "kwork.com",
    "wishup.co", "ureed.com", "hireoverseas.com", "globaltize.com",
    "topaiexperts.io", "fueler.io", "codemap.io", "himalayas.app",
    "contra.com", "skool.com", "behance.net", "page.gd", "mploy.co.il",
    "freelancermap.com", "freelancermap.de", "thejob.tech",
    "busquedasit.com", "acquire.com", "rafiki.works", "torre.ai",
    "hellodarwin.com", "nomads.com", "raffall.com", "linktr.ee",
    "agencylist.com",
    # Job boards that rank for "agency in [country]" due to job postings
    "jobrapido.com", "welcometothejungle.com", "tweakers.net",
    "join.com", "careers-page.com", "trabajo.org", "expertini.com",
    "learn4good.com", "remoterocketship.com", "jobgether.com",
    "moovijob.com",

    # --- SaaS platforms & tool vendors (the platforms we're looking for
    # agencies AROUND — their own sites never are the agency) ---
    "airtable.com", "make.com", "zapier.com", "n8n.io", "bubble.io",
    "webflow.com", "retool.com", "notion.so", "monday.com", "clickup.com",
    "hubspot.com", "salesforce.com", "prestashop.com", "abbyy.com",
    "axway.com", "mrisoftware.com", "voximplant.com",
    # Tool-maker marketing pages that rank for "n8n/OpenAI/Make + country"
    # queries via landing pages targeting those keywords.
    "replit.com", "xano.com", "framer.ai", "weweb.io", "webflow.io",
    "boost.space", "needle.app", "botpress.com", "creatio.com",
    "databricks.com", "zenml.io", "pega.com", "workato.com", "okta.com",
    "zscaler.com", "zendesk.de", "qualtrics.com", "denodo.com",
    "temenos.com", "canonical.com", "opensearch.org", "algolia.com",
    "hyland.com", "m-files.com", "sas.com", "tungstenautomation.com",
    "revverdocs.com", "newgensoft.com", "progress.com",
    "intersystems.com", "processmaker.com", "vapi.ai", "cluedin.com",
    "asctechnologies.com", "webcon.com", "deepeval.com",
    "dataslayer.ai", "navan.com", "zetaglobal.com", "screendragon.com",
    "beam.ai", "futurumgroup.com", "glean.com", "informatica.com",
    "onetrust.com", "unified.to", "convex.systems", "five9.com",
    "decisions.com", "moxo.com",
    # Found polluting worldwide-mode SERPs: AI/LLM frameworks, RPA
    # vendors, ecommerce platforms, and SaaS that rank for
    # "ai integration" / "automation partner" / "chatbot" queries.
    "langchain.com", "shopify.com", "pandadoc.com", "appian.com",
    "blueprism.com", "relevanceai.com", "qloo.com",

    # --- Ecommerce marketplaces ---
    "etsy.com", "amazon.com", "ebay.com", "alibaba.com",

    # --- Enterprise brands / corporations (not agencies for hire) ---
    "fujifilm.com", "ironmountain.com", "ricoh.co.nz", "ricoh.com",
    # Fortune-500 / global consultancies that rank via thought-leadership
    # pages, not agencies-for-hire in our sense.
    "apple.com", "bcg.com", "pwc.com", "kpmg.com", "ibm.com",
    "oracle.com", "microsoft.com", "redhat.com", "udemy.com",
    "slalom.com", "insight.com", "rsmcanada.com", "cgi.com",

    # --- B2B data / lead-gen / company-search platforms ---
    "zoominfo.com", "ensun.io", "apollo.io", "rocketreach.co",

    # --- News / media / blog aggregators ---
    "martechseries.com", "techcrunch.com", "venturebeat.com",
    "entrepreneur.com", "jacobin.com", "marginalrevolution.com",
    "timesofisrael.com", "infoq.com", "marketech-apac.com",
    "marketinginasia.com", "opengovasia.com", "africatechfestival.com",
    "siliconluxembourg.lu", "indiehackers.com", "colinkeeley.com",
    "tiinside.com.br", "virtualizationreview.com", "substack.com",
    "contactcentertechnologyinsights.com", "sourceforge.net",
    "awwwards.com",

    # --- Press-release mills publishing "Best AI Agency in X" puff pieces ---
    "businesswire.com", "prnewswire.com", "openpr.com",
    "financialcontent.com", "stellarbusiness.com", "aijourn.com",
    "bestofbestreview.com",

    # --- "Top 10" listicles & directory content sites observed in real runs ---
    "automationagencies.com", "neowork.com", "reverbico.com",
    "aaaaccelerator.com", "vollna.com", "uforocks.com", "latenode.com",
    "digitalagencynetwork.com", "firstpagesage.com", "techbehemoths.com",
    "aisuperior.com", "f6s.com", "50pros.com", "themanifest.com",
    "businessfirms.co", "tracxn.com", "deepresearchglobal.com",
    "aigabrielle.com", "aiagentstore.ai", "aiagencymap.com",
    "headofagents.ai", "echoloc.ai", "theirstack.com", "getlatka.com",
    "iao.org",
    # `consultancy.<tld>` listicle network. NOT to be confused with
    # `nobleprog.*` which are real training/consulting firms.
    "consultancy.eu", "consultancy.uk", "consultancy.org",
    "consultancy.lat", "consultancy-me.com", "consultancy.co.za",

    # --- Edu / gov / non-profit that snuck through via keyword matches ---
    "service.gov.uk", "itu.int", "ucp.pt", "vinnova.se", "ai.se",
    "swissbiotech.org",
}

# Prefix rules for domain families with many TLD variants.
# Matched via `domain.startswith(prefix)`.
_NON_AGENCY_DOMAIN_PREFIXES: tuple[str, ...] = (
    # freelancer.com / freelancer.ca / .si / .es / .cz / .co.ke / .co.id / .com.co / ...
    "freelancer.",
)


def canonical_domain(url: str) -> str | None:
    """Reduce any URL to its registrable root domain.

    Examples:
        https://www.acme-automation.com/services → acme-automation.com
        http://blog.acme.co.uk/post/1            → acme.co.uk
        acme.ai                                  → acme.ai
    Returns None for URLs we cannot parse.
    """
    if not url:
        return None
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    try:
        ext = tldextract.extract(url)
    except Exception:
        return None
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}".lower()


def is_directory_domain(domain: str | None) -> bool:
    """True if the domain is a known directory/platform, not an actual agency."""
    if not domain:
        return True
    d = domain.lower()
    if d in _NON_AGENCY_DOMAINS:
        return True
    return any(d.startswith(p) for p in _NON_AGENCY_DOMAIN_PREFIXES)


def normalise_name(name: str) -> str:
    """Normalise an agency name for fuzzy comparison.

    Lowercases, strips legal suffixes (Inc, LLC, GmbH, Pty, BV, ...),
    removes punctuation, collapses whitespace.
    """
    if not name:
        return ""
    n = name.lower().strip()
    for suffix in _COMPANY_SUFFIXES:
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
            break
    n = re.sub(r"[^\w\s]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def same_agency(a: dict, b: dict) -> bool:
    """Return True if two candidates plausibly refer to the same agency.

    Strong signal: same canonical domain.
    Weak signal:  identical normalised name on unrelated domains is
    NOT enough — two different agencies can share a generic name, so we
    require the domain match. This mirrors the sibling project's
    conservative dedup stance.
    """
    da = canonical_domain(a.get("website_url") or a.get("url") or a.get("domain") or "")
    db = canonical_domain(b.get("website_url") or b.get("url") or b.get("domain") or "")
    if da and db and da == db:
        return True
    return False


def extract_hostname(url: str) -> str | None:
    """Return the full hostname (with subdomain) from a URL, or None."""
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    try:
        return urlparse(url).hostname
    except Exception:
        return None
