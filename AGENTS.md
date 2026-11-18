# AGENTS.md

给在本仓库工作的 AI 编码代理的说明。人读的文档在 `README.md`，这里只写「怎么改、别踩什么坑」。

## 项目是什么

单文件脚本 `eudic_server.py`（约 640 行，只用标准库），把 macOS 版欧路词典包装成
**Google Cloud Translation API v2 兼容**的本地 HTTP 服务：

- 单词 → 读欧路词典的 WebKit 缓存 `~/Library/Caches/com.eusoft.eudic/Cache.db`
- 句子 → 读欧路词典的翻译 KV `~/Library/Eudb_en/kvCache/TranslationResult`
- 都没有 → AppleScript 唤起 App 查词，再轮询缓存等结果

没有构建步骤、没有依赖、没有自动化测试。`README.md` 是接口文档，改接口时同步更新它。

## 常用命令

```bash
# 语法检查（本项目唯一的“测试”，改完必跑）
python3 -m py_compile eudic_server.py

# 起服务
python3 eudic_server.py --port 8787

# 冒烟测试（用 trigger=0 避免抢焦点/唤起 App）
curl -s localhost:8787/health
curl -s 'localhost:8787/language/translate/v2?q=hola&target=zh-CN&trigger=0'
curl -s 'localhost:8787/language/translate/v2/detect?q=%E4%BD%A0%E5%A5%BD'
curl -s 'localhost:8787/language/translate/v2/languages?target=en'
curl -s -X POST localhost:8787/language/translate/v2 \
  -H 'Content-Type: application/json' \
  -d '{"q":["hello"],"target":"zh-CN","trigger":0}'

# 调试缓存解析（可以直接 import 脚本里的函数）
python3 -c "import importlib.util,json; s=importlib.util.spec_from_file_location('e','eudic_server.py'); \
m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(len(m.kv_records()))"

# 看欧路词典最近写了哪些缓存行
sqlite3 "file:$HOME/Library/Caches/com.eusoft.eudic/Cache.db?mode=ro" \
  "select entry_ID, request_key from cfurl_cache_response order by entry_ID desc limit 10"
```

注意：不带 `trigger=0` 的请求会**把欧路词典切到前台**（AppleScript `show dic`），
自动化测试时默认加上 `trigger=0`。

## 文件结构（按段定位）

| 行号区段 | 内容 |
|---|---|
| 1–49 | docstring（接口、语言方向说明）、常量路径、`_LOCK` |
| 50–87 | 缓存访问：`connect` / `max_entry_id` / `rows_since` / `body_of` |
| 88–110 | 文本工具：`to_text` / `normalize` / `query_of` |
| 111–125 | `trigger_lookup`（AppleScript `show dic`） |
| 126–254 | 翻译 KV 解析：`kv_records` / `_decode_archiver` / `kv_match` |
| 255–382 | `cached` / `is_definition` / `pack` / `lookup`（缓存→触发→轮询、失败重试） |
| 383–538 | Google v2 适配层：`dest_lang` / `dict_langs` / `supported_targets` / `lang_*` / `detect_language` / `translate_one` |
| 539–708 | HTTP：`Handler._params` / `_translate` / `_direction_hint` / `_detect` / `_languages` / `_route` |

## 硬约束（改之前先读）

1. **`TranslationResult` 是预分配 + mmap 的追加式文件**：追加记录时 `size` 和 `mtime` 都不变，
   所以不能用 `stat` 判断「有没有新数据」，只能每次重读内容。记录边界靠 plist trailer
   （`offsetTableOffset + 32 + numObjects * offsetIntSize`）精确计算，不要改成猜魔数/找分隔符。
2. **WebKit 大响应体不在 SQLite 里**：`receiver_data` 只是 36 位 UUID，实体在
   `~/Library/Caches/com.eusoft.eudic/fsCachedData/<UUID>`。任何读 body 的代码都要走 `body_of`。
3. **App 不支持并发查询**：所有唤起查询的路径必须拿 `_LOCK` 串行化，锁内要重新查一次缓存
   （等锁期间别人可能刚查过）。
4. **`Cache.db` 只读打开**：`connect()` 用 `mode=ro`，且允许 `sqlite3.OperationalError` 重试
   （App 正在写 WAL 时会短暂锁库）。
5. **对外格式只能是 Google v2**：成功 `{"data":{"translations":[{"translatedText","detectedSourceLanguage"}]}}`，
   失败 `{"error":{"code","message","errors":[{"message","domain","reason"}]}}`。
   要加自己的字段，只能放在额外参数（如 `timeout` / `trigger`）或 `/lookup` 这个调试入口里。
6. **`translatedText` 必须 HTML 转义**（`escape_translation`），这是 Google 的行为，客户端会反转义。
7. **目标语言是动态集合，不是单一值**：`supported_targets()` = `dest_lang()` ∪ `dict_langs()`（`~/Library/Eudb_*`）
   ∪ KV 里出现过的 `destLang`（有 30s 缓存）。不要改回「只允许 `TRANS_DestLangItem`」——
   App 是双向的，中→英译文会以 `destLang="en"` 落进 KV，早期版本就是因此把 `source=zh&target=en` 误判成 400。
8. **实时翻译只能翻成 App 当前译文语言**：`show dic` 无法在没有重启 App 的情况下切译向
   （`defaults write ... TRANS_DestLangItem en` 实测对运行中的 App 无效），所以反方向只能靠缓存 + 词典兜底，
   失败时用 `_direction_hint()` 把原因和做法写进错误消息，不要默默返回原文。
9. **与原文完全相同的译文要过滤**（`translate_one.usable`）：App 流式中间态/失败时会回写原文。

## 已知未解决

- 反向（中→英）**实时**翻译做不到：切译向需要用户在 App UI 里改并重启 App（实测 `defaults write`
  对运行中的进程无效）。若以后要支持，思路是改 App 自己的翻译引擎调用（`engineId` 字段里出现过 `edge`），
  但那是另一个项目，先和用户确认。
- 不要用 `/tmp` 里编译 ObjC + `NSPerformService("srvTranslate")`、System Events 点击之类的方式绕过：
  需要 TCC/辅助功能授权，且不会写进翻译 KV，已试过并放弃。

## 代码风格

- 标准库优先，不引第三方依赖。
- 类型标注保持现状风格（`str | None`，文件顶部有 `from __future__ import annotations`）。
- 注释和错误信息用中文，接口/字段名用英文。
- 段落之间的 `# ---- 标题` 分隔线保留，新功能按现有段落追加，不要把单文件拆包。
- 用户可见的错误信息要能直接指路（哪个缓存文件、哪里没命中）。
