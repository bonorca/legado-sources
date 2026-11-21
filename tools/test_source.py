"""在电脑上模拟阅读 APP 跑一遍书源：搜索 → 详情 → 目录 → 正文 → 发现。

只实现了本仓库书源用到的规则写法：
  @css:选择器@属性   属性可以是 text / html / ownText / 任意 HTML 属性
  text / href / src  直接取当前元素
  ##正则##替换       对结果做替换
  @js:java.t2s(result)  繁体转简体（用 opencc 模拟）
  @js:'前缀'+result     在结果前面拼一段（比如换域名）
选择器里的 :eq(n) 按 Jsoup 的意思处理（从 0 开始）。

用法：
  python tools/test_source.py sources/biquge-tw.json 剑来
"""
import json
import re
import sys
import time
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup
from opencc import OpenCC

T2S = OpenCC("t2s")
sys.stdout.reconfigure(encoding="utf-8")


def select(el, sel):
    """支持 Jsoup 的 :eq(n)，其余交给 soupsieve。"""
    m = re.search(r":eq\((\d+)\)", sel)
    if not m:
        return el.select(sel)
    head, tail = sel[: m.start()], sel[m.end():].strip()
    hits = el.select(head)
    n = int(m.group(1))
    if n >= len(hits):
        return []
    picked = hits[n]
    return select(picked, tail) if tail else [picked]


def get_attr(el, attr):
    if attr == "text":
        return re.sub(r"\s+", " ", el.get_text(" ", strip=True))
    if attr == "ownText":
        return "".join(t for t in el.find_all(string=True, recursive=False)).strip()
    if attr == "html":
        return str(el)
    v = el.get(attr)
    return v if isinstance(v, str) else (" ".join(v) if v else "")


def split_rule(rule):
    js = None
    if "@js:" in rule:
        rule, js = rule.split("@js:", 1)
    regex = None
    if "##" in rule:
        rule, regex = rule.split("##", 1)
    return rule, regex, js


def apply_post(value, regex, js):
    if regex is not None:
        pat, _, rep = regex.partition("##")
        value = re.sub(pat, rep, value)
    if js:
        js = js.strip()
        prefix = re.fullmatch(r"'([^']*)'\s*\+\s*result", js)
        if js == "java.t2s(result)":
            value = T2S.convert(value)
        elif prefix:
            value = prefix.group(1) + value
        else:
            raise NotImplementedError("测试脚本不支持这段 JS: " + js)
    return value.strip()


def get_string(el, rule):
    if not rule:
        return ""
    rule, regex, js = split_rule(rule)
    if rule.startswith("@css:"):
        body = rule[5:]
        sel, _, attr = body.rpartition("@")
        hits = select(el, sel) if sel else [el]
        vals = [get_attr(h, attr) for h in hits]
        value = "\n".join(v for v in vals if v)
    else:
        value = get_attr(el, rule)
    return apply_post(value, regex, js)


def get_elements(el, rule):
    assert rule.startswith("@css:"), rule
    return select(el, rule[5:])


class Runner:
    def __init__(self, src):
        self.src = src
        self.base = src["bookSourceUrl"]
        self.s = requests.Session()
        self.s.headers.update(json.loads(src.get("header") or "{}"))

    def fetch(self, url):
        url = urljoin(self.base + "/", url)
        for wait in [0, 5, 15, 30]:
            time.sleep(wait or 0.6)
            r = self.s.get(url, timeout=20)
            if r.status_code != 429:
                break
            print(f"     （429 限流，{wait or 5} 秒后重试）")
        r.raise_for_status()
        r.encoding = "utf-8"
        return url, BeautifulSoup(r.text, "lxml")

    def book_list(self, url, rules):
        page_url, soup = self.fetch(url)
        books = []
        for it in get_elements(soup, rules["bookList"]):
            b = {k: get_string(it, rules.get(k)) for k in
                 ["name", "author", "kind", "wordCount", "intro", "lastChapter", "coverUrl", "bookUrl"]}
            b["bookUrl"] = urljoin(page_url, b["bookUrl"])
            if b["coverUrl"]:
                b["coverUrl"] = urljoin(page_url, b["coverUrl"])
            books.append(b)
        return books


problems = []


def check(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        problems.append(msg)


def main():
    path, key = sys.argv[1], sys.argv[2]
    sources = json.load(open(path, encoding="utf-8"))
    for src in sources:
        r = Runner(src)
        print(f"===== {src['bookSourceName']}  ({src['bookSourceUrl']})")

        print(f"\n[搜索] {key}")
        url = src["searchUrl"].replace("{{key}}", quote(key)).replace("{{page}}", "1")
        books = r.book_list(url, src["ruleSearch"])
        check(len(books) > 0, f"搜到 {len(books)} 本")
        for b in books[:3]:
            print(f"     {b['name']} / {b['author']} / {b['kind']} / 最新：{b['lastChapter']}")
        check(all(b["name"] and b["bookUrl"] for b in books), "每本都有书名和链接")
        hit = next((b for b in books if b["name"] == key), books[0])
        check(T2S.convert(hit["name"]) == hit["name"], f"书名已是简体：{hit['name']}")

        url2 = src["searchUrl"].replace("{{key}}", quote(key)).replace("{{page}}", "2")
        books2 = r.book_list(url2, src["ruleSearch"])
        check(books2 and books2[0]["bookUrl"] != books[0]["bookUrl"], f"第 2 页能翻（{len(books2)} 本）")

        print(f"\n[详情] {hit['bookUrl']}")
        info_url, soup = r.fetch(hit["bookUrl"])
        ri = src["ruleBookInfo"]
        info = {k: get_string(soup, ri.get(k)) for k in
                ["name", "author", "kind", "wordCount", "lastChapter", "intro", "coverUrl", "tocUrl"]}
        for k, v in info.items():
            print(f"     {k}: {v[:60]}")
        check(info["name"] == hit["name"], "详情页书名和搜索结果一致")
        check(info["author"] == hit["author"].rstrip("."), "详情页作者和搜索结果一致")
        check(bool(info["intro"]), "有简介")
        check(bool(info["tocUrl"]), "有目录地址")

        print("\n[目录]")
        toc_url, soup = r.fetch(urljoin(info_url, info["tocUrl"]))
        rt = src["ruleToc"]
        chs = [(get_string(a, rt["chapterName"]), urljoin(toc_url, get_string(a, rt["chapterUrl"])))
               for a in get_elements(soup, rt["chapterList"])]
        check(len(chs) > 0, f"共 {len(chs)} 章")
        print(f"     第一章：{chs[0][0]}  {chs[0][1]}")
        print(f"     最后一章：{chs[-1][0]}  {chs[-1][1]}")
        check(len({u for _, u in chs}) == len(chs), "章节地址没有重复")
        check(all(n for n, _ in chs), "每章都有标题")

        print("\n[正文]")
        for name, u in [chs[0], chs[len(chs) // 2], chs[-1]]:
            _, soup = r.fetch(u)
            html = get_string(soup, src["ruleContent"]["content"])
            text = BeautifulSoup(html, "lxml").get_text("\n", strip=True)
            junk = [w for w in ["笔趣阁", "biquge", "请收藏", "最新章节", "http", "www."] if w in text]
            check(len(text) > 0, f"{name}：{len(text)} 字，开头：{text[:30]}"
                  + ("（偏短，可能是作者公告，自己看一眼）" if 0 < len(text) < 500 else ""))
            check(not junk, f"{name}：正文里没有广告字样" + (f"（发现 {junk}）" if junk else ""))
            check(T2S.convert(text) == text, f"{name}：正文是简体")
            time.sleep(0.5)

        if src.get("exploreUrl"):
            print("\n[发现]")
            for line in src["exploreUrl"].splitlines():
                title, _, u = line.partition("::")
                bs = r.book_list(u.replace("{{page}}", "1"), src["ruleExplore"])
                bs2 = r.book_list(u.replace("{{page}}", "2"), src["ruleExplore"])
                ok = len(bs) > 0 and all(b["name"] and b["bookUrl"] for b in bs)
                check(ok and bs2 and bs2[0]["bookUrl"] != bs[0]["bookUrl"],
                      f"{title}：第1页 {len(bs)} 本，第2页 {len(bs2)} 本，例：{bs[0]['name'] if bs else '-'} / {bs[0]['author'] if bs else '-'} / {bs[0]['kind'] if bs else '-'}")
                time.sleep(0.3)

    print("\n" + ("全部通过" if not problems else f"有 {len(problems)} 项没通过："))
    for p in problems:
        print("  - " + p)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
