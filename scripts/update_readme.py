#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

USERNAME = "thebeaarr"
ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = ROOT / "templates" / "README.template.md"
README_PATH = ROOT / "README.md"
CACHE_DIR = ROOT / ".cache"
CACHE_PATH = CACHE_DIR / "github_profile_stats.json"

# 6 hours
CACHE_TTL_SECONDS = 6 * 60 * 60

REST_API = "https://api.github.com"
GRAPHQL_API = "https://api.github.com/graphql"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M UTC")

def get_env_token() -> str | None : 
    return os.getenv("GITHUB_TOKEN")


def build_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USERNAME}-profile-readme-updater",
    }
    token = get_env_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def http_get_json(url: str) -> Any:
    req = Request(url, headers=build_headers(), method="GET")
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def graphql_query(query: str, variables: Dict[str, Any]) -> Any:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = Request(
        GRAPHQL_API,
        data=payload,
        headers={
            **build_headers(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "errors" in data:
        raise RuntimeError(f"GraphQL error: {data['errors']}")
    return data["data"]


def load_cache() -> Dict[str, Any] | None:
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        fetched_at = data.get("fetched_at", 0)
        if time.time() - fetched_at <= CACHE_TTL_SECONDS:
            return data
    except Exception:
        return None
    return None


def save_cache(stats: Dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": time.time(),
        "stats": stats,
    }
    CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def paginate_repos() -> List[Dict[str, Any]]:
    all_repos: List[Dict[str, Any]] = []
    page = 1

    while True:
        query = urlencode(
            {
                "per_page": 100,
                "page": page,
                "sort": "updated",
                "direction": "desc",
            }
        )
        url = f"{REST_API}/users/{USERNAME}/repos?{query}"
        data = http_get_json(url)

        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected repo response: {data}")

        if not data:
            break

        # Keep only non-fork repos for cleaner language aggregation.
        filtered = [repo for repo in data if not repo.get("fork", False)]
        all_repos.extend(filtered)

        if len(data) < 100:
            break
        page += 1

    return all_repos


def fetch_repo_languages(repo_name: str) -> Dict[str, int]:
    url = f"{REST_API}/repos/{USERNAME}/{repo_name}/languages"
    data = http_get_json(url)
    if not isinstance(data, dict):
        return {}
    return {k: int(v) for k, v in data.items()}


def aggregate_languages(repos: List[Dict[str, Any]]) -> Tuple[str, int]:
    totals: Dict[str, int] = defaultdict(int)

    for repo in repos:
        name = repo.get("name")
        if not name:
            continue
        try:
            lang_map = fetch_repo_languages(name)
        except Exception:
            continue
        for lang, value in lang_map.items():
            totals[lang] += value

    if not totals:
        return "n/a", 0

    sorted_langs = sorted(totals.items(), key=lambda x: x[1], reverse=True)
    top = sorted_langs[:4]
    rendered = " | ".join(f"{lang} {format_pct(val, sum(totals.values()))}" for lang, val in top)
    return rendered, len(totals)


def format_pct(value: int, total: int) -> str:
    if total <= 0:
        return "0%"
    pct = (value / total) * 100
    return f"{pct:.1f}%"


def contributions_and_streaks() -> Dict[str, Any]:
    # Exact yearly contributions + calendar-based streak computation.
    # contributionsCollection is part of GitHub's GraphQL schema.
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            weeks {
              contributionDays {
                contributionCount
                date
              }
            }
          }
          contributionCalendar {
            totalContributions
          }
        }
      }
    }
    """

    now = utc_now()
    year_ago = now - timedelta(days=365)

    data = graphql_query(
        query,
        {
            "login": USERNAME,
            "from": year_ago.isoformat(),
            "to": now.isoformat(),
        },
    )

    calendar = (
        data["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
    )
    total_contribs = int(
        data["user"]["contributionsCollection"]["contributionCalendar"][
            "totalContributions"
        ]
    )

    days: List[Tuple[str, int]] = []
    for week in calendar:
        for day in week["contributionDays"]:
            days.append((day["date"], int(day["contributionCount"])))

    days.sort(key=lambda x: x[0])

    longest = 0
    current = 0
    running = 0

    for _, count in days:
        if count > 0:
            running += 1
            if running > longest:
                longest = running
        else:
            running = 0

    # Current streak: count backwards from latest day with calendar order.
    for _, count in reversed(days):
        if count > 0:
            current += 1
        else:
            break

    return {
        "contribs_year": total_contribs,
        "current_streak": current,
        "longest_streak": longest,
    }


def fetch_pinned_repos() -> List[Dict[str, Any]]:
    query = """
    query($login: String!) {
      user(login: $login) {
        pinnedItems(first: 6, types: [REPOSITORY]) {
          nodes {
            ... on Repository {
              name
              description
              url
              primaryLanguage { name }
            }
          }
        }
      }
    }
    """

    data = graphql_query(query, {"login": USERNAME})
    nodes = data["user"]["pinnedItems"]["nodes"]

    return [
        {
            "name": node["name"],
            "description": node.get("description") or "—",
            "url": node["url"],
            "language": (node.get("primaryLanguage") or {}).get("name", "—"),
        }
        for node in nodes
    ]


def render_featured_projects(projects: List[Dict[str, Any]]) -> str:
    if not projects:
        return "_Pin repositories on your GitHub profile to feature them here._"

    lines = ["| Project | Description | Language |", "|---|---|---|"]
    for project in projects:
        lines.append(
            f"| [{project['name']}]({project['url']}) | {project['description']} | {project['language']} |"
        )
    return "\n".join(lines)


def collect_stats() -> Dict[str, Any]:
    repos = paginate_repos()

    top_langs_rendered, language_count = aggregate_languages(repos)
    contrib_stats = contributions_and_streaks()

    try:
        pinned = fetch_pinned_repos()
    except Exception:
        pinned = []

    return {
        "contribs_year": contrib_stats["contribs_year"],
        "current_streak": contrib_stats["current_streak"],
        "longest_streak": contrib_stats["longest_streak"],
        "top_langs": f"{top_langs_rendered}  ({language_count} total)",
        "featured_projects": render_featured_projects(pinned),
        "updated": iso_now(),
    }


def render_readme(stats: Dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "{{CONTRIBS_YEAR}}": str(stats["contribs_year"]),
        "{{CURRENT_STREAK}}": str(stats["current_streak"]),
        "{{LONGEST_STREAK}}": str(stats["longest_streak"]),
        "{{TOP_LANGS}}": stats["top_langs"],
        "{{FEATURED_PROJECTS}}": stats["featured_projects"],
        "{{UPDATED}}": stats["updated"],
    }

    output = template
    for placeholder, value in replacements.items():
        output = output.replace(placeholder, value)

    return output


def main() -> int:
    try:
        cached = load_cache()
        if cached:
            stats = cached["stats"]
        else:
            stats = collect_stats()
            save_cache(stats)

        rendered = render_readme(stats)
        README_PATH.write_text(rendered, encoding="utf-8")
        print("README.md updated successfully.")
        return 0

    except HTTPError as exc:
        print(f"HTTP Error: {exc.code}", file=sys.stderr)

        try:
            print(exc.read().decode(), file=sys.stderr)
        except Exception:
            pass

        return 1

    except Exception:
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    raise SystemExit(main())