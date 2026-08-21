"""CLI / FastAPI 双入口。

用法：
  # 脚本模式（默认）
  python -m app.main eval <handle> [--force] [--report]
  python -m app.main batch ./handles.txt [--force] [--sleep 0.5]
  python -m app.main report <handle>           # 已有数据出 markdown
  python -m app.main list
  python -m app.main purge <handle>

  # 服务模式
  python -m app.main --serve [--host 127.0.0.1] [--port 8010]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from sqlalchemy import select

from .collector import Evaluator, NotFoundError, evaluate_handle
from .config import load_config
from .db import get_session
from .github import BudgetExceededError, GitHubClient, RateLimitError
from .models import Scorecard, UserRecord
from .report import render_markdown

log = logging.getLogger("github-auditor")


def _scorecard_payload(u: UserRecord, sc: Scorecard) -> dict:
    """把 UserRecord + Scorecard 重组为 evaluate() 那样的 dict。"""
    return {
        "handle": u.handle,
        "gh_id": u.gh_id,
        "name": u.name,
        "type": u.type_,
        "fetched_at": u.fetched_at.isoformat() if u.fetched_at else None,
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


# ================================================================ CLI handlers

def cmd_eval(args) -> int:
    cfg = load_config()
    db = get_session(cfg)
    try:
        result = evaluate_handle(cfg, db, args.handle, force=args.force)
    except NotFoundError:
        print(f"❌ 用户 {args.handle} 不存在（GitHub 404）", file=sys.stderr)
        return 2
    except BudgetExceededError as e:
        print(f"❌ 请求预算超支: {e}", file=sys.stderr)
        return 3
    except RateLimitError as e:
        print(f"❌ 限流，等节点后再试: {e}", file=sys.stderr)
        return 4

    if args.report:
        print(render_markdown(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_batch(args) -> int:
    cfg = load_config()
    db = get_session(cfg)

    handles: list[str] = []
    with open(args.file, "r", encoding="utf-8") as f:
        for line in f:
            h = line.strip().lstrip("@")
            if h and not h.startswith("#"):
                handles.append(h)

    print(f"batch evaluating {len(handles)} handles...")
    succeeded, failed = 0, 0
    for i, h in enumerate(handles, 1):
        try:
            print(f"[{i}/{len(handles)}] @{h} ...", end=" ", flush=True)
            result = evaluate_handle(cfg, db, h, force=args.force)
            total = result["scores"]["total"]
            auth = result["scores"]["authenticity"]
            print(f"total={total} auth={auth}")
            succeeded += 1
        except NotFoundError:
            print(f"404 not-found")
            failed += 1
        except (BudgetExceededError, RateLimitError) as e:
            print(f"ERROR: {e}")
            failed += 1
        except Exception as e:  # noqa
            print(f"ERROR: {type(e).__name__}: {e}")
            failed += 1
        time.sleep(args.sleep)

    print(f"\n成功 {succeeded} 失败 {failed}")
    return 0 if failed == 0 else 1


def cmd_report(args) -> int:
    cfg = load_config(require_token=False)
    db = get_session(cfg)
    user = db.execute(select(UserRecord).where(UserRecord.handle == args.handle)).scalar_one_or_none()
    if not user:
        print(f"❌ 本地没有 @{args.handle} 的评估记录，先跑 eval", file=sys.stderr)
        return 2
    sc = db.execute(
        select(Scorecard).where(Scorecard.user_id == user.id)
        .order_by(Scorecard.computed_at.desc()).limit(1)
    ).scalar_one_or_none()
    if not sc:
        print(f"❌ @{args.handle} 没有 scorecard", file=sys.stderr)
        return 3
    print(render_markdown(_scorecard_payload(user, sc)))
    return 0


def cmd_list(args) -> int:
    cfg = load_config(require_token=False)
    db = get_session(cfg)
    rows = db.execute(
        select(UserRecord, Scorecard)
        .join(Scorecard, Scorecard.user_id == UserRecord.id)
        .where(Scorecard.id.in_(
            select(Scorecard.id)
            .order_by(Scorecard.computed_at.desc())
        ))
        .order_by(UserRecord.handle)
    ).all()
    if not rows:
        print("(no evaluations yet)")
        return 0
    print(f"{'handle':<28s} {'total':>6s} {'auth':>5s} {'act':>5s} {'cap':>5s} {'age_days':>10s} {'evaluated_at':<20s}")
    print("-" * 90)
    seen_users = set()
    for u, sc in rows:
        if u.id in seen_users:
            continue
        seen_users.add(u.id)
        age_days = 0
        if u.gh_created_at:
            from datetime import datetime
            age_days = (datetime.utcnow() - u.gh_created_at).days
        eval_at = sc.computed_at.strftime("%Y-%m-%d %H:%M:%S") if sc.computed_at else "-"
        print(f"{u.handle:<28s} {sc.total:>6d} {sc.authenticity:>5d} {sc.activity:>5d} {sc.capability:>5d} {age_days:>10d} {eval_at:<20s}")
    return 0


def cmd_purge(args) -> int:
    cfg = load_config(require_token=False)
    db = get_session(cfg)
    user = db.execute(select(UserRecord).where(UserRecord.handle == args.handle)).scalar_one_or_none()
    if not user:
        print(f"@{args.handle} 不存在")
        return 0
    db.delete(user)
    db.commit()
    print(f"@{args.handle} 已删除（含 repos/events/scorecards）")
    return 0


# ================================================================ FastAPI server

def cmd_serve(args) -> int:
    cfg = load_config(require_token=False)
    try:
        from fastapi import FastAPI, HTTPException, Response
        from pydantic import BaseModel
        import uvicorn
    except ImportError:
        print("缺少 fastapi/uvicorn，pip install -r requirements.txt", file=sys.stderr)
        return 2

    # 顶层 BaseModel（不能 def 里，pydantic 2.x 用 ForwardRef 找 Local 类名）
    class EvalReq(BaseModel):
        handle: str
        force: bool = False

    app = FastAPI(title="github-auditor", version="0.1.0")

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/users")
    def list_users():
        db = get_session(cfg)
        users = db.execute(select(UserRecord).order_by(UserRecord.handle)).scalars().all()
        return [{"handle": u.handle,
                 "fetched_at": u.fetched_at.isoformat() if u.fetched_at else None} for u in users]

    def _latest_payload(db, handle: str):
        u = db.execute(select(UserRecord).where(UserRecord.handle == handle)).scalar_one_or_none()
        if not u:
            raise HTTPException(404, f"@{handle} 未评估")
        sc = db.execute(
            select(Scorecard).where(Scorecard.user_id == u.id)
            .order_by(Scorecard.computed_at.desc()).limit(1)
        ).scalar_one_or_none()
        if not sc:
            raise HTTPException(404, f"@{handle} 无 scorecard")
        return _scorecard_payload(u, sc)

    @app.get("/users/{handle}")
    def get_user(handle: str):
        return _latest_payload(get_session(cfg), handle)

    @app.get("/users/{handle}/report.md")
    def report_md(handle: str):
        payload = _latest_payload(get_session(cfg), handle)
        return Response(content=render_markdown(payload),
                        media_type="text/markdown; charset=utf-8")

    @app.post("/evaluate")
    def eval_endpoint(body: dict):
        handle = (body or {}).get("handle") or ""
        force = bool((body or {}).get("force", False))
        if not handle:
            raise HTTPException(400, "missing 'handle'")
        db = get_session(cfg)
        try:
            return evaluate_handle(cfg, db, handle, force=force)
        except NotFoundError:
            raise HTTPException(404, f"@{handle} not found")
        except (BudgetExceededError, RateLimitError) as e:
            raise HTTPException(503, str(e))

    uvicorn.run(app, host=args.host or cfg.listen_host, port=args.port or cfg.listen_port, log_level="info")
    return 0


# ================================================================ entry

def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(prog="github-auditor", description="对 GitHub 账号打分（真实性 / 活跃度 / 能力）")
    ap.add_argument("--serve", action="store_true", help="服务模式（FastAPI）")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)

    sub = ap.add_subparsers(dest="cmd")

    p_eval = sub.add_parser("eval", help="评估单个账号")
    p_eval.add_argument("handle")
    p_eval.add_argument("--force", action="store_true", help="跳过缓存，强制重拉")
    p_eval.add_argument("--report", action="store_true", help="输出 markdown 报告而不是 JSON")
    p_eval.set_defaults(fn=cmd_eval)

    p_batch = sub.add_parser("batch", help="批量评估")
    p_batch.add_argument("file", help="一行一个 handle")
    p_batch.add_argument("--force", action="store_true")
    p_batch.add_argument("--sleep", type=float, default=0.5, help="每个账号间隔秒数")
    p_batch.set_defaults(fn=cmd_batch)

    p_rep = sub.add_parser("report", help="渲染最近一次评估为 markdown")
    p_rep.add_argument("handle")
    p_rep.set_defaults(fn=cmd_report)

    p_list = sub.add_parser("list", help="列出所有已评估账号")
    p_list.set_defaults(fn=cmd_list)

    p_purge = sub.add_parser("purge", help="删除一个账号的全部本地记录")
    p_purge.add_argument("handle")
    p_purge.set_defaults(fn=cmd_purge)

    args = ap.parse_args(argv)

    if args.serve:
        return cmd_serve(args)

    if not args.cmd:
        ap.print_help()
        return 1

    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
