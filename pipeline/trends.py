"""
trends.py — Google Trends topic picker with 90-day deduplication and weighted fallback.
"""
import json
import time
import random
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

TOPICS_FILE = Path("data/topics_used.json")
WEIGHTS_FILE = Path("data/topic_weights.json")
COOLDOWN_DAYS = 90

# Evergreen topic list — emotionally-driven, high-search-intent topics across 5 pillars.
# Format: keyword = the story angle (not just a plain topic), pillar = content category.
# These are written the way a viewer types them when they're worried or motivated.
EVERGREEN_TOPICS = [
    # budgeting — pain points + "I finally figured this out" angles
    {"keyword": "how much money should I have saved by 30", "pillar": "budgeting"},
    {"keyword": "why I was broke every month even with a good salary", "pillar": "budgeting"},
    {"keyword": "how to stop living paycheck to paycheck on a high income", "pillar": "budgeting"},
    {"keyword": "I built a 6 month emergency fund in 12 months here is how", "pillar": "budgeting"},
    {"keyword": "the real reason your savings account is losing you money", "pillar": "budgeting"},
    {"keyword": "high yield savings account vs regular savings the actual math", "pillar": "budgeting"},
    {"keyword": "how to budget when your income is irregular", "pillar": "budgeting"},
    {"keyword": "the 50 30 20 rule is wrong for most people here is why", "pillar": "budgeting"},
    # debt — fear and relief angles
    {"keyword": "I paid off 28k in debt in 18 months on a 60k salary", "pillar": "debt"},
    {"keyword": "why paying minimum payments is the worst financial decision you can make", "pillar": "debt"},
    {"keyword": "should I pay off debt or invest the honest answer", "pillar": "debt"},
    {"keyword": "how to get out of credit card debt when you are already overwhelmed", "pillar": "debt"},
    {"keyword": "the credit score number that actually matters for getting a mortgage", "pillar": "debt"},
    {"keyword": "debt avalanche vs debt snowball which one actually works faster", "pillar": "debt"},
    {"keyword": "how I improved my credit score by 100 points in 6 months", "pillar": "debt"},
    {"keyword": "why your student loan payoff strategy might be costing you more money", "pillar": "debt"},
    # investing — regret, urgency, and counterintuitive insights
    {"keyword": "why I regret not investing in my 20s the actual numbers", "pillar": "investing"},
    {"keyword": "roth ira vs 401k which one should you do first", "pillar": "investing"},
    {"keyword": "what nobody tells you about your first 401k contribution", "pillar": "investing"},
    {"keyword": "how to start investing with 500 dollars the exact steps I took", "pillar": "investing"},
    {"keyword": "index funds vs picking stocks the math ends the debate", "pillar": "investing"},
    {"keyword": "the compound interest graph that will make you start investing today", "pillar": "investing"},
    {"keyword": "why I stopped trying to time the market and what I do instead", "pillar": "investing"},
    {"keyword": "what is the difference between a Roth IRA and a brokerage account", "pillar": "investing"},
    # tax — money left on the table angles
    {"keyword": "tax deductions you are probably missing as a salaried employee", "pillar": "tax"},
    {"keyword": "how to legally pay less taxes in your 20s and 30s", "pillar": "tax"},
    {"keyword": "HSA is the most underrated retirement account nobody talks about", "pillar": "tax"},
    {"keyword": "roth ira vs traditional ira which one saves you more money in taxes", "pillar": "tax"},
    {"keyword": "the tax mistake most first time investors make in a brokerage account", "pillar": "tax"},
    {"keyword": "how to avoid capital gains tax on investments legally", "pillar": "tax"},
    {"keyword": "what the standard deduction actually means for your paycheck", "pillar": "tax"},
    {"keyword": "how contributing to a 401k changes your take home pay the real numbers", "pillar": "tax"},
    # career_income — first job regret and negotiation wins
    {"keyword": "the first salary mistake that cost me 15k and how to avoid it", "pillar": "career_income"},
    {"keyword": "how to negotiate your first salary even when you have no experience", "pillar": "career_income"},
    {"keyword": "what to do with your first real paycheck the exact order", "pillar": "career_income"},
    {"keyword": "how to ask for a raise and actually get it with a script that works", "pillar": "career_income"},
    {"keyword": "why your job salary will never build wealth without this one change", "pillar": "career_income"},
    {"keyword": "realistic side hustles that made me an extra 1000 a month", "pillar": "career_income"},
    {"keyword": "how to build wealth in your 20s even if you are starting from zero", "pillar": "career_income"},
    {"keyword": "the financial order of operations I wish I had at 22", "pillar": "career_income"},
]

# pytrends category code for Finance
FINANCE_CATEGORY = 7


def _load_used_topics() -> list[dict]:
    if not TOPICS_FILE.exists():
        return []
    with TOPICS_FILE.open() as f:
        return json.load(f).get("topics", [])


def _load_weights() -> dict:
    if not WEIGHTS_FILE.exists():
        return {}
    with WEIGHTS_FILE.open() as f:
        return json.load(f).get("pillars", {})


def _is_on_cooldown(slug: str, used_topics: list[dict]) -> bool:
    today = date.today().isoformat()
    for entry in used_topics:
        if entry["slug"] == slug and entry.get("expires", "1970-01-01") > today:
            return True
    return False


def _slugify(text: str) -> str:
    return text.lower().replace(" ", "-").replace("/", "-").replace("'", "")


def _pick_weighted(candidates: list[dict], weights: dict) -> dict:
    """Pick a topic randomly, weighted by pillar weights."""
    if not candidates:
        raise RuntimeError("No candidate topics available")
    scored = [(t, weights.get(t.get("pillar", ""), 1.0)) for t in candidates]
    total = sum(w for _, w in scored)
    r = random.uniform(0, total)
    cumulative = 0.0
    for topic, weight in scored:
        cumulative += weight
        if r <= cumulative:
            return topic
    return scored[-1][0]


def _fetch_pytrends_boost(available: list[dict]) -> list[dict]:
    """
    Use pytrends interest-over-time to boost the weight of currently trending
    topics from our curated list. Returns the same list with a 'trend_score'
    added to topics that are trending this week.

    This does NOT discover new topics — it only re-ranks existing curated ones.
    """
    try:
        from pytrends.request import TrendReq
    except ImportError:
        logger.warning("pytrends not installed, skipping trend boost")
        return available

    # pytrends build_payload accepts max 5 keywords.
    # Sample randomly from the full available pool so list order doesn't bias scoring.
    sample = random.sample(available, min(5, len(available)))
    kw_list = [t["keyword"][:50] for t in sample]  # pytrends has a length limit

    for attempt in range(3):
        try:
            pt = TrendReq(hl="en-US", tz=360)
            pt.build_payload(
                kw_list=kw_list,
                cat=FINANCE_CATEGORY,
                timeframe="now 7-d",
                geo="US",
            )
            iot = pt.interest_over_time()
            if iot.empty:
                return available

            # Compute mean interest score for each sampled keyword
            scores = {}
            for kw in kw_list:
                if kw in iot.columns:
                    scores[kw] = float(iot[kw].mean())

            # Attach trend_score to matching topics
            boosted = []
            for topic in available:
                t = dict(topic)
                match = next((s for k, s in scores.items() if k == topic["keyword"][:50]), None)
                if match is not None:
                    t["trend_score"] = match
                boosted.append(t)

            logger.info("Trend boost applied to %d topics", len(scores))
            return boosted

        except Exception as exc:
            wait = 2 ** attempt + random.uniform(0, 1)
            logger.warning("pytrends boost attempt %d failed: %s — retrying in %.1fs", attempt + 1, exc, wait)
            time.sleep(wait)

    logger.warning("pytrends boost failed after 3 attempts — using unweighted list")
    return available


def pick_topic() -> dict:
    """
    Returns a topic dict: {keyword, pillar, slug}.
    Always picks from the curated EVERGREEN_TOPICS list (filtered by 90-day cooldown).
    Uses pytrends interest-over-time to boost currently trending curated topics,
    combined with optimizer pillar weights.
    """
    used = _load_used_topics()
    weights = _load_weights()

    # Filter out topics on 90-day cooldown
    available = [
        t for t in EVERGREEN_TOPICS
        if not _is_on_cooldown(_slugify(t["keyword"]), used)
    ]
    if not available:
        logger.warning("All topics on cooldown, reusing earliest-expiring topic")
        available = list(EVERGREEN_TOPICS)

    # Optionally boost by current trending interest (re-ranks curated list only)
    available = _fetch_pytrends_boost(available)

    # Build effective weights: pillar weight × trend_score (if present)
    scored = []
    for t in available:
        pillar_w = weights.get(t.get("pillar", ""), 1.0)
        trend_w = t.get("trend_score", 50.0) / 50.0  # normalize: 50 = neutral
        scored.append((t, pillar_w * trend_w))

    total = sum(w for _, w in scored)
    r = random.uniform(0, total)
    cumulative = 0.0
    topic = scored[-1][0]
    for t, w in scored:
        cumulative += w
        if r <= cumulative:
            topic = t
            break

    topic = dict(topic)
    topic.pop("trend_score", None)  # clean up internal field
    topic["slug"] = _slugify(topic["keyword"])
    logger.info("Picked topic: %s [%s]", topic["keyword"], topic["pillar"])
    return topic


def mark_topic_used(slug: str) -> None:
    """Add a topic to the 90-day cooldown list. Call after successful pipeline run."""
    if not TOPICS_FILE.exists():
        data = {"topics": []}
    else:
        with TOPICS_FILE.open() as f:
            data = json.load(f)

    today = date.today()
    expires = (today + timedelta(days=COOLDOWN_DAYS)).isoformat()

    # Remove existing entry for same slug if present (refresh cooldown)
    data["topics"] = [t for t in data["topics"] if t["slug"] != slug]
    data["topics"].append({
        "slug": slug,
        "used_on": today.isoformat(),
        "expires": expires,
    })

    with TOPICS_FILE.open("w") as f:
        json.dump(data, f, indent=2)
    logger.info("Marked topic used: %s (expires %s)", slug, expires)
