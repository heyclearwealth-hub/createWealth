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

# Evergreen fallback list — 40 topics across 5 pillars
EVERGREEN_TOPICS = [
    # budgeting
    {"keyword": "50/30/20 budget rule explained", "pillar": "budgeting"},
    {"keyword": "zero based budgeting for beginners", "pillar": "budgeting"},
    {"keyword": "how to stop living paycheck to paycheck", "pillar": "budgeting"},
    {"keyword": "emergency fund how much to save", "pillar": "budgeting"},
    {"keyword": "sinking funds personal finance", "pillar": "budgeting"},
    {"keyword": "best budgeting apps 2026", "pillar": "budgeting"},
    {"keyword": "how to save money on groceries", "pillar": "budgeting"},
    {"keyword": "high yield savings account explained", "pillar": "budgeting"},
    # debt
    {"keyword": "debt avalanche vs debt snowball method", "pillar": "debt"},
    {"keyword": "how to pay off student loans fast", "pillar": "debt"},
    {"keyword": "credit card debt payoff strategy", "pillar": "debt"},
    {"keyword": "what is a good credit score", "pillar": "debt"},
    {"keyword": "how to improve credit score fast", "pillar": "debt"},
    {"keyword": "should you pay off debt or invest", "pillar": "debt"},
    {"keyword": "how to negotiate credit card interest rate", "pillar": "debt"},
    {"keyword": "personal loan vs credit card debt", "pillar": "debt"},
    # investing
    {"keyword": "roth ira for beginners 2026", "pillar": "investing"},
    {"keyword": "how to start investing with 1000 dollars", "pillar": "investing"},
    {"keyword": "index funds explained for beginners", "pillar": "investing"},
    {"keyword": "401k contribution limits 2026", "pillar": "investing"},
    {"keyword": "dollar cost averaging strategy explained", "pillar": "investing"},
    {"keyword": "compound interest explained with examples", "pillar": "investing"},
    {"keyword": "ETF vs mutual fund which is better", "pillar": "investing"},
    {"keyword": "what is a brokerage account", "pillar": "investing"},
    # tax
    {"keyword": "tax deductions for salaried employees 2026", "pillar": "tax"},
    {"keyword": "roth ira vs traditional ira tax benefits", "pillar": "tax"},
    {"keyword": "how to file taxes for beginners", "pillar": "tax"},
    {"keyword": "what is the standard deduction 2026", "pillar": "tax"},
    {"keyword": "HSA tax benefits explained", "pillar": "tax"},
    {"keyword": "how to reduce taxable income legally", "pillar": "tax"},
    {"keyword": "capital gains tax explained simply", "pillar": "tax"},
    {"keyword": "401k tax benefits for young professionals", "pillar": "tax"},
    # career_income
    {"keyword": "how to negotiate salary offer", "pillar": "career_income"},
    {"keyword": "first salary what to do with it", "pillar": "career_income"},
    {"keyword": "how to ask for a raise successfully", "pillar": "career_income"},
    {"keyword": "side hustle ideas for full time employees", "pillar": "career_income"},
    {"keyword": "freelancing vs full time job income", "pillar": "career_income"},
    {"keyword": "passive income ideas for beginners", "pillar": "career_income"},
    {"keyword": "how to build wealth in your 20s", "pillar": "career_income"},
    {"keyword": "financial goals for young professionals", "pillar": "career_income"},
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


def _fetch_pytrends(used_topics: list[dict]) -> list[dict]:
    """Fetch trending finance topics from pytrends. Returns list of dicts with keyword+pillar."""
    try:
        from pytrends.request import TrendReq
    except ImportError:
        logger.warning("pytrends not installed, skipping Google Trends fetch")
        return []

    candidates = []
    for attempt in range(3):
        try:
            pt = TrendReq(hl="en-US", tz=360)
            pt.build_payload(
                kw_list=["personal finance", "investing", "credit card debt", "roth ira", "budget"],
                cat=FINANCE_CATEGORY,
                timeframe="now 7-d",
                geo="US",
            )
            related = pt.related_queries()
            for kw, data in related.items():
                if data and data.get("rising") is not None:
                    for _, row in data["rising"].iterrows():
                        keyword = str(row.get("query", "")).strip()
                        if keyword and len(keyword) > 5:
                            slug = _slugify(keyword)
                            if not _is_on_cooldown(slug, used_topics):
                                candidates.append({"keyword": keyword, "pillar": "investing", "slug": slug})
            if candidates:
                return candidates
        except Exception as exc:
            wait = 2 ** attempt + random.uniform(0, 1)
            logger.warning("pytrends attempt %d failed: %s — retrying in %.1fs", attempt + 1, exc, wait)
            time.sleep(wait)

    logger.warning("pytrends failed after 3 attempts, falling back to evergreen list")
    return []


def pick_topic() -> dict:
    """
    Returns a topic dict: {keyword, pillar, slug}.
    Priority: pytrends trending → weighted evergreen fallback.
    Filters out any topic on 90-day cooldown.
    """
    used = _load_used_topics()
    weights = _load_weights()

    # Try pytrends first
    trending = _fetch_pytrends(used)
    if trending:
        topic = _pick_weighted(trending, weights)
        if "slug" not in topic:
            topic["slug"] = _slugify(topic["keyword"])
        logger.info("Picked trending topic: %s [%s]", topic["keyword"], topic["pillar"])
        return topic

    # Weighted evergreen fallback
    available = [
        t for t in EVERGREEN_TOPICS
        if not _is_on_cooldown(_slugify(t["keyword"]), used)
    ]
    if not available:
        # All topics on cooldown — pick the one that expired soonest
        logger.warning("All topics on cooldown, reusing earliest-expiring topic")
        available = list(EVERGREEN_TOPICS)

    topic = _pick_weighted(available, weights)
    topic = dict(topic)
    topic["slug"] = _slugify(topic["keyword"])
    logger.info("Picked evergreen topic: %s [%s]", topic["keyword"], topic["pillar"])
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
