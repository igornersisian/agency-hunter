"""
Phase 4 — fit classification.

Chain-of-thought first, score second. The LLM must output, in strict
order:
    1. pros           — specific positive signals referencing concrete
                        evidence from enriched_data
    2. cons           — specific HARD DISQUALIFIERS only (stack mismatch,
                        wrong industry, enterprise procurement, explicit
                        offline-only, dead site, off-target geography)
    3. fit_summary    — 1-2 sentence synthesis
    4. sub-scores     — tool_alignment, service_match, market_fit,
                        engagement_fit

**Scoring**: sub-scores are the only source of truth. No server-side
penalty from cons — the LLM already reflects cons in the sub-scores
(e.g. stack mismatch → tool_alignment=0). Double-penalty was dropped
after we observed it crushed borderline-good agencies (see
`workflows/classify_agency.md`).

    total  = tool_alignment + service_match + market_fit + engagement_fit
           = clamped [0, 100]

Igor's threshold is stored on the profile (`agency_fit_threshold`,
default 70). Scoring this high means ≥2 concrete tool/service matches
AND no hard disqualifier.

Non-cons (explicitly NOT concerns): team size, missing case studies,
missing LinkedIn, missing founded year. The prior rubric penalized
these and rejected real matches — Igor calibrated that out.
"""

from __future__ import annotations

import argparse
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from common.supabase_client import get_supabase
from common.llm import chat_completion
from common.profile import get_profile, get_agency_config

logger = logging.getLogger(__name__)

# Sub-score caps — sum to 100
_CAPS = {
    "tool_alignment":  40,   # n8n/make/zapier/openai/anthropic/supabase/weweb overlap
    "service_match":   30,   # services Igor has actually shipped
    "market_fit":      15,   # target country
    "engagement_fit":  15,   # remote-friendly assumed; drop only on explicit offline-only
}

# Hard country blacklist — auto-reject regardless of fit_score.
# Currently just India, where the pipeline keeps surfacing offshore dev
# shops that would compete with Igor rather than hire him. Expand as
# needed — this is a blunt tool, use it only for whole-country misfits.
_COUNTRY_BLACKLIST = {"IN"}

STALE_CLASSIFYING_MINUTES = 15  # rows stuck in status='classifying' longer than this get rescued
DEFAULT_WORKERS = 4


def is_country_blacklisted(country: str | None) -> bool:
    """True if the agency's country is auto-rejected regardless of fit."""
    return bool(country) and country.upper() in _COUNTRY_BLACKLIST


def _build_system_prompt(profile_text: str) -> str:
    return (
        "You are a strict agency-fit evaluator for a solo contractor (Igor) "
        "looking for REMOTE contract work. You will be shown an "
        "AI-automation agency's enriched public profile plus Igor's "
        "resume-derived profile. Evaluate whether Igor could realistically "
        "get paid remote contract work from this agency.\n\n"

        "WHO IGOR IS:\n"
        "- Solo remote contractor. No-code / AI-assisted builder "
        "(\"vibecoder\"), NOT a hand-written-code engineer.\n"
        "- Stack: n8n, make.com, zapier, openai, anthropic/claude, "
        "supabase, weweb, webflow, retool, bubble, airtable, langchain, "
        "Claude Code, Replit.\n"
        "- Ships: multi-agent AI, RAG systems, workflow automation, "
        "full-stack web apps, self-hosted infra.\n"
        "- Does NOT compete for: C++/Java/Rust/.NET backends, native "
        "iOS/Android, enterprise Python/Django, traditional SDLC work.\n\n"

        "GROUND RULE — concrete evidence only:\n"
        "Every pro and every con MUST reference a specific thing from "
        "the website text (a service name, a tool mention, a case study, "
        "a location statement, a hiring/engagement line). Never invent "
        "facts. If the text is thin, output fewer pros/cons — do "
        "NOT pad with generic statements.\n\n"

        "HARD DISQUALIFIERS — list these in `cons` ONLY if you find "
        "specific evidence. If none apply, `cons` is an empty list.\n"
        "  1. STACK MISMATCH — agency builds on hand-written code "
        "(C++/Java/Rust/native mobile/enterprise backends) with "
        "traditional SDLC. Igor cannot compete for this work.\n"
        "  2. NOT AN AI/AUTOMATION AGENCY — the agency is a pure design "
        "shop, SEO-only, PR firm, staffing/recruiting firm, cybersecurity "
        "consultancy, or pure devops house. AI/automation is not their "
        "business.\n"
        "  3. ENTERPRISE PROCUREMENT ONLY — explicit Fortune 500 language, "
        "RFP-driven, 6-month sales cycles, named F500 logos only. Solo "
        "contractors don't pass procurement there.\n"
        "  4. EXPLICITLY OFFLINE-ONLY — agency literally states that work "
        "happens at their office, 'in-person collaboration required', "
        "'come to our studio', 'on-site only'. DEFAULT assumption is "
        "REMOTE — only flag this con when there's explicit offline-only "
        "language. The ABSENCE of remote-friendly claims is NOT a con.\n"
        "  5. DEAD / ABANDONED — last case study or blog post from 2021 "
        "or earlier, copyright footer older than 2 years, 'coming soon' "
        "placeholder, or broken site structure.\n"
        "  6. OFF-TARGET GEOGRAPHY — agency primarily serves a market "
        "outside Igor's target country list AND the site is region-locked "
        "(non-English, local language only).\n"
        "  7. INFO PRODUCT / COURSE / COACHING BUSINESS — the 'agency' is "
        "actually selling a course, curriculum, coaching program, "
        "accelerator, community membership, or 'AI business in a box' to "
        "aspiring founders. Tell-tale signals: services/offers include "
        "'coaching', 'classroom', 'curriculum', 'mentorship', '1-on-1 "
        "calls', 'success manager', 'accountability', 'community of "
        "founders', 'students', 'members worldwide', 'book a strategy "
        "call', or promises of 'build an N-figure AI business / agency'. "
        "These businesses TEACH people how to become AI agency founders — "
        "they compete WITH Igor for the same clients, they do NOT hire "
        "solo contractors. If this disqualifier applies, set "
        "tool_alignment=0 AND service_match=0 regardless of what tools "
        "or service keywords the site lists.\n"
        "  8. DIRECTORY / MARKETPLACE / AGGREGATOR — the site is a "
        "directory listing agencies, a freelancer marketplace, a platform "
        "that matches businesses with agencies, or an aggregator of "
        "automation tools/services. Tell-tale signals: 'find agencies', "
        "'browse providers', 'connect with experts', 'hire freelancers', "
        "'top N agencies', 'compare agencies', 'agency directory', "
        "'marketplace'. These sites do NOT hire contractors — they list "
        "or match them. If this applies, set all sub-scores to 0.\n"
        "  9. PRODUCT COMPANY (NOT AN AGENCY) — the primary business is "
        "a SaaS product, platform, or software tool — NOT a services "
        "agency. They sell licenses/subscriptions to their own product, "
        "not consulting or implementation services. Examples: ABBYY "
        "(document AI product), Glide (app builder platform), Etsy "
        "(e-commerce marketplace). Even if they mention integrations "
        "with n8n/make/zapier, a product company does NOT hire solo "
        "contractors for client delivery. If this applies, cap total "
        "score at 30 maximum.\n"
        "  10. SOLO PRACTITIONER / PERSONAL BRAND — the 'agency' is "
        "actually one individual freelancer or consultant operating "
        "under their own name, not a company that could hire contractors. "
        "Tell-tale signals: domain is a person's name "
        "(e.g. johnsmith.com, tendaigumunyu.co.za); contact is a personal "
        "gmail/outlook/yahoo address rather than a branded one "
        "(name@domain.com); site is a personal blog or portfolio "
        "(/blog/... posts authored by one person, /about-me page); "
        "copy is in first-person singular ('I help clients...', 'my "
        "process', 'reach out to me') rather than plural ('we', 'our "
        "team'); no team/about-us page listing multiple people; LinkedIn "
        "links to one personal profile. These individuals COMPETE WITH "
        "Igor for the same contract work — they do NOT hire him. If "
        "this disqualifier applies, set tool_alignment=0 AND "
        "service_match=0 regardless of what tools or services the site "
        "lists. A legitimate small agency with 2-5 people is fine — "
        "this only applies to true solo operators.\n\n"

        "NOT CONS — do NOT list these. They are red herrings and the "
        "previous rubric wrongly penalized agencies for them:\n"
        "  - Team size of any kind. 5-person shops and 200-person agencies "
        "are both fine. Igor targets project-level overflow, not headcount.\n"
        "  - Missing case studies on the site — many agencies keep work "
        "under NDA.\n"
        "  - Missing LinkedIn for team members — privacy preference.\n"
        "  - Missing founded year, missing founder bios.\n"
        "  - Generic corporate marketing language.\n"
        "  - Only one or two services visible — many agencies lead with one.\n"
        "  - ABSENCE of explicit 'we hire freelancers' language — most "
        "agencies never say it even when they do subcontract.\n\n"

        "OUTPUT ORDER (chain-of-thought):\n"
        "STEP 1 — `pros`: concrete reasons Igor fits. Each references "
        "specific evidence. Examples:\n"
        "  - \"offers n8n consulting (services page) — matches Igor's "
        "primary tool\"\n"
        "  - \"case study 'RAG chatbot for fintech' — Igor built a "
        "multi-agent RAG consultant\"\n"
        "  - \"based in AU — in Igor's target country list\"\n\n"

        "STEP 2 — `cons`: HARD DISQUALIFIERS ONLY (from the list above), "
        "with specific evidence. If none apply, return []. Examples:\n"
        "  - \"builds native iOS apps in Swift — stack mismatch, Igor does "
        "not do native mobile\"\n"
        "  - \"services listed are SEO, PPC, content marketing — not an "
        "AI/automation agency\"\n"
        "  - \"team page states 'all work happens at our London office' — "
        "explicitly offline-only\"\n"
        "  - \"last blog post from 2020, team page empty — site appears "
        "abandoned\"\n"
        "  - \"domain is founder's personal name, gmail contact, "
        "first-person 'I help' copy — solo practitioner, not a hiring "
        "agency\"\n\n"

        "STEP 3 — `fit_summary`: 1-2 sentences, addressed to Igor "
        "directly ('you/your').\n\n"

        "STEP 4 — sub-scores (ground truth, reflect pros/cons honestly):\n"
        "  tool_alignment (0-40): overlap with Igor's stack. Scoring:\n"
        "    - 10 per clearly-stated tool match (n8n, make, zapier, openai, "
        "anthropic, claude, supabase, weweb, webflow, retool, bubble, "
        "airtable, langchain). Cap 40.\n"
        "    - IMPLIED ALIGNMENT (10-15) when the agency offers AI "
        "automation / workflow automation / no-code / low-code services "
        "AND there is NO evidence of traditional hand-written-code "
        "approach (no Java/.NET/C++/native mobile stack listed). These "
        "agencies almost certainly use tools from Igor's stack even if "
        "they don't list them on their marketing site. Award 10-15 "
        "implied alignment in this case, ON TOP of any explicit matches.\n"
        "    - 0 only if the agency explicitly works with traditional "
        "hand-written code stacks OR there is no AI/automation signal.\n"
        "  service_match (0-30): do stated services map to what Igor has "
        "shipped (RAG, multi-agent AI, workflow automation, AI "
        "integrations, no-code full-stack)? Score 0 for hardcore dev "
        "consulting, design-only, staffing, or SEO-only shops. 30 for "
        "pure AI/automation agencies. 15-25 for mixed.\n"
        "  market_fit (0-15): 15 if in Igor's target countries. 5 if "
        "English-speaking but off-list (e.g. IN, PH, SG). 0 otherwise.\n"
        "  engagement_fit (0-15): DEFAULT 15 (remote-friendly assumed). "
        "Drop to 5 only if the site has vague in-office language that "
        "isn't fully explicit. Drop to 0 ONLY if the site explicitly "
        "requires on-site/in-person work (matches hard disqualifier 4). "
        "Do NOT drop this just because remote work isn't explicitly "
        "mentioned — assume remote unless contradicted.\n\n"

        f"IGOR'S PROFILE:\n{profile_text}\n\n"

        "Return ONLY this JSON (pros/cons FIRST, scores LAST):\n"
        "{\n"
        '  "pros": ["..."],\n'
        '  "cons": ["..."],\n'
        '  "fit_summary": "<1-2 sentences addressed to Igor>",\n'
        '  "tool_alignment": <int 0-40>,\n'
        '  "service_match": <int 0-30>,\n'
        '  "market_fit": <int 0-15>,\n'
        '  "engagement_fit": <int 0-15>\n'
        "}"
    )


def _clamp(val, lo: int, hi: int) -> int:
    try:
        v = int(val)
    except (TypeError, ValueError):
        v = 0
    return max(lo, min(hi, v))


def classify_one(agency_id: str, enriched_data: dict, profile: dict,
                 target_countries: list[str],
                 raw_website_text: str = "") -> dict:
    """Score a single agency. Uses raw website text as primary input for
    classification (full context), with enriched_data as structured supplement.

    Returns a dict with the final computed score and breakdown.
    """
    # Inject target countries into the profile view the model sees
    profile_view = dict(profile)
    profile_view["target_countries"] = target_countries
    profile_text = json.dumps(profile_view, ensure_ascii=False, indent=2)

    # Build the user message: raw text is the primary source, enriched JSON is supplementary
    if raw_website_text:
        user_content = (
            f"AGENCY WEBSITE TEXT (raw scraped pages):\n{raw_website_text}\n\n"
            f"STRUCTURED EXTRACT (may be incomplete):\n"
            f"{json.dumps(enriched_data, ensure_ascii=False, indent=2)}"
        )
    else:
        # Fallback for agencies enriched before raw_website_text was saved
        user_content = (
            f"AGENCY ENRICHED DATA (structured extract only — raw text unavailable):\n"
            f"{json.dumps(enriched_data, ensure_ascii=False, indent=2)}"
        )

    response = chat_completion(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _build_system_prompt(profile_text)},
            {"role": "user", "content": user_content},
        ],
    )
    result = json.loads(response.choices[0].message.content)

    pros = [str(p) for p in (result.get("pros") or []) if p]
    cons = [str(c) for c in (result.get("cons") or []) if c]
    fit_summary = result.get("fit_summary", "")

    # Clamp sub-scores to valid ranges — sub-scores are the sole source of truth
    tool_alignment = _clamp(result.get("tool_alignment"),  0, _CAPS["tool_alignment"])
    service_match  = _clamp(result.get("service_match"),   0, _CAPS["service_match"])
    market_fit     = _clamp(result.get("market_fit"),      0, _CAPS["market_fit"])
    engagement_fit = _clamp(result.get("engagement_fit"),  0, _CAPS["engagement_fit"])

    total = tool_alignment + service_match + market_fit + engagement_fit
    total = min(max(total, 0), 100)

    breakdown = {
        "pros": pros,
        "cons": cons,
        "sub_scores": {
            "tool_alignment": tool_alignment,
            "service_match":  service_match,
            "market_fit":     market_fit,
            "engagement_fit": engagement_fit,
        },
        "total": total,
    }

    return {
        "fit_score": total,
        "fit_reasoning": fit_summary,
        "fit_breakdown": breakdown,
        "flagged_issues": cons,
    }


def _rescue_stale_classifying(sb) -> int:
    """Flip rows stuck in status='classifying' back to 'enriched'.

    Same pattern as enrich's zombie rescue — protects against a crash
    between the in-progress flip and the final update.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_CLASSIFYING_MINUTES)).isoformat()
    res = (
        sb.table("agency_agencies")
        .update({"status": "enriched", "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("status", "classifying")
        .lt("updated_at", cutoff)
        .execute()
    )
    n = len(res.data or [])
    if n:
        logger.warning(f"Rescued {n} stale 'classifying' rows back to 'enriched'")
    return n


def _classify_worker(row: dict, profile: dict, target_countries: list[str],
                     threshold: int) -> bool:
    """Classify a single row — designed to run inside a thread pool."""
    sb = get_supabase()

    # Country blacklist — skip the LLM call entirely, auto-reject
    if is_country_blacklisted(row.get("country")):
        sb.table("agency_agencies").update({
            "status": "rejected",
            "fit_score": 0,
            "fit_reasoning": f"Auto-rejected: country {row.get('country')} is blacklisted.",
            "fit_breakdown": {"auto_reject": "country_blacklist", "country": row.get("country")},
            "last_classified_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", row["id"]).execute()
        logger.info(f"Auto-rejected {row['id']}: country {row.get('country')} blacklisted")
        return True

    sb.table("agency_agencies").update({"status": "classifying"}).eq("id", row["id"]).execute()
    try:
        result = classify_one(
            row["id"], row.get("enriched_data") or {}, profile, target_countries,
            raw_website_text=row.get("raw_website_text") or "",
        )
        new_status = "qualified" if result["fit_score"] >= threshold else "rejected"

        sb.table("agency_agencies").update({
            **result,
            "status": new_status,
            "last_classified_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", row["id"]).execute()
        logger.info(f"Classified {row['id']}: {result['fit_score']} -> {new_status}")
        return True
    except Exception as e:
        logger.error(f"Classification failed for {row['id']}: {e}")
        sb.table("agency_agencies").update({
            "status": "classify_failed",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", row["id"]).execute()
        return False


def run_batch(limit: int = 20, dry_run: bool = False, workers: int = DEFAULT_WORKERS) -> int:
    """Classify up to `limit` agencies currently in `status='enriched'`.
    Uses `workers` parallel threads for faster throughput.
    """
    profile = get_profile() or {}
    cfg = get_agency_config()
    threshold = cfg["agency_fit_threshold"]
    target_countries = cfg["agency_target_countries"]

    sb = get_supabase()
    _rescue_stale_classifying(sb)
    rows = (
        sb.table("agency_agencies")
        .select("id,enriched_data,country,raw_website_text")
        .eq("status", "enriched")
        .limit(limit)
        .execute()
        .data
        or []
    )

    if dry_run:
        logger.info(f"[dry-run] would classify {len(rows)} rows (threshold={threshold})")
        for r in rows[:10]:
            bl = " [COUNTRY-BLACKLISTED]" if is_country_blacklisted(r.get("country")) else ""
            logger.info(f"  - {r['id']} (country={r.get('country')}){bl}")
        if len(rows) > 10:
            logger.info(f"  ... and {len(rows) - 10} more")
        return 0

    logger.info(f"Classifying {len(rows)} agencies, {workers} workers, threshold={threshold}")
    success = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_classify_worker, row, profile, target_countries, threshold): row
            for row in rows
        }
        for future in as_completed(futures):
            done += 1
            try:
                if future.result():
                    success += 1
            except Exception as e:
                logger.error(f"Worker exception for {futures[future]['id']}: {e}")
            if done % 20 == 0:
                logger.info(f"Progress: {done}/{len(rows)} done, {success} classified")
    return success


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Classify enriched agencies for fit.")
    ap.add_argument("--limit", type=int, default=20, help="Max rows to process (default: 20)")
    ap.add_argument("--dry-run", action="store_true", help="List rows that would be classified and exit")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"Parallel threads (default: {DEFAULT_WORKERS})")
    args = ap.parse_args()
    n = run_batch(limit=args.limit, dry_run=args.dry_run, workers=args.workers)
    if not args.dry_run:
        print(f"Classified {n} agencies.")
