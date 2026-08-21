"""对 scorer.py 的打分测试。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scorer import compute_scores
from app.signals import collect_signals


FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _score_of(fixture_name):
    fx = _load(fixture_name)
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
    return compute_scores(sigs)


def test_real_user_high_authenticity():
    res = _score_of("torvalds.json")
    auth = res["dimensions"]["authenticity"]
    assert auth >= 70, f"expected torvalds authenticity≥70, got {auth}\n{res['evidence']}"


def test_real_user_high_total():
    res = _score_of("torvalds.json")
    assert res["total"] >= 55
    assert res["total"] <= 100


def test_fake_bot_low_authenticity():
    res = _score_of("fake_bot.json")
    auth = res["dimensions"]["authenticity"]
    assert auth <= 35, f"expected fake_bot authenticity≤35, got {auth}\n{res['evidence']}"


def test_fake_bot_low_total():
    res = _score_of("fake_bot.json")
    assert res["total"] <= 30


def test_dimension_keys_present():
    res = _score_of("torvalds.json")
    dims = res["dimensions"]
    for k in ["authenticity", "activity", "capability", "breadth", "longevity", "identity"]:
        assert k in dims, f"missing {k}"
        assert 0 <= dims[k] <= 100


def test_real_vs_fake_differential():
    """真实性维度：真人必须显著高于假号。"""
    real = _score_of("torvalds.json")["dimensions"]["authenticity"]
    fake = _score_of("fake_bot.json")["dimensions"]["authenticity"]
    assert real - fake >= 30, f"real({real}) - fake({fake}) must be ≥30"
