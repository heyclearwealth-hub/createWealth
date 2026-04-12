"""
issue_manager.py — GitHub Issues create/parse for the approval gate.

Creates a [PIPELINE] issue with:
  - Video review checklist
  - Hidden artifact run_id marker: <!-- artifact-run-id: {run_id} -->

Parses APPROVE / REJECT comments and validates artifact provenance.
"""
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
REPO = os.environ.get("GITHUB_REPOSITORY", "heyclearwealth-hub/createWealth")
APPROVER = os.environ.get("PIPELINE_APPROVER_USERNAME", "heyclearwealth-hub")


def _headers() -> dict:
    token = os.environ["GITHUB_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ── Issue creation ─────────────────────────────────────────────────────────────

def create_review_issue(
    run_id: str,
    pipeline_json: dict,
    candidates: dict,
) -> str:
    """
    Create a GitHub Issue for human review.
    Returns the issue HTML URL.
    """
    slug = pipeline_json.get("slug", "unknown")
    title_default = candidates.get("titles", ["(no title)"])[candidates.get("default_index", 0)]
    hook_score = pipeline_json.get("hook_score", "n/a")
    compliance = pipeline_json.get("compliance", "pass")

    body = f"""\
## ClearWealth Pipeline — Review Required

**Slug:** `{slug}`
**Default title:** {title_default}
**Hook score:** {hook_score}
**Compliance:** {compliance}

### Review Checklist

- [ ] Watched `final_video.mp4` in full (download from artifact below)
- [ ] Hook grabs attention in first 10 seconds
- [ ] No misleading claims / earnings guarantees
- [ ] Finance disclaimer present in description
- [ ] Thumbnail concept matches video content
- [ ] Audio quality and pacing are acceptable

### Title Candidates

{_format_candidates(candidates.get("titles", []))}

### Thumbnail Texts

{_format_candidates(candidates.get("thumbnail_texts", []))}

### Approve or Reject

Reply with exactly `APPROVE` to upload, or `REJECT: <reason>` to skip.

---

<!-- artifact-run-id: {run_id} -->
"""

    payload = {
        "title": f"[PIPELINE] Review: {slug}",
        "body": body,
        "labels": ["pipeline-review"],
    }
    resp = requests.post(
        f"{GITHUB_API}/repos/{REPO}/issues",
        headers=_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    url = resp.json()["html_url"]
    logger.info("Created review issue: %s", url)
    return url


def _format_candidates(items: list) -> str:
    lines = []
    for i, item in enumerate(items):
        lines.append(f"{i + 1}. {item}")
    return "\n".join(lines) if lines else "(none)"


# ── run_id extraction ──────────────────────────────────────────────────────────

def extract_run_id(issue_body: str) -> str:
    """
    Extract run_id from hidden HTML comment.
    Pattern: <!-- artifact-run-id: <digits> -->
    Raises ValueError if not found or malformed.
    """
    match = re.search(r"<!--\s*artifact-run-id:\s*(\d{7,})\s*-->", issue_body)
    if not match:
        raise ValueError("No valid artifact-run-id marker found in issue body")
    return match.group(1)


# ── Provenance validation ──────────────────────────────────────────────────────

def validate_run_provenance(run_id: str) -> bool:
    """
    Validate artifact provenance via GitHub API.
    Returns True if all checks pass, False otherwise.

    Guards:
    1. run_id is all-digits and within plausible range
    2. Workflow run exists on GitHub
    3. workflow name == research-and-render
    4. workflow path ends with .github/workflows/research-and-render.yml
    5. branch == main
    6. conclusion == success
    7. run belongs to this repository
    8. event in {schedule, workflow_dispatch}
    9. artifact named pipeline-{run_id} exists for this run
    """
    # Guard 1: format check
    if not re.fullmatch(r"\d{7,20}", run_id):
        logger.warning("run_id format invalid: %s", run_id)
        return False

    # Guards 2–8: fetch workflow run
    run_url = f"{GITHUB_API}/repos/{REPO}/actions/runs/{run_id}"
    try:
        resp = requests.get(run_url, headers=_headers(), timeout=30)
    except requests.RequestException as exc:
        logger.error("GitHub API request failed: %s", exc)
        return False

    if resp.status_code == 404:
        logger.warning("Workflow run %s not found", run_id)
        return False
    resp.raise_for_status()
    run = resp.json()

    # Guard 3: workflow name
    workflow_name = run.get("name", "")
    if workflow_name != "research-and-render":
        logger.warning("Unexpected workflow name '%s' for run %s", workflow_name, run_id)
        return False

    # Guard 4: workflow file path
    workflow_path = run.get("path", "")
    if not workflow_path.endswith(".github/workflows/research-and-render.yml"):
        logger.warning("Unexpected workflow path '%s' for run %s", workflow_path, run_id)
        return False

    # Guard 5: branch
    head_branch = run.get("head_branch", "")
    if head_branch != "main":
        logger.warning("Run %s was on branch '%s', not main", run_id, head_branch)
        return False

    # Guard 6: conclusion
    conclusion = run.get("conclusion", "")
    if conclusion != "success":
        logger.warning("Run %s conclusion is '%s'", run_id, conclusion)
        return False

    # Guard 7: repository match
    repo_full_name = run.get("repository", {}).get("full_name", "")
    if repo_full_name != REPO:
        logger.warning("Run %s repo '%s' does not match expected '%s'", run_id, repo_full_name, REPO)
        return False

    # Guard 8: event type
    event = run.get("event", "")
    if event not in {"schedule", "workflow_dispatch"}:
        logger.warning("Run %s has unexpected event '%s'", run_id, event)
        return False

    # Guard 9: artifact named pipeline-{run_id} exists
    artifacts_url = f"{GITHUB_API}/repos/{REPO}/actions/runs/{run_id}/artifacts"
    try:
        resp = requests.get(artifacts_url, headers=_headers(), timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Artifact list request failed: %s", exc)
        return False

    artifacts = resp.json().get("artifacts", [])
    expected_name = f"pipeline-{run_id}"
    if not any(a.get("name") == expected_name for a in artifacts):
        logger.warning("Artifact '%s' not found for run %s", expected_name, run_id)
        return False

    logger.info("Provenance validated for run %s", run_id)
    return True


# ── Comment parsing ────────────────────────────────────────────────────────────

def parse_comment(comment_body: str) -> dict:
    """
    Parse a comment body into an action dict.
    Returns {"action": "approve"} or {"action": "reject", "reason": "..."} or {"action": "ignore"}.
    """
    cleaned = comment_body.strip()
    if cleaned == "APPROVE":
        return {"action": "approve"}
    if cleaned.startswith("REJECT:"):
        reason = cleaned[len("REJECT:") :].strip()
        return {"action": "reject", "reason": reason}
    return {"action": "ignore"}


# ── Label helpers ──────────────────────────────────────────────────────────────

def add_label(issue_number: int, label: str) -> None:
    resp = requests.post(
        f"{GITHUB_API}/repos/{REPO}/issues/{issue_number}/labels",
        headers=_headers(),
        json={"labels": [label]},
        timeout=30,
    )
    resp.raise_for_status()


def close_issue(issue_number: int, comment: str) -> None:
    # Post comment
    requests.post(
        f"{GITHUB_API}/repos/{REPO}/issues/{issue_number}/comments",
        headers=_headers(),
        json={"body": comment},
        timeout=30,
    ).raise_for_status()
    # Close
    requests.patch(
        f"{GITHUB_API}/repos/{REPO}/issues/{issue_number}",
        headers=_headers(),
        json={"state": "closed"},
        timeout=30,
    ).raise_for_status()
