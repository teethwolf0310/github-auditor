"""对 signals.py 的纯函数测试。"""
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.signals import (
    collect_signals, perfect_hour_ratio, distinct_event_hours,
    distinct_event_days, events_within_24h, weekday_ratio,
    account_age_days, has_custom_avatar,
    own_repos, fork_repos, starred_own_originals, total_stars_on_own,
    distinct_languages,
)


FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def test_real_chronology():
    fx = _load("torvalds.json")
    evts = fx["performed_events"]
    # 跨多 hr
    assert distinct_event_hours(evts) >= 5
    # 跨多天
    assert len(distinct_event_days(evts)) >= 8
    # 不是 100% 整点
    assert perfect_hour_ratio(evts) < 0.5
    # 不全部集中在 24h
    assert events_within_24h(evts) < 8


def test_bot_chronology():
    fx = _load("fake_bot.json")
    evts = fx["performed_events"]
    # 全集中 1 天文井
    assert events_within_24h(evts) >= 25
    # 全部整点
    assert perfect_hour_ratio(evts) > 0.9


def test_custom_avatar_real():
    fx = _load("torvalds.json")
    assert has_custom_avatar(fx["profile"])


def test_custom_avatar_fakebot():
    fx = _load("fake_bot.json")
    # identicon/gravatar 默认头像
    fx["profile"]["avatar_url"] = "https://www.gravatar.com/avatar/ab12cd?d=identicon"
    assert not has_custom_avatar(fx["profile"])

    # 空 URL 也算非自定义
    fx["profile"]["avatar_url"] = ""
    assert not has_custom_avatar(fx["profile"])


def test_account_age_days_profile():
    fx = _load("torvalds.json")
    age = account_age_days(fx["profile"])
    assert age > 3000  # 至少几年


def test_own_vs_fork():
    fx = _load("torvalds.json")
    own = own_repos(fx["repos"])
    fork = fork_repos(fx["repos"])
    assert len(own) == 3
    assert len(fork) == 0
    assert total_stars_on_own(fx["repos"]) > 100_000


def test_starred_own():
    fx = _load("torvalds.json")
    assert starred_own_originals(fx["repos"], min_stars=1) == 3
    assert starred_own_originals(fx["repos"], min_stars=1000) == 2


def test_distinct_languages():
    fx = _load("torvalds.json")
    langs = distinct_languages(fx["repos"])
    assert "C" in langs and "C++" in langs


def test_collect_signals_shape():
    fx = _load("torvalds.json")
    sigs = collect_signals(
        profile=fx["profile"],
        repos=fx["repos"],
        performed_events=fx["performed_events"],
        received_events=fx["received_events"],
        orgs=fx["orgs"],
        gpg_keys=fx["gpg_keys"],
        ssh_keys=fx["ssh_keys"],
        top_own_repos_with_commits=fx["top_own_with_commits"],
    )
    # 关键字段必然存在
    for k in ["has_custom_avatar", "followers", "own_repos_count",
              "total_stars_on_own", "distinct_languages", "perfect_hour_ratio",
              "cross_owners_received", "commit_distinct_hours"]:
        assert k in sigs, f"missing {k}"
