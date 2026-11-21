# 阅读书源

阅读（Legado）书源，全部转成简体。

## 导入

阅读 APP → 我的 → 书源管理 → ⋮ → 网络导入，粘贴：

```
https://raw.githubusercontent.com/bonorca/legado-sources/main/all.json
```

打不开时用：

```
https://cdn.jsdelivr.net/gh/bonorca/legado-sources@main/all.json
```

以后新增的书源都会合进这个文件，重新导入一次就能更新。

| 书源 | 站点 |
|---|---|
| 笔趣阁 biquge.tw | https://www.biquge.tw |
| 微风小说 wfxs.tw | https://m.wfxs.tw |

两个站是同一个后台，书和章节一样，可以互相换源。

## 维护

每个站的书源单独放在 `sources/`，改完后合并、测试：

```bash
pip install -r tools/requirements.txt
python tools/build.py
python tools/test_source.py all.json 剑来
```

## 注意

- 两个站请求太快都会被限流，书源限速每秒 2 次。
- biquge.tw 正文目前没有分页；以后章节标题出现「（1 / 2）」要补 `nextContentUrl`。
- wfxs.tw 的目录从电脑版 www.wfxs.tw 取，手机版每页只有 30 章。
