"""六维打分规则 + 综合 total。每条规则都可解释（返回 evidence dict）。

打分函数的设计原则：
1. 每个维度返回 (score_0_100, evidence_dict) 方便审计。
2. 不能简单的 binary 累加，要有 log/clip 平滑，避免一处缺失直接 0 分。
3. 反作弊信号**只在 authenticity 内扣分**，不影响其他维度（其他维度独立）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# 默认权重（在 config.yaml 里可以覆盖；和必须等于 100）
DEFAULT_WEIGHTS = {
    "authenticity": 35,
    "activity": 20,
    "capability": 20,
    "breadth": 10,
    "longevity": 8,
    "identity": 7,
}


@dataclass
class DimResult:
    score: int         # 0-100
    evidence: dict     # 子信号 + 加分/扣分记录
    notes: list        # 人类可读说明


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> int:
    return int(max(lo, min(hi, round(x))))


def _log_scale(n: float, scale_at: float, max_at: float) -> int:
    """对数曲线：n=scale_at 时约给一半，n ≥ max_at 时给满分。"""
    if n <= 0:
        return 0
    if n >= max_at:
        return 100
    # y = 100 * log(1 + n) / log(1 + max_at) 再稍微钳
    return _clip(100.0 * math.log1p(n) / math.log1p(max_at))


# ====================================================================
# 各维度打分

def score_authenticity(sig: dict[str, Any]) -> DimResult:
    """真实性

    加权 100 分制，10 项加分项 + 3 项强反作弊扣分。

    - GitHub 官方 Bot 账号（profile.type == "Bot"）→ 直接判 0，因为他们
      本来就是机器，真实性这个问题不适用，不需要维度拆解。
    """
    if sig.get("type") == "Bot":
        return DimResult(
            0,
            {"verdict": "account type=Bot → authenticity=0 (declared bot)"},
            ["GitHub 已声明 type=Bot"],
        )
    ev: dict[str, Any] = {}
    notes: list[str] = []
    score = 0.0

    # + 15 自定义头像
    # 例外：GitHub 官方 Bot 类型的账号头像其实也是默认 avatar。Bot 不该拿这个分。
    if sig["has_custom_avatar"] and sig.get("type") != "Bot":
        score += 15
        ev["custom_avatar"] = "+15"
    else:
        ev["custom_avatar"] = "+0" + ("(Bot)" if sig.get("type") == "Bot" else "(默认头像)")

    # + 10 名字 + bio
    if sig["has_name"] and sig["has_bio"]:
        score += 10
        ev["name_bio"] = "+10"
    elif sig["has_name"] or sig["has_bio"]:
        score += 5
        ev["name_bio"] = "+5 (只一项)"
    else:
        ev["name_bio"] = "+0"

    # + 10 location or company
    if sig["has_location_or_company"]:
        score += 10
        ev["location_or_company"] = "+10"
    else:
        ev["location_or_company"] = "+0"

    # + 10 followers（log）
    f_pts = _log_scale(sig["followers"], scale_at=10, max_at=200) // 10  # 转 0-10
    score += f_pts
    ev[f"followers={sig['followers']}"] = f"+{f_pts}"

    # + 10 原创 repo
    if sig["own_repos_count"] > 0:
        score += 10
        ev["own_repos"] = "+10"
    else:
        ev["own_repos"] = "+0 (全部 fork)"

    # + 15 commit 时间健康度：跨时段 + 跨天 + 部分非整点
    ch = 0
    missing_commit_sample = sig["sampled_commits_count"] == 0
    if sig["commit_distinct_hours"] >= 6:
        ch += 6
    elif sig["commit_distinct_hours"] >= 3:
        ch += 3
    if sig["commit_distinct_weekdays"] >= 5:
        ch += 5
    elif sig["commit_distinct_weekdays"] >= 3:
        ch += 2
    if sig["commit_distinct_days"] >= 10:
        ch += 4
    elif sig["commit_distinct_days"] >= 5:
        ch += 2
    score += ch
    if missing_commit_sample and sig["own_repos_count"] > 0 and sig["performed_events_count"] < 5:
        ev["commit_chronology"] = f"+{ch} (own repos top-N 全部由他人维护 + 也几乎无 public event — 真信号弱)"
    elif missing_commit_sample and sig["performed_events_count"] >= 5 and sig["distinct_event_days"] >= 7:
        # 用户活跃但采样不到 own commits（典型 org 工程师 / 全开 fork 协作）：
        # 用 event chronology 折算 — 真实用户的 event 也跨时段/跨天
        fallback = 0
        if sig["activity_distinct_hours"] >= 6:
            fallback += 5
        elif sig["activity_distinct_hours"] >= 3:
            fallback += 2
        if sig["activity_distinct_weekdays"] >= 5:
            fallback += 4
        elif sig["activity_distinct_weekdays"] >= 3:
            fallback += 2
        if sig["activity_distinct_days"] >= 10:
            fallback += 3
        elif sig["activity_distinct_days"] >= 5:
            fallback += 1
        score += fallback
        ev["commit_chronology"] = (
            f"+{fallback} (未采样 own commits，但用户近期活跃: events 跨 {sig['activity_distinct_hours']}h/"
            f"{sig['activity_distinct_weekdays']}wd/{sig['activity_distinct_days']}d，折合给分)"
        )
    else:
        ev["commit_chronology"] = f"+{ch} (hours={sig['commit_distinct_hours']}, weekdays={sig['commit_distinct_weekdays']}, days={sig['commit_distinct_days']})"
    if sig["perfect_hour_ratio"] > 0.5 and sig["sampled_commits_count"] > 10:
        score -= 5
        ev["commit_chronology_penalty"] = f"-5 (perfect_hour_ratio={sig['perfect_hour_ratio']:.2f})"

    # + 10 cross-owner reach（received_events 里出现过的 owner 数）
    # 启发：真用户在自家 repo 上总会收到几个 fork/star/issue，至少 1-2 个 owner 自然产生
    if sig["cross_owners_received"] >= 5:
        score += 10
        ev["cross_owner"] = "+10"
    elif sig["cross_owners_received"] >= 2:
        score += 5
        ev["cross_owner"] = "+5"
    else:
        ev["cross_owner"] = "+0"

    # + 10 任一身份外锚
    anchor_cnt = sum([
        sig["has_public_email"], sig["has_blog"], sig["has_twitter"], sig["has_hireable"]
    ])
    if anchor_cnt >= 2:
        score += 10
        ev["external_anchor"] = "+10"
    elif anchor_cnt >= 1:
        score += 5
        ev["external_anchor"] = "+5"
    else:
        ev["external_anchor"] = "+0"

    # + 10 没有 burst 信号（事件不集中在同一天）
    burst_ratio = (
        sig["events_in_peak_24h"] / max(1, sig["performed_events_count"] + sig["received_events_count"])
    )
    if burst_ratio < 0.5:
        score += 10
        ev["no_burst"] = "+10"
    else:
        ev["no_burst"] = f"+0 (peak_24h_ratio={burst_ratio:.2f})"

    # ---- 扣分 ----
    # 1) 全部事件集中 24h
    if sig["performed_events_count"] + sig["received_events_count"] > 30 and burst_ratio > 0.7:
        score -= 30
        ev["burst_penalty"] = f"-30 (events_in_peak_24h={sig['events_in_peak_24h']})"

    # 2) <7 天小号 + 大量事件
    if sig["account_age_days"] < 7 and (sig["performed_events_count"] + sig["received_events_count"]) >= 50:
        score -= 25
        ev["young_hyper_penalty"] = f"-25 (age={sig['account_age_days']}d)"

    # 3) 买粉嫌疑
    if sig["follower_following_ratio"] < 0.05 and sig["followers"] > 50:
        score -= 20
        ev["follower_suspect_penalty"] = f"-20 (ratio={sig['follower_following_ratio']:.3f})"

    return DimResult(_clip(score), ev, notes)


def score_activity(sig: dict[str, Any]) -> DimResult:
    """活跃度 = 量化 6 个月内做出的事。"""
    ev: dict[str, Any] = {}
    notes: list[str] = []
    perf = sig["performed_events_count"]
    recv = sig["received_events_count"]

    # 50% 主动事件量
    pts_events = _log_scale(perf, scale_at=15, max_at=150) // 2
    score = pts_events
    ev[f"performed_events={perf}"] = f"+{pts_events}"

    # 25% 事件类型多样性
    type_div = sig["performed_event_type_diversity"]
    if type_div >= 6:
        pts_type = 25
    elif type_div >= 4:
        pts_type = 18
    elif type_div >= 2:
        pts_type = 10
    elif type_div == 1:
        pts_type = 4
    else:
        pts_type = 0
    score += pts_type
    ev[f"event_type_diversity={type_div}"] = f"+{pts_type}"

    # 15% 覆盖天数
    days = sig["distinct_event_days"]
    pts_days = _log_scale(days, scale_at=10, max_at=60) // 7  # 0-15
    score += pts_days
    ev[f"distinct_event_days={days}"] = f"+{pts_days}"

    # 10% 接收事件（说明别人注意到自己）
    pts_recv = _log_scale(recv, scale_at=8, max_at=80) // 10
    score += pts_recv
    ev[f"received_events={recv}"] = f"+{pts_recv}"

    return DimResult(_clip(score), ev, notes)


def score_capability(sig: dict[str, Any]) -> DimResult:
    """代码能力 = 原创 repo + 拿到 star + 语言种类 + 协作。"""
    ev: dict[str, Any] = {}
    notes: list[str] = []

    own = sig["own_repos_count"]
    star = sig["total_stars_on_own"]
    lang = sig["distinct_languages"]
    own_with_star = sig["own_repos_with_star_ge_1"]

    # 30% 原创 repo 数
    pts_own = _log_scale(own, scale_at=3, max_at=15) * 0.30
    score = pts_own
    ev[f"own_repos={own}"] = f"+{pts_own:.1f}"

    # 35% 累计 star（最强信号）
    pts_star = _log_scale(star, scale_at=10, max_at=500) * 0.35
    score += pts_star
    ev[f"stars_on_own={star}"] = f"+{pts_star:.1f}"

    # 20% 语言数
    pts_lang = _log_scale(lang, scale_at=2, max_at=8) * 0.20
    score += pts_lang
    ev[f"languages={lang}"] = f"+{pts_lang:.1f}"

    # 15% 拿到 star 的 repo 数
    pts_own_with_star = _log_scale(own_with_star, scale_at=2, max_at=10) * 0.15
    score += pts_own_with_star
    ev[f"own_with_ge1_star={own_with_star}"] = f"+{pts_own_with_star:.1f}"

    return DimResult(_clip(score), ev, notes)


def score_breadth(sig: dict[str, Any]) -> DimResult:
    """广度：涉及到的 owner 数 / 组织数 / 语言数 / 主题数。"""
    ev: dict[str, Any] = {}

    owners = sig["distinct_event_owners"]
    orgs = sig["orgs_count"]
    lang = sig["distinct_languages"]
    topics = sig["topics_diversity"]

    pts_owner = _log_scale(owners, scale_at=5, max_at=30) * 0.35
    pts_org = _log_scale(orgs, scale_at=1, max_at=5) * 0.20
    pts_lang = _log_scale(lang, scale_at=2, max_at=8) * 0.20
    pts_topic = _log_scale(topics, scale_at=4, max_at=20) * 0.25

    total = pts_owner + pts_org + pts_lang + pts_topic
    ev[f"distinct_owners={owners}"] = f"+{pts_owner:.1f}"
    ev[f"orgs={orgs}"] = f"+{pts_org:.1f}"
    ev[f"languages={lang}"] = f"+{pts_lang:.1f}"
    ev[f"topics={topics}"] = f"+{pts_topic:.1f}"
    return DimResult(_clip(total), ev, [])


def score_longevity(sig: dict[str, Any]) -> DimResult:
    """历史：账号年龄曲线。一个 5+ 年号即拿满分。"""
    days = sig["account_age_days"]
    years = days / 365.25
    if years >= 5:
        score = 100
    elif years >= 4:
        score = 90
    elif years >= 3:
        score = 78
    elif years >= 2:
        score = 62
    elif years >= 1:
        score = 40
    elif days >= 180:
        score = 25
    elif days >= 90:
        score = 15
    elif days >= 30:
        score = 8
    else:
        score = 3
    return DimResult(score, {f"age_days={days}": f"{years:.2f}y → {score}"}, [])


def score_identity(sig: dict[str, Any]) -> DimResult:
    """身份锚定：profile 上能否找到 lab 实际身份的指针。"""
    ev: dict[str, Any] = {}
    score = 0.0

    if sig["has_blog"]:
        score += 20
        ev["blog"] = "+20"
    if sig["has_twitter"]:
        score += 15
        ev["twitter"] = "+15"
    if sig["has_public_email"]:
        score += 20
        ev["public_email"] = "+20"
    if sig["has_hireable"]:
        score += 10
        ev["hireable"] = "+10"
    if sig["gpg_key_count"] > 0:
        score += 20
        ev["gpg_keys"] = f"+20 ({sig['gpg_key_count']})"
    elif sig["ssh_key_count"] > 0:
        score += 8
        ev["ssh_keys"] = f"+8 ({sig['ssh_key_count']})"
    if sig["has_location_or_company"]:
        score += 15
        ev["location_or_company"] = "+15"

    return DimResult(_clip(score), ev, [])


# ====================================================================
# 综合

DIM_SCORERS = {
    "authenticity": score_authenticity,
    "activity": score_activity,
    "capability": score_capability,
    "breadth": score_breadth,
    "longevity": score_longevity,
    "identity": score_identity,
}


def compute_scores(signals: dict[str, Any], weights: dict | None = None) -> dict[str, Any]:
    """主入口：输入 collect_signals() 的输出，返回六维 + total + evidence。"""
    weights = weights or DEFAULT_WEIGHTS
    dims: dict[str, int] = {}
    evidences: dict[str, Any] = {}
    notes_all: dict[str, list] = {}

    for name, fn in DIM_SCORERS.items():
        r = fn(signals)
        dims[name] = r.score
        evidences[name] = r.evidence
        notes_all[name] = r.notes

    total = sum(dims[k] * weights.get(k, 0) for k in dims) / 100.0

    return {
        "dimensions": dims,
        "total": _clip(total),
        "evidence": evidences,
        "weights_used": weights,
    }
