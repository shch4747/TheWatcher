"""
Thin wrapper around the GitHub REST API for the activity signals the
healthbar needs. Deliberately narrow — only pulls what healthbar.py and
reasoner.py actually consume, not a general-purpose GitHub client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from config import GITHUB_TOKEN

API_ROOT = "https://api.github.com"


@dataclass
class RepoActivity:
    repo: str  # "org/name"
    last_commit_at: datetime | None
    last_committers: list[str] = field(default_factory=list)  # usernames, most-recent-first, deduped
    open_issues: int = 0
    closed_issues_last_30d: int = 0
    opened_issues_last_30d: int = 0
    commits_this_window: int = 0
    commits_prev_window: int = 0
    contributors: list[str] = field(default_factory=list)


class GitHubClient:
    def __init__(self, token: str = GITHUB_TOKEN):
        self.session = requests.Session()
        if token:
            self.session.headers.update(
                {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
            )

    def _get(self, path: str, **params) -> list | dict:
        r = self.session.get(f"{API_ROOT}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def get_activity(self, repo: str, window_days: int = 14) -> RepoActivity:
        """
        repo: "org/name"
        window_days: size of the "recent" window used for commit/issue trend
        comparisons (commits_this_window vs commits_prev_window).
        """
        now = datetime.now(timezone.utc)

        commits = self._get(f"/repos/{repo}/commits", per_page=100)
        last_commit_at = None
        last_committers: list[str] = []
        if commits:
            last_commit_at = _parse_gh_date(commits[0]["commit"]["author"]["date"])
            for c in commits[:20]:
                login = (c.get("author") or {}).get("login")
                if login and login not in last_committers:
                    last_committers.append(login)

        commits_this_window = _count_since(commits, now, window_days)
        commits_prev_window = _count_since(commits, now, window_days * 2) - commits_this_window

        issues = self._get(
            f"/repos/{repo}/issues", state="all", per_page=100, since=_iso_days_ago(now, 30)
        )
        open_issues = sum(1 for i in issues if i.get("state") == "open" and "pull_request" not in i)
        opened_last_30d = sum(1 for i in issues if "pull_request" not in i)
        closed_last_30d = sum(
            1 for i in issues if i.get("state") == "closed" and "pull_request" not in i
        )

        contributors_raw = self._get(f"/repos/{repo}/contributors", per_page=100)
        contributors = [c["login"] for c in contributors_raw if "login" in c]

        return RepoActivity(
            repo=repo,
            last_commit_at=last_commit_at,
            last_committers=last_committers,
            open_issues=open_issues,
            closed_issues_last_30d=closed_last_30d,
            opened_issues_last_30d=opened_last_30d,
            commits_this_window=commits_this_window,
            commits_prev_window=max(commits_prev_window, 0),
            contributors=contributors,
        )

    def get_readme_and_topics(self, repo: str) -> tuple[str, list[str]]:
        """Used for the richer, second-pass similarity check once a repo exists."""
        try:
            readme = self._get(f"/repos/{repo}/readme")
            import base64

            content = base64.b64decode(readme["content"]).decode("utf-8", errors="ignore")
        except requests.HTTPError:
            content = ""
        try:
            repo_info = self._get(f"/repos/{repo}")
            topics = repo_info.get("topics", [])
        except requests.HTTPError:
            topics = []
        return content, topics


def _parse_gh_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _iso_days_ago(now: datetime, days: int) -> str:
    from datetime import timedelta

    return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _count_since(commits: list[dict], now: datetime, days: int) -> int:
    from datetime import timedelta

    cutoff = now - timedelta(days=days)
    count = 0
    for c in commits:
        d = _parse_gh_date(c["commit"]["author"]["date"])
        if d >= cutoff:
            count += 1
    return count
