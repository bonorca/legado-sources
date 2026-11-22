"""每次检测后写进仓库的记录：STATUS.md（给人看）和 reports/history.jsonl（每次一行）。

用法：
  python tools/report.py 检测结果.json [--maint 自动维护报告.md] [--trigger schedule|push|manual]
        [--run-url 运行记录地址] [--root 仓库目录] [--commit-msg 输出提交信息的文件]
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STAGES = ["搜索", "详情", "目录", "正文", "发现"]
BJ = timezone(timedelta(hours=8))
TRIGGER = {"schedule": "每日定时检测", "push": "推送后检测", "workflow_dispatch": "手动检测", "manual": "本地检测"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--maint")
    ap.add_argument("--trigger", default="manual")
    ap.add_argument("--run-url", default="")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--commit-msg")
    a = ap.parse_args()

    root = Path(a.root)
    now = datetime.now(BJ)
    results = json.loads(Path(a.results).read_text(encoding="utf-8"))
    maint = []
    if a.maint and Path(a.maint).exists():
        maint = [ln[2:] for ln in Path(a.maint).read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
    enabled = {s["bookSourceUrl"]: s.get("enabled", True)
               for s in json.loads((root / "all.json").read_text(encoding="utf-8"))}
    passed = sum(r["ok"] for r in results)
    failed = [r["name"] for r in not_ok(results)]

    # 历史：每次一行
    hist_file = root / "reports" / "history.jsonl"
    hist_file.parent.mkdir(exist_ok=True)
    entry = {"time": now.isoformat(timespec="minutes"), "trigger": a.trigger, "total": len(results),
             "passed": passed, "failed": failed, "actions": maint}
    with hist_file.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    history = [json.loads(ln) for ln in hist_file.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # STATUS.md
    L = ["# 书源检测", ""]
    run = f" · [运行记录]({a.run_url})" if a.run_url else ""
    L.append(f"最近一次：{now:%Y-%m-%d %H:%M}（北京时间）· {TRIGGER.get(a.trigger, a.trigger)} · "
             f"**{passed}/{len(results)} 通过**{run}")
    L += ["", "| 书源 | " + " | ".join(STAGES) + " | 状态 |", "|---" * (len(STAGES) + 2) + "|"]
    for r in results:
        cells = ["❌" if s in r["failed"] else ("—" if s in r.get("skipped", []) else "✅") for s in STAGES]
        state = "启用" if enabled.get(r["url"], True) else "已停用"
        L.append(f"| {r['name']} | " + " | ".join(cells) + f" | {state} |")
    bad = not_ok(results)
    if bad:
        L += ["", "## 没通过的项目", ""]
        for r in bad:
            for s in STAGES:
                for m in r["failed"].get(s, []):
                    L.append(f"- {r['name']} / {s}：{m}")
    if maint:
        L += ["", "## 自动维护", ""] + [f"- {x}" for x in maint]
    # 近 30 天：每天取最后一次检测
    days = {}
    for h in history:
        days[h["time"][:10]] = h
    recent = sorted(days.items())[-30:]
    if recent:
        L += ["", "## 最近 30 天", "", "| 日期 | 通过 | 没通过的书源 |", "|---|---|---|"]
        for d, h in reversed(recent):
            L.append(f"| {d} | {h['passed']}/{h['total']} | {'、'.join(h['failed']) or '—'} |")
    (root / "STATUS.md").write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")

    if a.commit_msg:
        msg = f"Daily check {now:%Y-%m-%d}: {passed}/{len(results)} passed"
        prev = Path(a.commit_msg).read_text(encoding="utf-8").strip() if Path(a.commit_msg).exists() else ""
        if prev.startswith("Auto-maintain: "):
            msg += "; " + prev[len("Auto-maintain: "):]
        Path(a.commit_msg).write_text(msg, encoding="utf-8")
    print(f"STATUS.md 已更新：{passed}/{len(results)} 通过")


def not_ok(results):
    return [r for r in results if not r["ok"]]


if __name__ == "__main__":
    main()
