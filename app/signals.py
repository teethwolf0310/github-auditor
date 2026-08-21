"""从 GitHub API 返回的 raw JSON 中抽取**所有**打分需要的可解释信号。

每个函数都是纯函数：输入 dict/list，输出标量，方便测试。
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


# ---------- 通用时间工具 ----------

def _parse_ts(s: Any) -> Optional[datetime]:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        s2 = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


NOW = datetime.now(timezone.utc)


# ---------- profile 信号 ----------

# GitHub 的默认头像识别（启发式）：
# - 自定义头像 URL 一律是 avatars.githubusercontent.com/u/<uid>?v=4
# - 默认头像 / identicon 会带 "identicon" 字符串（gravatar）
DEFAULT_AVATAR_PATTERNS = (
    "/identicons/",
    "identicon",
    "gravatar.com/avatar/",
)


def has_custom_avatar(profile: dict) -> bool:
    url = (profile.get("avatar_url") or "").lower()
    if not url:
        return False
    return not any(p in url for p in DEFAULT_AVATAR_PATTERNS)


def has_name(profile: dict) -> bool:
    return bool((profile.get("name") or "").strip())


def has_bio(profile: dict) -> bool:
    return bool((profile.get("bio") or "").strip())


def has_location_or_company(profile: dict) -> bool:
    return bool((profile.get("location") or "").strip()) or bool((profile.get("company") or "").strip())


def has_public_email(profile: dict) -> bool:
    return bool((profile.get("email") or "").strip())


def has_hireable(profile: dict) -> bool:
    return bool(profile.get("hireable"))


def has_blog(profile: dict) -> bool:
    return bool((profile.get("blog") or "").strip())


def has_twitter(profile: dict) -> bool:
    return bool((profile.get("twitter_username") or "").strip())


def account_age_days(profile: dict) -> int:
    created = _parse_ts(profile.get("created_at"))
    if not created:
        return 0
    return max(0, (NOW - created).days)


def followers_num(profile: dict) -> int:
    return int(profile.get("followers") or 0)


def following_num(profile: dict) -> int:
    return int(profile.get("following") or 0)


# ---------- repos 信号 ----------

def is_fork_repo(repo: dict) -> bool:
    return bool(repo.get("fork"))


def own_repos(repos: list[dict]) -> list[dict]:
    return [r for r in repos if not is_fork_repo(r)]


def fork_repos(repos: list[dict]) -> list[dict]:
    return [r for r in repos if is_fork_repo(r)]


def distinct_languages(repos: list[dict]) -> set[str]:
    return {r.get("language") for r in repos if r.get("language")}


def starred_own_originals(repos: list[dict], min_stars: int = 1) -> int:
    return sum(1 for r in own_repos(repos) if (r.get("stargazers_count") or 0) >= min_stars)


def total_stars_on_own(repos: list[dict]) -> int:
    return sum(int(r.get("stargazers_count") or 0) for r in own_repos(repos))


def has_non_readme_text(description_or_topics: Iterable[str]) -> bool:
    """至少有 1 个非空 description 或 topic 表示开发者有维护意图。"""
    return any((s or "").strip() for s in description_or_topics)


def topics_diversity(repos: list[dict]) -> int:
    topics = set()
    for r in repos:
        for t in (r.get("topics") or []):
            topics.add(t)
    return len(topics)


# ---------- events 信号 ----------

def event_type_distribution(events: list[dict]) -> Counter:
    return Counter(e.get("type") for e in events if e.get("type"))


def distinct_event_owners(events: list[dict]) -> set[str]:
    owners = set()
    for e in events:
        repo = (e.get("repo") or {}).get("name") or ""
        if "/" in repo:
            owners.add(repo.split("/", 1)[0])
    return owners


def distinct_event_days(events: list[dict]) -> set[str]:
    days = set()
    for e in events:
        ts = _parse_ts(e.get("created_at"))
        if ts:
            days.add(ts.strftime("%Y-%m-%d"))
    return days


def event_span_hours(events: list[dict]) -> float:
    """事件跨越的总时长（小时）。"""
    ts_list = sorted([_parse_ts(e.get("created_at")) for e in events if e.get("created_at")])
    ts_list = [t for t in ts_list if t]
    if len(ts_list) < 2:
        return 0.0
    return (ts_list[-1] - ts_list[0]).total_seconds() / 3600.0


def events_within_24h(events: list[dict]) -> int:
    """落入事件最高峰 24h 窗口的事件数 —— burst detection."""
    ts_sorted = sorted([_parse_ts(e.get("created_at")) for e in events if e.get("created_at")])
    ts_sorted = [t for t in ts_sorted if t]
    if not ts_sorted:
        return 0
    best = 1
    for i, t in enumerate(ts_sorted):
        j = i
        while j < len(ts_sorted) and (ts_sorted[j] - t).total_seconds() <= 86400:
            j += 1
        if j - i > best:
            best = j - i
    return best


def event_hours_histogram(events: list[dict]) -> Counter:
    """UTC hour of day histogram of events."""
    hrs = Counter()
    for e in events:
        ts = _parse_ts(e.get("created_at"))
        if ts:
            hrs[ts.hour] += 1
    return hrs


def distinct_event_hours(events: list[dict]) -> int:
    return len(event_hours_histogram(events))


def perfect_hour_ratio(events: list[dict]) -> float:
    """整点（minute=0）占比。AI/bot 经常 cron 到点触发。"""
    if not events:
        return 0.0
    perfect = 0
    total = 0
    for e in events:
        ts = _parse_ts(e.get("created_at"))
        if ts:
            total += 1
            if ts.minute == 0 and ts.second == 0:
                perfect += 1
    return perfect / total if total else 0.0


def weekday_ratio(events: list[dict]) -> float:
    if not events:
        return 0.0
    wd = 0
    tt = 0
    for e in events:
        ts = _parse_ts(e.get("created_at"))
        if ts:
            tt += 1
            if ts.weekday() < 5:
                wd += 1
    return wd / tt if tt else 0.0


# ---------- 顶层组装：一个用户全部信号 ----------

def collect_signals(
    profile: dict,
    repos: list[dict],
    performed_events: list[dict],
    received_events: list[dict],
    orgs: list[dict],
    gpg_keys: list[dict],
    ssh_keys: list[dict],
    top_own_repos_with_commits: list[dict],  # [{repo, commits:[{commit:{committer:{date}}}...]}]
) -> dict:
    """统一组装可解释信号 dict，让 scorer 只依赖这一个入口。"""
    own = own_repos(repos)
    all_evts = performed_events + received_events

    # cross-owner reach: 别人在他自己的 repo 上 star/fork/issue/PR 数（用 received events 估算）
    cross_owners = {((e.get("repo") or {}).get("name") or "").split("/", 1)[0]
                    for e in received_events
                    if "/" in ((e.get("repo") or {}).get("name") or "")}

    # 提交时间健康度（用 sample own repos 的 commits）
    commits_all: list[datetime] = []
    for r in top_own_repos_with_commits:
        for c in r.get("commits") or []:
            ts = _parse_ts(((c.get("commit") or {}).get("committer") or {}).get("date"))
            if ts:
                commits_all.append(ts)
    commit_day_set = {t.strftime("%Y-%m-%d") for t in commits_all}
    commit_hours = {t.hour for t in commits_all}
    commit_weekdays = {t.weekday() for t in commits_all}

    # 事件（performed + received）的时间分布 —— 用于 commit 采样不到时的兜底
    event_ts_all: list[datetime] = []
    for e in all_evts:
        ts = _parse_ts(e.get("created_at"))
        if ts:
            event_ts_all.append(ts)
    event_day_set_all = {t.strftime("%Y-%m-%d") for t in event_ts_all}
    event_hours_set_all = {t.hour for t in event_ts_all}
    event_weekday_set_all = {t.weekday() for t in event_ts_all}

    return {
        # profile
        "has_custom_avatar": has_custom_avatar(profile),
        "has_name": has_name(profile),
        "has_bio": has_bio(profile),
        "has_location_or_company": has_location_or_company(profile),
        "has_public_email": has_public_email(profile),
        "has_hireable": has_hireable(profile),
        "has_blog": has_blog(profile),
        "has_twitter": has_twitter(profile),
        "account_age_days": account_age_days(profile),
        "followers": followers_num(profile),
        "following": following_num(profile),
        "follower_following_ratio": (
            followers_num(profile) / max(1, following_num(profile))
        ) if followers_num(profile) else 0.0,
        "public_repos_count": int(profile.get("public_repos") or 0),
        "type": profile.get("type") or "User",

        # repos
        "own_repos_count": len(own),
        "fork_repos_count": len(fork_repos(repos)),
        "own_repos_with_star_ge_1": starred_own_originals(repos, min_stars=1),
        "total_stars_on_own": total_stars_on_own(repos),
        "distinct_languages": len(distinct_languages(repos)),
        "topics_diversity": topics_diversity(repos),

        # activity
        "performed_events_count": len(performed_events),
        "received_events_count": len(received_events),
        "performed_event_type_diversity": len(event_type_distribution(performed_events)),
        "distinct_event_owners": len(distinct_event_owners(all_evts)),
        "distinct_event_days": len(distinct_event_days(all_evts)),
        "event_span_hours": event_span_hours(all_evts),
        "events_in_peak_24h": events_within_24h(all_evts),
        "distinct_event_hours": distinct_event_hours(all_evts),
        "perfect_hour_ratio": perfect_hour_ratio(all_evts),
        "weekday_ratio": weekday_ratio(all_evts),

        # commits chronology（own + 兜底 events sample）
        "sampled_commits_count": len(commits_all),
        "commit_distinct_days": len(commit_day_set),
        "commit_distinct_hours": len(commit_hours),
        "commit_distinct_weekdays": len(commit_weekdays),
        # expanded: 合并 event chronology 后用于真实性判定
        "activity_distinct_days": len(event_day_set_all),
        "activity_distinct_hours": len(event_hours_set_all),
        "activity_distinct_weekdays": len(event_weekday_set_all),

        # 锚定
        "orgs_count": len(orgs),
        "gpg_key_count": len(gpg_keys),
        "ssh_key_count": len(ssh_keys),

        # cross-owner reach
        "cross_owners_received": len(cross_owners),
    }
