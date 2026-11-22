"""根据每日检测结果自动维护书源（GitHub Actions 里由 bot 运行，本地也能跑）。

做三件事：
  1. 连续 2 次检测失败的书源 → 停用（enabled=false），书源说明开头写上原因
  2. 被 bot 停用的书源重新通过检测 → 恢复启用
  3. 检测失败、而且首页跳转到了新域名 → 用新域名把整套测试再跑一遍，通过才改过去

它不会修改失效的规则（网站改版），那种情况只会停用并在 issue 里报告。
用户自己手动停用的书源（不是 bot 停的）不会被动。

用法：
  python tools/maintain.py 检测结果.json [--root 仓库目录] [--today 2026-11-22]
        [--report 报告.md] [--commit-msg 提交信息.txt]
跑完如果有改动，会重新生成 all.json。
"""
import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_source as T  # noqa: E402

DISABLE_AFTER = 2  # 连续失败几次后停用
PREFIX_RE = re.compile(r"^【自动停用[^】]*】[^\n]*\n?")


def origin(url):
    u = urlsplit(url.strip())
    return f"{u.scheme}://{u.netloc}"


def load_sources(root):
    """url -> (文件, 序号)；同时返回每个文件的内容"""
    files, index = {}, {}
    for f in sorted((root / "sources").glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        files[f] = data
        for i, s in enumerate(data):
            index[s["bookSourceUrl"]] = (f, i)
    return files, index


def first_error(failed):
    for stage in T.STAGES:
        if failed.get(stage):
            return f"{stage}：{failed[stage][0]}"[:120]
    return "未知错误"


def try_new_domain(src, key):
    """首页跳到别的域名时，换成新域名重测；通过就返回新书源，否则返回 None。"""
    old = origin(src["bookSourceUrl"])
    try:
        r = requests.get(old + "/", headers=json.loads(src.get("header") or "{}"), timeout=20, allow_redirects=True)
    except requests.RequestException:
        return None, None
    new = origin(r.url)
    if r.status_code != 200 or new == old:
        return None, None
    cand = json.loads(json.dumps(src, ensure_ascii=False).replace(old, new))
    cand["bookSourceUrl"] = new
    print(f"\n>>> {src['bookSourceName']} 的首页从 {old} 跳到了 {new}，用新域名重测")
    rep = T.test_source(cand, key)
    return (cand, new) if rep.ok else (None, new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--report")
    ap.add_argument("--commit-msg")
    a = ap.parse_args()

    root = Path(a.root)
    status_file = root / "status.json"
    kw_file = root / "tools" / "keywords.json"
    status = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
    keywords = json.loads(kw_file.read_text(encoding="utf-8"))
    results = json.loads(Path(a.results).read_text(encoding="utf-8"))
    files, index = load_sources(root)

    actions = []          # 给人看的
    short = []            # 给提交信息用的（英文短句）
    touched = set()

    for res in results:
        url, name = res["url"], res["name"]
        if url not in index:
            continue
        f, i = index[url]
        src = files[f][i]
        st = status.get(url, {"fail_streak": 0, "disabled_by_bot": False})

        if res["ok"]:
            st["fail_streak"] = 0
            st.pop("last_fail", None)
            st.pop("last_error", None)
            if st.get("disabled_by_bot"):
                src["enabled"] = True
                src["enabledExplore"] = True
                src["bookSourceComment"] = PREFIX_RE.sub("", src.get("bookSourceComment", ""))
                st["disabled_by_bot"] = False
                touched.add((f, i))
                actions.append(f"✅ 恢复启用 **{name}**：检测重新通过")
                short.append(f"re-enable {name.split()[-1]}")
        else:
            st["fail_streak"] = st.get("fail_streak", 0) + 1
            st["last_fail"] = a.today
            st["last_error"] = first_error(res["failed"])

            cand, new = try_new_domain(src, keywords.get(url, ""))
            if cand:
                was_bot_disabled = st.get("disabled_by_bot")
                if was_bot_disabled:
                    cand["enabled"] = True
                    cand["enabledExplore"] = True
                    cand["bookSourceComment"] = PREFIX_RE.sub("", cand.get("bookSourceComment", ""))
                files[f][i] = src = cand
                touched.add((f, i))
                keywords[cand["bookSourceUrl"]] = keywords.pop(url, "")
                status.pop(url, None)
                url = cand["bookSourceUrl"]
                st = {"fail_streak": 0, "disabled_by_bot": False}
                actions.append(f"🔀 **{name}** 换了域名：{origin(res['url'])} → {new}，新域名完整测试通过，已改过去")
                short.append(f"move {name.split()[-1]} to {urlsplit(new).netloc}")
            elif st["fail_streak"] >= DISABLE_AFTER and src.get("enabled", True) and not st.get("disabled_by_bot"):
                src["enabled"] = False
                src["enabledExplore"] = False
                src["bookSourceComment"] = (f"【自动停用 {a.today}】连续 {st['fail_streak']} 次检测失败，{st['last_error']}\n"
                                            + PREFIX_RE.sub("", src.get("bookSourceComment", "")))
                st["disabled_by_bot"] = True
                touched.add((f, i))
                actions.append(f"⛔ 停用 **{name}**：连续 {st['fail_streak']} 次检测失败（{st['last_error']}）")
                short.append(f"disable {name.split()[-1]}")
            else:
                note = "已停用，等它恢复" if st.get("disabled_by_bot") else f"第 {st['fail_streak']} 次失败，连续 {DISABLE_AFTER} 次才停用"
                if new:
                    note += f"；首页跳到 {new}，但新域名测试没通过，没有改"
                actions.append(f"⚠️ **{name}**：{st['last_error']}（{note}）")

        # 只记录有问题的书源，全部正常时 status.json 不变，bot 就不会每天提交
        if st.get("fail_streak") or st.get("disabled_by_bot"):
            st["name"] = name
            status[url] = st
        else:
            status.pop(url, None)

    now = int(time.time() * 1000)
    for f, i in touched:
        files[f][i]["lastUpdateTime"] = now
    for f in {f for f, _ in touched}:
        f.write_text(json.dumps(files[f], ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    new_status = json.dumps(dict(sorted(status.items())), ensure_ascii=False, indent=2) + "\n"
    if status or status_file.exists():
        old_status = status_file.read_text(encoding="utf-8") if status_file.exists() else ""
        if new_status != old_status:
            if status:
                status_file.write_text(new_status, encoding="utf-8", newline="\n")
            else:
                status_file.unlink()
    new_kw = json.dumps(keywords, ensure_ascii=False, indent=2) + "\n"
    if new_kw != kw_file.read_text(encoding="utf-8"):
        kw_file.write_text(new_kw, encoding="utf-8", newline="\n")

    if touched:
        import build  # noqa: E402  重新生成 all.json
        build.OUT = root / "all.json"
        build.ROOT = root
        (root / "all.json").write_text(build.build(), encoding="utf-8", newline="\n")

    print("\n========== 自动维护")
    for line in actions or ["全部正常，没有改动"]:
        print(line)
    if a.report:
        Path(a.report).write_text("\n".join(f"- {x}" for x in actions) + "\n" if actions else "", encoding="utf-8")
    if a.commit_msg:
        Path(a.commit_msg).write_text(("Auto-maintain: " + "; ".join(short)) if short else "", encoding="utf-8")


if __name__ == "__main__":
    main()
