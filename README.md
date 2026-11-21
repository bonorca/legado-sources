# 阅读书源

阅读（Legado）书源，全部转成简体。

## 导入

阅读 APP → 我的 → 书源管理 → ⋮ → 网络导入，粘贴地址：

| 站点 | 地址 |
|---|---|
| 笔趣阁 biquge.tw | `https://raw.githubusercontent.com/bonorca/legado-sources/main/sources/biquge-tw.json` |
| 微风小说 wfxs.tw | `https://raw.githubusercontent.com/bonorca/legado-sources/main/sources/wfxs-tw.json` |

打不开时把 `raw.githubusercontent.com/bonorca/legado-sources/main` 换成 `cdn.jsdelivr.net/gh/bonorca/legado-sources@main`。

两个站是同一个后台，书和章节一样，可以互相换源。

## 测试

```bash
pip install -r tools/requirements.txt
python tools/test_source.py sources/biquge-tw.json 剑来
```

## 注意

- 两个站请求太快都会被限流，书源限速每秒 2 次。
- biquge.tw 正文目前没有分页；以后章节标题出现「（1 / 2）」要补 `nextContentUrl`。
- wfxs.tw 的目录从电脑版 www.wfxs.tw 取，手机版每页只有 30 章。
