"""把 sources/ 下每个站的书源合并成根目录的 all.json（网络导入只用这一个地址）。

用法：
  python tools/build.py          重新生成 all.json
  python tools/build.py --check  只检查 all.json 是不是最新的，不是就返回 1
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "all.json"


def build():
    merged, seen = [], {}
    for f in sorted((ROOT / "sources").glob("*.json")):
        for src in json.loads(f.read_text(encoding="utf-8")):
            url = src["bookSourceUrl"]
            if url in seen:
                sys.exit(f"书源地址重复：{url}（{seen[url]} 和 {f.name}）")
            seen[url] = f.name
            merged.append(src)
    merged.sort(key=lambda s: (s.get("customOrder", 0), s["bookSourceUrl"]))
    return json.dumps(merged, ensure_ascii=False, indent=2) + "\n"


def main():
    text = build()
    if "--check" in sys.argv:
        old = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if old != text:
            sys.exit("all.json 不是最新的，先跑 python tools/build.py")
        print("all.json 是最新的")
        return
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"已生成 all.json，共 {len(json.loads(text))} 个书源")


if __name__ == "__main__":
    main()
