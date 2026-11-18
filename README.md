# eudic-server

把 macOS 版的 **欧路词典 (Eudic)** 包装成一个 **Google Cloud Translation API v2 兼容**的本地 HTTP 服务。

- 单词/短语：读欧路词典的 WebKit 缓存（`Cache.db`），不关心是否联网
- 句子：读欧路词典的翻译缓存（`~/Library/Eudb_en/kvCache/TranslationResult`）
- 两者都没有时，用 AppleScript 让欧路词典去查一次，然后轮询缓存等结果
- 对外只暴露 Google Translation v2 的请求/响应格式，任何「自定义 Google 翻译 API 地址」的客户端都能直接接

## 依赖

- macOS + 欧路词典（`/Applications/Eudic.app`，已登录/已装词典）
- Python 3.9+，只用标准库，无需 pip 安装
- 首次让脚本唤起欧路词典查词时，系统可能会问「终端/脚本要控制 Eudic」的权限，允许即可

## 启动

```bash
python3 eudic_server.py --port 8787
```

默认只监听 `127.0.0.1`。建议放到后台常驻：

```bash
nohup python3 eudic_server.py --port 8787 >/tmp/eudic-server.log 2>&1 &
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / POST | `/language/translate/v2` | 翻译（Google v2 格式） |
| GET / POST | `/language/translate/v2/detect` | 语言检测 |
| GET / POST | `/language/translate/v2/languages` | 可用的目标语言 |
| GET | `/health` | 健康检查 |
| GET | `/lookup?word=…` | 调试用，返回原始缓存条目（非 Google 格式） |

### 翻译

```bash
# GET
curl 'http://127.0.0.1:8787/language/translate/v2?q=hello&target=zh-CN&source=en&format=text'

# POST JSON，q 可以是字符串或数组
curl -X POST 'http://127.0.0.1:8787/language/translate/v2' \
  -H 'Content-Type: application/json' \
  -d '{"q":["Hello world","Winter is coming"],"target":"zh-CN","format":"html"}'

# POST 表单，q 可重复
curl -X POST 'http://127.0.0.1:8787/language/translate/v2' \
  -d 'target=zh-CN&format=text' \
  --data-urlencode 'q="He was just a lost dog, like the girl said"'

# 反方向（中文 → 英文）：需要 App 曾以英文为译文语言翻过这句，或单词走中文词典释义
curl 'http://127.0.0.1:8787/language/translate/v2?q=我喜欢你&source=zh&target=en'
```

成功响应：

```json
{
  "data": {
    "translations": [
      { "translatedText": "正如那个女孩所说，他不过是一只迷路的狗罢了。",
        "detectedSourceLanguage": "en" }
    ]
  }
}
```

失败响应（Google 错误信封）：

```json
{
  "error": {
    "code": 400,
    "message": "Missing required parameter: target",
    "errors": [{ "message": "Missing required parameter: target",
                 "domain": "global", "reason": "invalidArgument" }]
  }
}
```

#### 参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `q` | 是 | 待翻译文本，可重复/传数组 |
| `target` | 是 | 目标语言。可接受欧路词典当前译文语言（`TRANS_DestLangItem`，一般 `zh-CN`）、KV 缓存里出现过的 `destLang`、以及已装词典语言（`~/Library/Eudb_<lang>`）；本机现有 `zh-CN` 和 `en`。同基名的 `zh`、`zh-Hans` 也接受；完全没沾边的语言（如 `de`）返回 400 |
| `source` | 否 | 源语言；省略或 `auto` 时由服务端按字符集猜测并回填到 `detectedSourceLanguage` |
| `format` | 否 | `text`（默认）或 `html`。单词回退到词典条目时，`html` 会返回词典 HTML |
| `key` | 否 | Google 客户端通常会带 API key，这里忽略 |
| `timeout` | 否 | 扩展参数，等待欧路词典查词的最长秒数，默认 `10` |
| `trigger` | 否 | 扩展参数，`0`/`false`/`no` 表示只查缓存、不唤起 App，默认允许唤起 |

响应里的 `translatedText` 按 Google 约定做了 HTML 转义（`&`、`<`、`>`、引号都会变成实体）。

### 语言检测

```bash
curl 'http://127.0.0.1:8787/language/translate/v2/detect?q=hola%20mundo&q=你好'
```

```json
{"data":{"detections":[
  [{"language":"en","isReliable":false,"confidence":0.85}],
  [{"language":"zh-CN","isReliable":false,"confidence":0.85}]
]}}
```

按字符集判断（日文假名 → `ja`、韩文 → `ko`、汉字 → `zh-CN`、西里尔 → `ru`、阿拉伯 → `ar`…），都不是就当英语。

### 可用语言

```bash
curl 'http://127.0.0.1:8787/language/translate/v2/languages?target=en'
```

```json
{"data":{"languages":[
  {"language":"zh-CN","name":"Chinese (Simplified)"},
  {"language":"en","name":"English"}
]}}
```

只列出本机实际能产出的语言（App 当前译向 + KV 里出现过的 `destLang` + 已装词典语言），当前译向排在最前；
不带 `target` 时不返回 `name`（与 Google 一致）。

### 调试入口

```bash
curl 'http://127.0.0.1:8787/lookup?word=serendipity' | jq '.results[].endpoint'
# "/api/v2/dicts/search"、"https://dict.youdao.com/dp/dp" …
```

返回命中行原文（`html` 与去标签的 `text`）、`translations`（KV 缓存里的句子译文）、耗时和 `cache_hit`。

## 工作原理

1. **单词**：`~/Library/Caches/com.eusoft.eudic/Cache.db` 里是 WebKit 缓存的网络响应（`api.frdic.com`、`dict.youdao.com` 等），按 `request_key` 里的 URL 编码文本匹配；大响应体只在 `fsCachedData/<UUID>` 里。
2. **句子**：`~/Library/Eudb_en/kvCache/TranslationResult` 是追加式 KV，每条记录是 `[key][bplist(NSKeyedArchiver)]`，用 plist trailer 精确算长度逐条解析；这个文件预分配 + mmap 写入，大小和 mtime 都不变，只能每次重读内容。先精确匹配 `origin`，再按 `difflib` 相似度 > 0.92 兜底。
3. **未命中**：`osascript` 调 AppleScript `show dic with word`（这个入口既能查词也能出长句译文），然后用 `entry_ID` 水位线轮询缓存；2.5 秒内一条新记录都没有就重试一次触发，仍无则失败。同一时刻只允许一个查询（App 不支持并发），用线程锁串行化。

## 已知限制

- **实时翻译只能翻成 App 当前译文语言**（`TRANS_DestLangItem`，现为 `zh-CN`）。
  `target=en` 这类反方向：缓存里已有的句子（之前把 App 译文语言改成英文翻过）能返回，
  单词会回退到中文词典的英文释义，未缓存的长句返回 500 并在 `message` 里说明原因。
  改 App 的「翻译 → 译文语言」需要重启 App 才生效（`defaults write` 改完不重启没用），生效后再翻一次才会进缓存。
- 触发查词会把欧路词典切到前台，`trigger=0` 可以避开这一点（但就只能读缓存）。
- 语言检测是字符集启发式，不区分 `zh-CN` / `zh-TW`，短文本容易猜错。
- 词典缓存被清理（或语言目录不是 `Eudb_en`）后需要重新在 App 里查一遍才有结果。
- App 有时会把原文当译文回写、或给 AI 改写版；服务端只过滤「与原文完全相同」的脏记录，其余质量取决于欧路词典自己的翻译引擎。

## License

个人自用脚本，无授权声明。
