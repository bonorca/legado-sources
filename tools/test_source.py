"""在电脑上模拟阅读 APP 跑一遍书源：搜索 → 详情 → 目录 → 正文 → 发现。

只实现了本仓库书源用到的规则写法：
  @css:选择器@属性   属性可以是 text / html / ownText / 任意 HTML 属性
  text / href / src  直接取当前元素
  ##正则##替换       对结果做替换
  @js:java.t2s(result)  繁体转简体（用 opencc 模拟）；也认 (typeof java.t2s=="function"?java.t2s(result):result)
  @js:'前缀'+result     在结果前面拼一段（比如换域名）
  地址,{"method":"POST","body":"..."}   POST 请求
  {{表达式}}           地址里的 JS，交给 node 计算
  nextContentUrl / replaceRegex   正文分页和正文替换
  nextTocUrl         目录分页：取到一个地址就一页页往下翻，取到多个就全部打开
  被挡的页面          403/429/503、Cloudflare 验证页等，先请 r.jina.ai 代取一次（只限 GET），取到就照常解析
  loginCheckJs       只认两种写法：响应里有某段字（拦截页）就用 java.connect 带着 Cookie 再请求一次，
                     或者用 java.startBrowserAwait 弹出浏览器让人验证（检测时记为受限）
选择器里的 :eq(n) 按 Jsoup 的意思处理（从 0 开始），:contains() 也能用。

结果分三种：✅ 通过；❌ 规则有问题（页面正常打开了，但规则取不到东西）；
⚠️ 受限（网站把请求挡了：403/429/503、Cloudflare 验证、「搜索间隔」之类的提示页），受限不算失败。
搜索失败或受限时，如果 tools/keywords.json 里给了固定的书页地址，就用它接着测详情、目录、正文。

用法：
  python tools/test_source.py all.json                 每个书源用 tools/keywords.json 里的书名测
  python tools/test_source.py sources/xx.json 剑来      指定书名
  python tools/test_source.py all.json --summary 报告.md  另外写一份 Markdown 汇总
"""
import argparse
import json
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urljoin, quote

import unicodedata

import requests
from bs4 import BeautifulSoup, Comment
from opencc import OpenCC

T2S = OpenCC("t2s")
sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
STAGES = ["搜索", "详情", "目录", "正文", "发现"]


# ---------- 规则解析 ----------

def select(el, sel):
    """支持 Jsoup 的 :eq(n) 和 :contains()，其余交给 soupsieve。"""
    sel = sel.replace(":contains(", ":-soup-contains(")
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
        # 和 Jsoup 的 text() 一样：行内标签（<em> 等）之间不加空格，空白合并成一个
        return re.sub(r"\s+", " ", el.get_text()).strip()
    if attr == "ownText":
        return "".join(t for t in el.find_all(string=True, recursive=False)).strip()
    if attr == "textNodes":
        # 和阅读一样：只取元素自己的文字节点（不含子标签里的字），每段去掉首尾空白后用换行连起来
        parts = (t.strip() for t in el.find_all(string=True, recursive=False) if not isinstance(t, Comment))
        return "\n".join(p for p in parts if p)
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
        rep = re.sub(r"\$(\d)", r"\\\1", rep)  # 阅读用 Java 的 $1 引用分组，Python 写成 \1
        value = re.sub(pat, rep, value)
    if js:
        # 有的 APP（iOS 上的）没有 java.t2s，书源里写成「有就转、没有就原样」；测试按有 java.t2s 的阅读 APP 来
        js = js.strip().replace('(typeof java.t2s=="function"?java.t2s(result):result)', "java.t2s(result)")
        wrap = re.fullmatch(r"(?:'([^']*)'\s*\+\s*)?result(?:\s*\+\s*'([^']*)')?", js)
        chain = re.fullmatch(r"(java\.t2s\(result\)|result)((?:\.replace\(/(?:[^/\\]|\\.)+/g,\s*\"[^\"]*\"\))*)", js)
        if chain:
            if chain.group(1) != "result":
                value = T2S.convert(value)
            for pat, rep in re.findall(r"\.replace\(/((?:[^/\\]|\\.)+)/g,\s*\"([^\"]*)\"\)", chain.group(2)):
                value = re.sub(pat, rep, value)
        elif wrap:
            value = (wrap.group(1) or "") + value + (wrap.group(2) or "")
        else:
            raise NotImplementedError("测试脚本不支持这段 JS: " + js)
    return value.strip()


def select_default(el, rule):
    """阅读的默认写法（JSoup Default）里最常用的一种：class.名.序号 / id.名 / tag.名 / text.文字，用 @ 串起来。"""
    hits = [el]
    for seg in rule.split("@"):
        kind, _, rest = seg.partition(".")
        name, _, idx = rest.partition(".")
        nxt = []
        for h in hits:
            if kind == "class":
                nxt += h.select("." + name)
            elif kind == "id":
                nxt += h.select("#" + name)
            elif kind == "tag":
                nxt += h.find_all(name)
            elif kind == "text":  # 自己的文字里含有这段字的元素
                nxt += [e for e in h.find_all(True)
                        if name in "".join(t for t in e.find_all(string=True, recursive=False))]
            else:
                raise NotImplementedError("测试脚本不支持这种默认写法: " + seg)
        if idx:
            i = int(idx)
            nxt = [nxt[i]] if -len(nxt) <= i < len(nxt) else []
        hits = nxt
    return hits


DEFAULT_RULE = re.compile(r"^(class|id|tag|text)\.")


def get_string(el, rule):
    if not rule:
        return ""
    rule, regex, js = split_rule(rule)
    if DEFAULT_RULE.match(rule) and "@" in rule:
        sel, _, attr = rule.rpartition("@")
        value = "\n".join(v for v in (get_attr(h, attr) for h in select_default(el, sel)) if v)
    elif rule.startswith("@css:"):
        sel, _, attr = rule[5:].rpartition("@")
        hits = select(el, sel) if sel else [el]
        value = "\n".join(v for v in (get_attr(h, attr) for h in hits) if v)
    else:
        value = get_attr(el, rule)
    return apply_post(value, regex, js)


def get_elements(el, rule):
    assert rule.startswith("@css:"), rule
    return select(el, rule[5:])


def fill_url(template, key="", page=1):
    """替换 {{key}}、{{page}}；其余 {{表达式}} 交给 node 当 JS 算（和 APP 一样）。"""
    m = re.search(r'"charset"\s*:\s*"([^"]+)"', template)
    cs = (m.group(1) if m else "utf-8").lower()
    enc_key = quote(key.encode("gb18030" if cs in ("gbk", "gb2312") else cs))
    url = template.replace("{{key}}", enc_key).replace("{{page}}", str(page))

    def run_js(m):
        code = f"const page={page}, key={json.dumps(key)}; process.stdout.write(String({m.group(1)}))"
        return subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout

    return re.sub(r"\{\{(.+?)\}\}", run_js, url)


def split_url(url):
    """阅读的地址可以写成 地址,{选项}；拆成地址和选项。"""
    m = re.match(r"^(.*?),\s*(\{[\s\S]*\})\s*$", url)
    if not m:
        return url, {}
    return m.group(1), json.loads(m.group(2))


# ---------- 请求 ----------

class Blocked(Exception):
    """网站把请求挡了（不是规则的问题）。"""


BLOCK_TEXT = re.compile(r"Just a moment|cf-chl|cf_chl_opt|challenge-platform/h/|Verify you are human|"
                        r"搜索间隔|搜索过于频繁|操作过于频繁|访问过于频繁|请求过于频繁|人机验证|安全验证")


def blocked_reason(r):
    if r.status_code in (403, 429, 503):
        return f"网站拒绝访问（HTTP {r.status_code}）"
    if len(r.content) < 50000:  # 拦截页都很小；正常网页里偶尔出现这些字不算
        raw = r.content.decode("utf-8", "ignore") + r.content.decode("gb18030", "ignore")
        m = BLOCK_TEXT.search(raw)
        if m:
            return f"网站返回了拦截页（「{m.group(0)}」）"
    return None


class Runner:
    def __init__(self, src):
        self.src = src
        self.base = src["bookSourceUrl"]
        self.s = requests.Session()
        self.s.headers.update(json.loads(src.get("header") or "{}"))
        self.jina_urls = []

    def via_jina(self, url):
        """直接访问被挡时，请 r.jina.ai 用它的浏览器打开页面，取回渲染后的 HTML；它也被挡就返回 None。"""
        try:
            time.sleep(3)  # 不带 key 的 jina 每分钟只能调用十几次
            r = requests.get("https://r.jina.ai/" + url, headers={"X-Return-Format": "html", "X-No-Cache": "true"},
                             timeout=90)
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        r.encoding = "utf-8"
        if BLOCK_TEXT.search(r.text[:50000]) and len(r.content) < 50000:
            return None
        return r.text

    def fetch(self, url):
        url, opt = split_url(url)
        url = urljoin(self.base + "/", url.strip())
        method = (opt.get("method") or "GET").upper()
        body = opt.get("body")
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        for n, wait in enumerate([0, 5, 15, 30]):
            time.sleep(wait or 0.6)
            try:
                r = self.s.request(method, url, data=body, headers=headers, timeout=45)
            except requests.exceptions.SSLError:
                if not self.s.verify:
                    raise
                # 阅读 APP 不校验 HTTPS 证书；有的站证书链不完整（缺中间证书），电脑上会报错，APP 里照常能用
                print("     （证书校验不过，和 APP 一样跳过校验后重试）")
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                self.s.verify = False
                r = self.s.request(method, url, data=body, headers=headers, timeout=45)
            except (requests.ConnectionError, requests.Timeout) as e:
                if n == 3:
                    raise
                print(f"     （连接失败 {type(e).__name__}，稍后重试）")
                continue
            if r.status_code != 429:
                break
            print(f"     （429 限流，{wait or 5} 秒后重试）")
        chk = self.src.get("loginCheckJs")
        if chk:
            m = re.search(r'indexOf\("([^"]+)"\)\s*>\s*-1', chk)
            if not (m and ("java.connect(" in chk or "java.startBrowserAwait(" in chk)):
                raise NotImplementedError("测试脚本不支持这段 loginCheckJs: " + chk)
        why = None
        if chk:
            m = re.search(r'indexOf\("([^"]+)"\)\s*>\s*-1', chk)
            if not (m and ("java.connect(" in chk or "java.startBrowserAwait(" in chk)):
                raise NotImplementedError("测试脚本不支持这段 loginCheckJs: " + chk)
            if m.group(1) in r.text and "java.startBrowserAwait(" in chk:
                # APP 里会弹出浏览器让人点验证；检测时没人点
                why = f"要在浏览器里人工验证（「{m.group(1)}」）"
            elif m.group(1) in r.text:
                print("     （碰到拦截页，和 loginCheckJs 一样带着 Cookie 再请求一次）")
                time.sleep(0.6)
                r = self.s.request(method, url, data=body, headers=headers, timeout=45)
        why = why or blocked_reason(r)
        if why:
            jr = self.via_jina(url) if method == "GET" else None
            if jr is None:
                raise Blocked(why)
            print(f"     （{why}，改由 r.jina.ai 代取，接着用同样的规则解析）")
            self.jina_urls.append(url)
            return url, BeautifulSoup(jr, "lxml")
        r.raise_for_status()
        cs = opt.get("charset")
        if not cs:  # 和 APP 一样：没写 charset 就看响应头和网页里的 <meta charset>
            m = re.search(r"charset=([\w-]+)", r.headers.get("content-type", "")) or \
                re.search(rb'charset=["\']?([\w-]+)', r.content[:3000])
            cs = (m.group(1) if isinstance(m.group(1), str) else m.group(1).decode()) if m else "utf-8"
        r.encoding = {"gbk": "gb18030", "gb2312": "gb18030", "big5": "big5hkscs"}.get(cs.lower(), cs)
        return r.url, BeautifulSoup(r.text, "lxml")

    def book_list(self, url, rules):
        page_url, soup = self.fetch(url)
        self.last_url, self.last_soup = page_url, soup
        books = []
        for it in get_elements(soup, rules["bookList"]):
            b = {k: get_string(it, rules.get(k)) for k in
                 ["name", "author", "kind", "wordCount", "intro", "lastChapter", "coverUrl", "bookUrl"]}
            b["bookUrl"] = urljoin(page_url, b["bookUrl"])
            if b["coverUrl"]:
                b["coverUrl"] = urljoin(page_url, b["coverUrl"])
            if b["name"]:  # APP 会丢掉没有书名的条目
                books.append(b)
        return books

    def content(self, url, next_chapter_url):
        """取一章正文；有 nextContentUrl 时一页页接上，遇到下一章地址就停（和 APP 一样）。"""
        rc = self.src["ruleContent"]
        parts, seen = [], {url}
        while True:
            page_url, soup = self.fetch(url)
            parts.append(get_string(soup, rc["content"]))
            nxt = get_string(soup, rc.get("nextContentUrl"))
            nxt = urljoin(page_url, nxt) if nxt else ""
            if not nxt or nxt in seen or nxt == next_chapter_url or len(parts) >= 20:
                break
            seen.add(nxt)
            url = nxt
        text = "\n".join(BeautifulSoup(p, "lxml").get_text("\n", strip=True) for p in parts)
        if rc.get("replaceRegex"):
            _, regex, _ = split_rule(rc["replaceRegex"])
            text = apply_post(text, regex, None)
        return text, len(parts)


# ---------- 检查 ----------

class Report:
    def __init__(self, name, url=""):
        self.name = name
        self.url = url
        self.failed = {s: [] for s in STAGES}
        self.blocked = {s: [] for s in STAGES}
        self.skipped = set()
        self.book_url = ""
        self.jina = 0  # 有几个页面是被挡后由 r.jina.ai 代取的

    def record(self, stage, e):
        """某一步抛了异常：被网站挡了记「受限」，其余记「失败」。"""
        if isinstance(e, Blocked):
            print(f"  ⚠ 受限：{e}")
            self.blocked[stage].append(str(e))
        else:
            msg = f"出错：{type(e).__name__}: {e}"[:200]
            print("  ✗ " + msg)
            traceback.print_exc(limit=1)
            self.failed[stage].append(msg)

    def check(self, stage, cond, msg):
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            self.failed[stage].append(msg)

    @property
    def ok(self):
        return not any(self.failed.values())

    @property
    def limited(self):
        return any(self.blocked.values())


JUNK = ["笔趣阁", "biquge", "52书库", "福门", "书吧", "返回顶部", "请收藏", "请大家收藏", "全网最快", "最新章节", "http", "www."]


class StopStage(Exception):
    """这一步已经记了失败，不用再往下做。"""


def test_source(src, key, book=None):
    """book：固定的书页地址，搜索失败或受限时用它接着测。"""
    rep = Report(src["bookSourceName"], src["bookSourceUrl"])
    r = Runner(src)
    print(f"\n===== {src['bookSourceName']}  ({src['bookSourceUrl']})  关键词：{key}")

    hit = None
    stage = "搜索"
    try:
        print(f"\n[搜索] {key}")
        books = r.book_list(fill_url(src["searchUrl"], key, 1), src["ruleSearch"])
        pat = src.get("bookUrlPattern")
        if not books and pat and re.fullmatch(pat, r.last_url):
            # 只有一个结果时站点直接跳到书页；APP 按 bookUrlPattern 认出来，用详情规则取书名
            ri = src["ruleBookInfo"]
            books = [{"name": get_string(r.last_soup, ri.get("name")), "author": get_string(r.last_soup, ri.get("author")),
                      "kind": get_string(r.last_soup, ri.get("kind")), "lastChapter": get_string(r.last_soup, ri.get("lastChapter")),
                      "bookUrl": r.last_url}]
            print(f"     （搜索直接跳到了书页 {r.last_url}）")
        rep.check(stage, len(books) > 0, f"搜到 {len(books)} 本")
        if not books:
            raise StopStage
        for b in books[:3]:
            print(f"     {b['name']} / {b['author']} / {b['kind']} / 最新：{b['lastChapter']}")
        rep.check(stage, all(b["name"] and b["bookUrl"] for b in books), "每本都有书名和链接")
        hit = next((b for b in books if b["name"] == key), books[0])
        rep.check(stage, T2S.convert(hit["name"]) == hit["name"], f"书名已是简体：{hit['name']}")
        if "{{page}}" in src["searchUrl"] and len(books) >= 10:
            books2 = r.book_list(fill_url(src["searchUrl"], key, 2), src["ruleSearch"])
            rep.check(stage, books2 and books2[0]["bookUrl"] != books[0]["bookUrl"], f"第 2 页能翻（{len(books2)} 本）")

        rep.book_url = hit["bookUrl"]
    except StopStage:
        hit = None
    except Exception as e:
        rep.record(stage, e)
        hit = None

    if hit is None and book:
        print(f"\n     （搜索没拿到书，改用固定的书接着测：{book}）")
        hit = {"bookUrl": book, "name": "", "author": ""}
    if hit is None:
        rep.skipped |= {"详情", "目录", "正文"}
    else:
        try:
            stage = "详情"
            print(f"\n[详情] {hit['bookUrl']}")
            info_url, soup = r.fetch(hit["bookUrl"])
            ri = src["ruleBookInfo"]
            info = {k: get_string(soup, ri.get(k)) for k in
                    ["name", "author", "kind", "wordCount", "lastChapter", "intro", "coverUrl", "tocUrl"]}
            for k, v in info.items():
                print(f"     {k}: {v[:60]}")
            if hit["name"]:
                rep.check(stage, info["name"] == hit["name"], "详情页书名和搜索结果一致")
            else:
                rep.check(stage, bool(info["name"]), f"详情页有书名：{info['name']}")
            if not ri.get("author"):
                print("     （详情页不取作者，沿用搜索结果里的）")
            elif hit["author"]:
                rep.check(stage, info["author"] == hit["author"].rstrip("."), "详情页作者和搜索结果一致")
            else:
                rep.check(stage, bool(info["author"]), f"详情页有作者（搜索结果里没有作者）：{info['author']}")
            rep.check(stage, bool(info["intro"]), "有简介")
            if ri.get("tocUrl"):
                rep.check(stage, bool(info["tocUrl"]), "有目录地址")

            stage = "目录"
            print("\n[目录]")
            if info["tocUrl"]:
                toc_url, soup = r.fetch(urljoin(info_url, info["tocUrl"]))
            else:
                toc_url = info_url  # 没写目录地址时，APP 直接在详情页上找目录
            rt = src["ruleToc"]
            pages = [(toc_url, soup)]
            nxt = [urljoin(toc_url, u) for u in get_string(soup, rt.get("nextTocUrl")).split(chr(10)) if u.strip()]
            nxt = [u for u in dict.fromkeys(nxt) if u != toc_url]
            if len(nxt) > 1:  # 和 APP 一样：一次给出多个地址就全部打开
                pages += [r.fetch(u) for u in nxt]
            while len(nxt) == 1 and nxt[0] not in {u for u, _ in pages} and len(pages) < 200:
                pages.append(r.fetch(nxt[0]))
                nxt = [urljoin(pages[-1][0], u) for u in get_string(pages[-1][1], rt.get("nextTocUrl")).split(chr(10)) if u.strip()][:1]
            if len(pages) > 1:
                print(f"     目录共 {len(pages)} 页")
            chs = [(get_string(a, rt["chapterName"]), urljoin(u, get_string(a, rt["chapterUrl"])))
                   for u, sp in pages for a in get_elements(sp, rt["chapterList"])]
            rep.check(stage, len(chs) > 0, f"共 {len(chs)} 章")
            print(f"     第一章：{chs[0][0]}  {chs[0][1]}")
            print(f"     最后一章：{chs[-1][0]}  {chs[-1][1]}")
            rep.check(stage, len({u for _, u in chs}) == len(chs), "章节地址没有重复")
            rep.check(stage, all(n for n, _ in chs), "每章都有标题")

            stage = "正文"
            print("\n[正文]")
            for i in sorted({0, len(chs) // 2, len(chs) - 1}):
                name, u = chs[i]
                nxt = chs[i + 1][1] if i + 1 < len(chs) else None
                text, pages = r.content(u, nxt)
                junk = [w for w in JUNK if w in text]
                # 站点自己的域名：先把花体字母归一、去掉 emoji 和符号，再找（例：𝘀𝘁𝗼𝟵.𝗰𝗼𝗺、sto🎉9.com）
                flat = re.sub(r"[^a-z0-9\n]", "", unicodedata.normalize("NFKC", text).lower())
                label = re.sub(r"^(www|m|wap)\.", "", src["bookSourceUrl"].split("//")[-1].split("/")[0]).split(".")[0]
                hits = [ln for ln in flat.split("\n") if label + "com" in ln or label + "net" in ln or label + "cc" in ln]
                if hits:
                    junk.append(f"{label} 域名广告 {len(hits)} 处")
                # 繁转简漏掉的「著」（看著、跟著）
                zhe = re.findall(r"(?<![显土卓巨名原编论译遗专新拙合昭较])著(?![作名称述者录])", text)
                if zhe:
                    junk.append(f"「著」没改成「着」{len(zhe)} 处")
                rep.check(stage, len(text) > 0, f"{name}：{len(text)} 字" + (f"（{pages} 页拼起来）" if pages > 1 else "")
                          + f"，开头：{text[:30]}" + ("（偏短，可能是作者公告，自己看一眼）" if 0 < len(text) < 500 else ""))
                rep.check(stage, not junk, f"{name}：正文里没有广告字样" + (f"（发现 {junk}）" if junk else ""))
                changed = sum(a != b for a, b in zip(text, T2S.convert(text)))
                rep.check(stage, changed <= len(text) * 0.002,
                          f"{name}：正文是简体" + (f"（有 {changed} 个字会被繁转简改动，多半是人名地名）" if changed else ""))

        except Exception as e:
            rep.record(stage, e)
            for s in STAGES[STAGES.index(stage) + 1:STAGES.index("正文") + 1]:
                rep.skipped.add(s)

    try:
        stage = "发现"
        if src.get("exploreUrl"):
            print("\n[发现]")
            for line in src["exploreUrl"].splitlines():
                title, _, u = line.partition("::")
                bs = r.book_list(fill_url(u, page=1), src["ruleExplore"])
                ok = len(bs) > 0 and all(b["name"] and b["bookUrl"] for b in bs)
                try:
                    bs2 = r.book_list(fill_url(u, page=2), src["ruleExplore"])
                except requests.HTTPError as e:
                    if e.response is not None and e.response.status_code == 404 and ok:
                        rep.check(stage, True, f"{title}：第1页 {len(bs)} 本，只有 1 页（第2页 404，APP 会停在这里）")
                        continue
                    raise
                if ok and bs2 and [b["bookUrl"] for b in bs2] == [b["bookUrl"] for b in bs]:
                    rep.check(stage, True, f"{title}：第1页 {len(bs)} 本，只有 1 页（站点把第2页显示成第1页）")
                    continue
                # 第 2 页有第 1 页没有的书就算能翻。网站按更新时间排序时，两页可能隔着缓存错开几本，首本相同不算错
                rep.check(stage, ok and bs2 and bool({b["bookUrl"] for b in bs2} - {b["bookUrl"] for b in bs}),
                          f"{title}：第1页 {len(bs)} 本，第2页 {len(bs2)} 本，例：{bs[0]['name'] if bs else '-'} / {bs[0]['author'] if bs else '-'} / {bs[0]['kind'] if bs else '-'}")
        else:
            rep.skipped.add(stage)
    except Exception as e:
        rep.record(stage, e)
    rep.jina = len(r.jina_urls)
    return rep

def cell(failed, blocked, skipped, s):
    if failed.get(s):
        return "❌"
    if blocked.get(s):
        return "⚠️"
    return "—" if s in skipped else "✅"


def write_summary(path, reports):
    lines = ["| 书源 | " + " | ".join(STAGES) + " |", "|---" * (len(STAGES) + 1) + "|"]
    for rp in reports:
        cells = [cell(rp.failed, rp.blocked, rp.skipped, s) for s in STAGES]
        lines.append(f"| {rp.name} | " + " | ".join(cells) + " |")
    notes = [f"- ❌ {rp.name} / {s}：{m}" for rp in reports for s in STAGES for m in rp.failed[s]]
    notes += [f"- ⚠️ {rp.name} / {s}：{m}" for rp in reports for s in STAGES for m in rp.blocked[s]]
    if notes:
        lines += ["", "**问题**", ""] + notes
    via = [f"- {rp.name}：{rp.jina} 个页面" for rp in reports if rp.jina]
    if via:
        lines += ["", "**直接访问被挡、改由 r.jina.ai 代取的**", ""] + via
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def keyword_entry(keywords, url):
    """tools/keywords.json 里一项可以只写书名，也可以写 {"key": 书名, "book": 固定的书页地址}。"""
    v = keywords.get(url)
    if isinstance(v, dict):
        return v.get("key"), v.get("book")
    return v, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("keyword", nargs="?")
    ap.add_argument("--summary")
    ap.add_argument("--json", help="把每个书源的结果写成 JSON，给 tools/maintain.py 用")
    a = ap.parse_args()
    sources = json.load(open(a.file, encoding="utf-8"))
    keywords = json.loads((ROOT / "tools" / "keywords.json").read_text(encoding="utf-8"))
    reports = []
    for src in sources:
        key, book = keyword_entry(keywords, src["bookSourceUrl"])
        key = a.keyword or key
        if not key:
            sys.exit(f"tools/keywords.json 里没有 {src['bookSourceUrl']} 的测试书名")
        reports.append(test_source(src, key, book))
    print("\n========== 汇总")
    for rp in reports:
        bad = [m for s in STAGES for m in rp.failed[s]]
        lim = [m for s in STAGES for m in rp.blocked[s]]
        mark = "❌ " if bad else ("⚠️ " if lim else "✅ ")
        print(mark + rp.name + (f"（{len(bad)} 项没通过）" if bad else ("（受限）" if lim else ""))
              + (f"（{rp.jina} 个页面经 jina 代取）" if rp.jina else ""))
        for m in bad + lim:
            print("   - " + m)
    if a.summary:
        write_summary(a.summary, reports)
    if a.json:
        Path(a.json).write_text(json.dumps(
            [{"url": rp.url, "name": rp.name, "ok": rp.ok,
              "failed": {s: m for s, m in rp.failed.items() if m},
              "blocked": {s: m for s, m in rp.blocked.items() if m},
              "skipped": sorted(rp.skipped - {s for s, m in rp.failed.items() if m}
                                - {s for s, m in rp.blocked.items() if m}, key=STAGES.index),
              "book_url": rp.book_url, "jina": rp.jina}
             for rp in reports],
            ensure_ascii=False, indent=1), encoding="utf-8")
    sys.exit(0 if all(rp.ok for rp in reports) else 1)


if __name__ == "__main__":
    main()
