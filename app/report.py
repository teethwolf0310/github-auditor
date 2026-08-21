"""从 scorecard 字典渲染 markdown 报告。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

DIM_LABELS = {
    "authenticity": "真实性",
    "activity": "活跃度",
    "capability": "代码能力",
    "breadth": "广度",
    "longevity": "历史",
    "identity": "身份锚定",
}

DIM_ORDER = ["authenticity", "activity", "capability", "breadth", "longevity", "identity"]


def _bar(score: int, width: int = 20) -> str:
    filled = round(score * width / 100)
    return "█" * filled + "░" * (width - filled)


def render_markdown(sc: dict[str, Any]) -> str:
    """输入 collector.evaluate() 输出的 dict（含 scores/evidence/...）。"""
    handle = sc.get("handle", "?")
    s = sc.get("scores") or {}
    ev = sc.get("evidence") or {}
    computed_at = sc.get("computed_at") or ""
    fetched_at = sc.get("fetched_at") or f"req_used={sc.get('req_used')}"

    lines: list[str] = []
    lines.append(f"# GitHub 账号评估 — @{handle}")
    lines.append("")
    if sc.get("name"):
        lines.append(f"**{sc['name']}** ({sc.get('type', 'User')})  ")
    lines.append(f"评估时间 `{computed_at}` / 数据抓取 `{fetched_at}`")
    lines.append("")

    # 汇总
    lines.append("## 总分")
    lines.append("")
    total = s.get("total", 0)
    lines.append(f"**{total}** / 100  `{_bar(total)}`")
    lines.append("")

    # 六维
    lines.append("## 各维度 (0–100)")
    lines.append("")
    lines.append("| 维度 | 得分 | 图 |")
    lines.append("|---|---|---|")
    for dim in DIM_ORDER:
        sc_dim = s.get(dim, 0)
        lines.append(f"| {DIM_LABELS[dim]} `{dim}` | **{sc_dim}** | `{_bar(sc_dim, 14)}` |")
    lines.append("")

    # 解释
    lines.append("## 解释（可解释性证据）")
    for dim in DIM_ORDER:
        sub = ev.get(dim) or {}
        if not sub:
            continue
        lines.append("")
        lines.append(f"### {DIM_LABELS[dim]} `{dim}` = {s.get(dim, 0)}")
        for k, v in sub.items():
            lines.append(f"- `{k}` → `{v}`")
    lines.append("")

    # 建议
    lines.append("## 判读建议")
    auth = s.get("authenticity", 0)
    if auth >= 80:
        verdict = "高"
    elif auth >= 60:
        verdict = "中等"
    elif auth >= 40:
        verdict = "低"
    else:
        verdict = "可疑（倾向 bot/假号）"
    lines.append(f"- 真实性 **{verdict}**（{auth}）" + ("" if auth >= 80 or auth < 40 else "，建议人工复核"))
    lines.append("")

    return "\n".join(lines)
