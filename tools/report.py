"""每次检测后写进仓库的记录：README.md 底部的「书源状态」（给人看）和 reports/history.jsonl（每次一行）。

README 里只改 <!-- 书源状态 开始 --> 和 <!-- 书源状态 结束 --> 之间的内容，上面的文字不动；
没有这两行标记时，会把它们加到 README 最后。

用法：
  python tools/report.py 检测结果.json [--maint 自动维护报告.md] [--trigger schedule|push|manual]
        [--run-url 运行记录地址] [--root 仓库目录] [--commit-msg 输出提交信息的文件]
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STAGES = ["搜索", "详情", "目录", "正文", "发现"]
TZ = timezone(timedelta(hours=-7))  # 显示用 UTC-7
TRIGGER = {"schedule": "每日定时检测", "push": "推送后检测", "workflow_dispatch": "手动检测", "manual": "本地检测"}
BEGIN = "<!-- 书源状态 开始（下面的内容由检测自动生成，改了会被覆盖） -->"
END = "<!-- 书源状态 结束 -->"


def cell(r, s):
    if s in r["failed"]:
        return "❌"
    if s in r.get("blocked", {}):
        return "⚠️"
    return "—" if s in r.get("skipped", []) else "✅"


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
    now = datetime.now(TZ)
    results = json.loads(Path(a.results).read_text(encoding="utf-8"))
    maint = []
    if a.maint and Path(a.maint).exists():
        maint = [ln[2:] for ln in Path(a.maint).read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
    enabled = {s["bookSourceUrl"]: s.get("enabled", True)
               for s in json.loads((root / "all.json").read_text(encoding="utf-8"))}
    status_file = root / "status.json"
    status = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
    passed = sum(r["ok"] and not r.get("blocked") for r in results)
    failed = [r["name"] for r in results if not r["ok"]]
    limited = [r["name"] for r in results if r["ok"] and r.get("blocked")]

    # 历史：每次一行
    hist_file = root / "reports" / "history.jsonl"
    hist_file.parent.mkdir(exist_ok=True)
    entry = {"time": now.isoformat(timespec="minutes"), "trigger": a.trigger, "total": len(results),
             "passed": passed, "failed": failed, "limited": limited, "actions": maint}
    with hist_file.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    history = [json.loads(ln) for ln in hist_file.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # README 底部
    L = ["## 书源状态", ""]
    run = f" · [运行记录]({a.run_url})" if a.run_url else ""
    count = f"**{passed}/{len(results)} 正常**" + (f"，{len(limited)} 个受限" if limited else "") \
            + (f"，{len(failed)} 个有问题" if failed else "")
    L.append(f"最近一次检测：{now:%Y-%m-%d %H:%M}（UTC-7）· {TRIGGER.get(a.trigger, a.trigger)} · {count}{run}")
    L += ["", "| 书源 | " + " | ".join(STAGES) + " | 状态 |", "|---" * (len(STAGES) + 2) + "|"]
    for r in results:
        st = status.get(r["url"], {})
        if not enabled.get(r["url"], True):
            state = "已停用"
        elif not r["ok"]:
            state = f"有问题（第 {st['fail_streak']} 天）" if st.get("fail_streak") else "有问题"
        elif r.get("blocked"):
            state = f"受限（连续 {st['blocked_streak']} 天）" if st.get("blocked_streak", 0) > 1 else "受限"
        else:
            state = "正常"
        L.append(f"| {r['name']} | " + " | ".join(cell(r, s) for s in STAGES) + f" | {state} |")
    notes = [f"- ❌ {r['name']} / {s}：{m}" for r in results for s in STAGES for m in r["failed"].get(s, [])]
    notes += [f"- ⚠️ {r['name']} / {s}：{m}" for r in results for s in STAGES for m in r.get("blocked", {}).get(s, [])]
    if notes:
        L += ["", "**问题**", ""] + notes
    if maint:
        L += ["", "**自动维护**", ""] + [f"- {x}" for x in maint]
    # 近 30 天：每天取最后一次检测
    days = {}
    for h in history:
        days[h["time"][:10]] = h
    recent = sorted(days.items())[-30:]
    if recent:
        L += ["", "<details><summary>最近 30 天</summary>", "",
              "| 日期 | 正常 | 受限 | 有问题 |", "|---|---|---|---|"]
        for d, h in reversed(recent):
            L.append(f"| {d} | {h['passed']}/{h['total']} | {'、'.join(h.get('limited', [])) or '—'} "
                     f"| {'、'.join(h['failed']) or '—'} |")
        L += ["", "</details>"]
    write_readme(root / "README.md", "\n".join(L))

    if a.commit_msg:
        msg = f"Daily check {now:%Y-%m-%d}: {passed}/{len(results)} passed"
        prev = Path(a.commit_msg).read_text(encoding="utf-8").strip() if Path(a.commit_msg).exists() else ""
        if prev.startswith("Auto-maintain: "):
            msg += "; " + prev[len("Auto-maintain: "):]
        Path(a.commit_msg).write_text(msg, encoding="utf-8")
    print(f"README 书源状态已更新：{passed}/{len(results)} 正常，受限 {len(limited)}，有问题 {len(failed)}")


def write_readme(path, section):
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if BEGIN in text and END in text:
        head = text[:text.index(BEGIN)]
        tail = text[text.index(END) + len(END):]
    else:
        head, tail = text.rstrip("\n") + "\n\n", "\n"
    path.write_text(head + BEGIN + "\n\n" + section + "\n\n" + END + (tail if tail.strip() else "\n"),
                    encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
