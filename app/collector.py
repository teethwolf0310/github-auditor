"""一个用户的完整评估流水线：拉数据 → 入库 → 抽信号 → 打分 → 写 scorecard。

v1 是同步实现。批量评估在外层 Python 串行调用 + 时间 sleep。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import Config
from .github import BudgetExceededError, GitHubClient, RateLimitError
from .models import EventRecord, RepoRecord, Scorecard, UserRecord
from .scorer import compute_scores
from .signals import collect_signals

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers

def _parse_dt(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        s2 = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


# ---------------------------------------------------------------- public

class NotFoundError(Exception):
    pass


class Evaluator:
    DEFAULT_TTL_HOURS = 6

    def __init__(self, cfg: Config, db: Session, gh: GitHubClient):
        self.cfg = cfg
        self.db = db
        self.gh = gh

    # ------------------------------------------------ TTL
    def _recent_scorecard(self, user_id: int) -> Optional[Scorecard]:
        stmt = (
            select(Scorecard)
            .where(Scorecard.user_id == user_id)
            .order_by(Scorecard.computed_at.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def is_fresh(self, user: UserRecord, ttl_hours: int = DEFAULT_TTL_HOURS) -> bool:
        sc = self._recent_scorecard(user.id)
        if not sc:
            return False
        if not user.fetched_at:
            return False
        return (datetime.utcnow() - user.fetched_at) < timedelta(hours=ttl_hours)

    # ------------------------------------------------ main
    def evaluate(self, handle: str, force: bool = False) -> dict[str, Any]:
        """对一个 GitHub handle 完整评估。

        Returns: dict(scorecard summary + dimensions + evidence)。
        """
        handle = handle.strip().lstrip("@")
        if not handle:
            raise ValueError("empty handle")

        user = self.db.execute(select(UserRecord).where(UserRecord.handle == handle)).scalar_one_or_none()

        # 命中缓存且未 force → 直接返回最近 scorecard
        if user and not force and self.is_fresh(user):
            sc = self._recent_scorecard(user.id)
            log.info("cache hit, reuse scorecard (handle=%s, computed_at=%s)", handle, sc.computed_at)
            return self._scorecard_to_dict(user, sc)

        log.info("(%s) fetching from GitHub", handle)
        self.gh.reset_eval()
        try:
            payload = self._fetch_bundle(handle)
        except NotFoundError:
            # 不存在的 user：删掉旧数据
            if user:
                self.db.delete(user)
                self.db.commit()
            raise
        except (RateLimitError, BudgetExceededError) as e:
            # 中途限流：保留旧数据，向上抛
            log.error("evaluation aborted for %s: %s", handle, e)
            raise

        # ---- upsert user
        user = self._upsert_user(user, handle, payload["profile"])

        # ---- 重建 repos/events
        self._replace_repos(user, payload["repos"])
        self._replace_events(user, payload["performed_events"],
                             payload["received_events"])

        # ---- 抽 signals
        signals = collect_signals(
            profile=payload["profile"],
            repos=payload["repos"],
            performed_events=payload["performed_events"],
            received_events=payload["received_events"],
            orgs=payload["orgs"],
            gpg_keys=payload["gpg_keys"],
            ssh_keys=payload["ssh_keys"],
            top_own_repos_with_commits=payload["top_own_with_commits"],
        )

        # ---- 打分
        result = compute_scores(signals, weights=self.cfg.weights)

        # ---- 写 scorecard
        sc = Scorecard(
            user_id=user.id,
            req_used=self.gh.req_used,
            authenticity=result["dimensions"]["authenticity"],
            activity=result["dimensions"]["activity"],
            capability=result["dimensions"]["capability"],
            breadth=result["dimensions"]["breadth"],
            longevity=result["dimensions"]["longevity"],
            identity=result["dimensions"]["identity"],
            total=result["total"],
            evidence_json=json.dumps(result["evidence"], ensure_ascii=False, indent=2),
        )
        self.db.add(sc)
        self.db.commit()
        self.db.refresh(user)
        self.db.refresh(sc)

        return self._scorecard_to_dict(user, sc)

    # ------------------------------------------------ fetch from GH

    def _fetch_bundle(self, handle: str) -> dict[str, Any]:
        gh = self.gh

        # 1) profile
        profile, hdr = gh.get(f"/users/{handle}")
        if profile is None:
            raise NotFoundError(handle)
        if not isinstance(profile, dict):
            raise RuntimeError(f"unexpected profile payload for {handle}")

        # 2) repos（owner + sort=pushed）
        repos = gh.get_all_pages(
            f"/users/{handle}/repos",
            params={"type": "owner", "sort": "pushed"},
            max_pages=self.cfg.max_repo_pages,
        )

        # 3) 事件流
        performed = gh.get_all_pages(
            f"/users/{handle}/events/public",
            max_pages=self.cfg.max_event_pages,
        )
        received = gh.get_all_pages(
            f"/users/{handle}/received_events/public",
            max_pages=self.cfg.max_event_pages,
        )

        # 4) orgs
        orgs = gh.get_all_pages(f"/users/{handle}/orgs", max_pages=1)

        # 5) 公钥
        gpg_keys, _ = gh.get(f"/users/{handle}/gpg_keys")
        ssh_keys, _ = gh.get(f"/users/{handle}/keys")

        # 6) 抽 commits 做时间序列
        #   (a) 优先：自己的 top own repo（author=handle 采样）
        #   (b) 兜底：若 own repo 全空（典型 org 工程师，自己 repo 没人 push），
        #       从 performed PushEvent 里挑 Push 密度最高的 2 个非 own repo 再采
        own_sorted = sorted(
            [r for r in repos if not r.get("fork")],
            key=lambda r: (
                (r.get("stargazers_count") or 0) + (r.get("forks_count") or 0),
                r.get("pushed_at") or "",
            ),
            reverse=True,
        )
        top_n = self.cfg.top_own_repos_sample
        own_sampled: list[dict] = []
        non_empty_own = 0
        for r in own_sorted[: max(top_n * 2, top_n + 3)]:
            if non_empty_own >= top_n:
                break
            try:
                commits = gh.get_all_pages(
                    f"/repos/{r['full_name']}/commits",
                    params={"author": handle},
                    max_pages=1,
                )
            except BudgetExceededError:
                log.warning("budget exhausted before commits of %s — using empty", r["full_name"])
                commits = []
            own_sampled.append({"repo": r, "commits": commits, "scope": "own"})
            if commits:
                non_empty_own += 1

        # 若全部 own repo 都没命中 commits（典型"在 org 仓里工作的工程师"）
        if non_empty_own == 0:
            from collections import Counter
            # 把 PR / Review / Push 等"实写操作"事件计数；单看 PushEvent 不行 —
            # PR 合并后 commits 落到 base 仓，PR 提交者在 base repo 里就有 author
            COLLAB_KINDS = {
                "PushEvent",
                "PullRequestEvent",
                "PullRequestReviewEvent",
                "PullRequestReviewCommentEvent",
                "IssuesEvent",
                "IssueCommentEvent",
                "CreateEvent",
            }
            cnt: Counter = Counter()
            for e in performed:
                if e.get("type") not in COLLAB_KINDS:
                    continue
                rn = (e.get("repo") or {}).get("name") or ""
                if "/" not in rn:
                    continue
                if rn.split("/", 1)[0].lower() == handle.lower():
                    continue  # 自己 repo 已试过
                cnt[rn] += 1
            for repo_name, _cntr in cnt.most_common(3):
                try:
                    commits = gh.get_all_pages(
                        f"/repos/{repo_name}/commits",
                        params={"author": handle},
                        max_pages=1,
                    )
                except BudgetExceededError:
                    log.warning("budget exhausted before external commits of %s", repo_name)
                    commits = []
                if commits:
                    own_sampled.append({
                        "repo": {"full_name": repo_name},
                        "commits": commits,
                        "scope": "events",
                    })

        top_own_with_commits = own_sampled[: top_n + 3]  # top_n own + 最多 3 个协作仓

        return {
            "profile": profile,
            "repos": repos,
            "performed_events": performed,
            "received_events": received,
            "orgs": orgs,
            "gpg_keys": gpg_keys or [],
            "ssh_keys": ssh_keys or [],
            "top_own_with_commits": top_own_with_commits,
        }

    # ------------------------------------------------ upsert

    def _upsert_user(self, user: Optional[UserRecord], handle: str, profile: dict) -> UserRecord:
        now = datetime.utcnow()
        if user is None:
            user = UserRecord(handle=handle)
            self.db.add(user)

        user.gh_id = profile.get("id")
        user.avatar_url = profile.get("avatar_url") or ""
        user.name = profile.get("name") or ""
        user.email = profile.get("email") or ""
        user.blog = profile.get("blog") or ""
        user.company = profile.get("company") or ""
        user.location = profile.get("location") or ""
        user.bio = profile.get("bio") or ""
        user.twitter_username = profile.get("twitter_username") or ""
        user.hireable = 1 if profile.get("hireable") else (0 if profile.get("hireable") is False else -1)
        user.type_ = profile.get("type") or "User"
        user.site_admin = 1 if profile.get("site_admin") else 0
        user.followers = int(profile.get("followers") or 0)
        user.following = int(profile.get("following") or 0)
        user.public_repos = int(profile.get("public_repos") or 0)
        user.public_gists = int(profile.get("public_gists") or 0)
        user.gh_created_at = _parse_dt(profile.get("created_at"))
        user.gh_updated_at = _parse_dt(profile.get("updated_at"))
        user.fetched_at = now
        user.raw_json = json.dumps(profile, ensure_ascii=False)
        self.db.flush()
        return user

    def _replace_repos(self, user: UserRecord, repos: list[dict]) -> None:
        self.db.execute(delete(RepoRecord).where(RepoRecord.user_id == user.id))
        for r in repos:
            topics = ",".join(r.get("topics") or [])
            self.db.add(RepoRecord(
                user_id=user.id,
                full_name=r.get("full_name") or "",
                name=r.get("name") or "",
                # Owner could be org (owner repos) — keep raw
                owner_login=(r.get("owner") or {}).get("login") or "",
                is_fork=1 if r.get("fork") else 0,
                is_archived=1 if r.get("archived") else 0,
                primary_language=r.get("language") or "",
                languages_total_kb=int(r.get("size") or 0),
                stargazers=int(r.get("stargazers_count") or 0),
                forks=int(r.get("forks_count") or 0),
                open_issues=int(r.get("open_issues_count") or 0),
                subscribers=int(r.get("subscribers_count") or 0),
                size_kb=int(r.get("size") or 0),
                description=r.get("description") or "",
                topics=topics,
                gh_created_at=_parse_dt(r.get("created_at")),
                gh_pushed_at=_parse_dt(r.get("pushed_at")),
                license_spdx=(r.get("license") or {}).get("spdx_id") or "",
                has_homepage=1 if r.get("homepage") else 0,
                default_branch=r.get("default_branch") or "",
                raw_json=json.dumps(r, ensure_ascii=False)[:100 * 1024],  # cap
            ))
        self.db.flush()

    def _replace_events(self, user: UserRecord,
                        performed: list[dict],
                        received: list[dict]) -> None:
        self.db.execute(delete(EventRecord).where(EventRecord.user_id == user.id))
        for direction, lst in (("performed", performed), ("received", received)):
            seen: set[str] = set()
            for e in lst:
                eid = str(e.get("id") or "")
                # 同一 (user, direction, gh_event_id) 去重：GitHub 公共事件流偶然返回重复项
                if eid and eid in seen:
                    continue
                if eid:
                    seen.add(eid)
                self.db.add(EventRecord(
                    user_id=user.id,
                    direction=direction,
                    gh_event_id=eid,
                    kind=e.get("type") or "",
                    repo_full=(e.get("repo") or {}).get("name") or "",
                    actor_login=(e.get("actor") or {}).get("login") or "",
                    event_ts=_parse_dt(e.get("created_at")),
                    payload_json=json.dumps(
                        {"action": (e.get("payload") or {}).get("action")},
                        ensure_ascii=False,
                    ),
                ))
        self.db.flush()

    # ------------------------------------------------ dict
    def _scorecard_to_dict(self, user: UserRecord, sc: Scorecard) -> dict[str, Any]:
        return {
            "handle": user.handle,
            "gh_id": user.gh_id,
            "name": user.name,
            "type": user.type_,
            "fetched_at": user.fetched_at.isoformat() if user.fetched_at else None,
            "computed_at": sc.computed_at.isoformat() if sc.computed_at else None,
            "req_used": sc.req_used,
            "scores": {
                "authenticity": sc.authenticity,
                "activity": sc.activity,
                "capability": sc.capability,
                "breadth": sc.breadth,
                "longevity": sc.longevity,
                "identity": sc.identity,
                "total": sc.total,
            },
            "evidence": json.loads(sc.evidence_json) if sc.evidence_json else {},
        }


# ---------------------------------------------------------------- entry

def evaluate_handle(cfg: Config, db: Session, handle: str, force: bool = False) -> dict[str, Any]:
    gh = GitHubClient(
        token=cfg.github_token,
        proxy=cfg.proxy,
        max_requests_per_eval=cfg.max_requests_per_eval,
    )
    ev = Evaluator(cfg, db, gh)
    return ev.evaluate(handle, force=force)
