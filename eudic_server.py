#!/usr/bin/env python3
"""把 macOS 欧路词典(Eudic) 包装成 Google Cloud Translation (v2) 格式的 HTTP API。

策略：缓存优先 → 未命中就用 AppleScript 触发查询 → 轮询缓存直到出现新响应 → 返回。

对外接口（Google Translation API v2 兼容）:
    GET  /language/translate/v2?q=hello&target=zh-CN&source=en&format=text
    POST /language/translate/v2          (JSON 或 form 表单，q 可为字符串或数组)
    GET  /language/translate/v2/detect?q=hola
    GET  /language/translate/v2/languages[?target=zh-CN]
    GET  /health
    GET  /lookup?word=...                (调试用，返回原始缓存条目)

用法:
    python3 eudic_server.py --port 8787
    curl 'http://127.0.0.1:8787/language/translate/v2?q=hello&target=zh-CN'
    curl -X POST 'http://127.0.0.1:8787/language/translate/v2' \
         -H 'Content-Type: application/json' \
         -d '{"q":["Hello world"],"target":"zh-CN","format":"text"}'

语言方向（重要）：
    能接受的目标语言 = App 设置的译文语言（TRANS_DestLangItem，现为 zh-CN）
                      ∪ KV 缓存里出现过的 destLang ∪ ~/Library/Eudb_<lang> 已装词典语言。
    但**实时翻译只能翻成 App 当前译文语言**：`show dic` 是查词/翻译入口，
    翻成其他语言只在缓存命中时才能返回（或在那个方向上先在 App 里翻一次）。
    所以 zh→en 这种反方向：缓存过的句子能用，单词会回退到中文词典的英文释义。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CACHE_DIR = os.path.expanduser("~/Library/Caches/com.eusoft.eudic")
KV_FILE = os.path.expanduser("~/Library/Eudb_en/kvCache/TranslationResult")
CACHE_DB = os.path.join(CACHE_DIR, "Cache.db")
FS_CACHE = os.path.join(CACHE_DIR, "fsCachedData")

_LOCK = threading.Lock()  # 词典 App 只能一个查询一个查询地跑


# ---------------------------------------------------------------- cache access
def connect():
    uri = "file:%s?mode=ro" % urllib.parse.quote(CACHE_DB)
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def max_entry_id() -> int:
    with connect() as conn:
        return conn.execute("select coalesce(max(entry_ID), 0) from cfurl_cache_response").fetchone()[0]


def rows_since(entry_id: int) -> list[dict]:
    sql = (
        "select r.entry_ID, r.request_key, r.time_stamp, d.receiver_data "
        "from cfurl_cache_response r "
        "join cfurl_cache_receiver_data d on d.entry_ID = r.entry_ID "
        "where r.entry_ID > ? order by r.entry_ID"
    )
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, (entry_id,))]


def body_of(row: dict) -> str:
    body = row["receiver_data"] or ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    # WebKit 大响应只留 UUID，实体在 fsCachedData/<UUID>
    if re.fullmatch(r"[0-9A-Fa-f-]{36}", body.strip()):
        path = os.path.join(FS_CACHE, body.strip())
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as fd:
                return fd.read()
        return ""
    return body


# ------------------------------------------------------------------ text utils
SKIP_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
BLOCK_RE = re.compile(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>|</table>")
TAG_RE = re.compile(r"<[^>]+>")


def to_text(fragment: str) -> str:
    s = SKIP_RE.sub(" ", fragment)
    s = BLOCK_RE.sub("\n", s)
    s = TAG_RE.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    return "\n".join(line.strip() for line in s.splitlines() if line.strip())


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def query_of(request_key: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(request_key).query))


# --------------------------------------------------------------------- trigger

def trigger_lookup(word: str) -> None:
    """用 AppleScript 让欧路词典去查这个句子（会切到前台）。"""
    script = (
        'on run argv\n'
        '  tell application "Eudic" to show dic with word (item 1 of argv)\n'
        'end run'
    )
    subprocess.run(["osascript", "-e", script, word],
                   check=True, capture_output=True, timeout=10)




# ------------------------------------------------------- translation KV store
# 句子翻译不走 WebKit 缓存，而是落在 ~/Library/Eudb_en/kvCache/TranslationResult
# 格式: 追加式 [key][bplist(NSKeyedArchiver: TranslationResultModel)]
import plistlib


def kv_size() -> int:
    try:
        return os.path.getsize(KV_FILE)
    except OSError:
        return 0


def kv_records() -> list[dict]:
    """解析追加式 KV：记录 = [key 元数据][bplist(NSKeyedArchiver)]。

    用 plist trailer 精确算出长度（offsetTableOffset + 32 + numObjects*offsetIntSize），
    不必猜边界。
    """
    # 注意：这个文件是预分配 + mmap 写入，追加记录时 size 和 mtime 都不变，
    # 所以不能靠 stat 判断“有没有新数据”，只能每次读内容。
    try:
        with open(KV_FILE, "rb") as fd:
            data = fd.read()
    except OSError:
        return []

    import struct

    out = []
    pos = 0
    while True:
        start = data.find(b"bplist00", pos)
        if start < 0:
            break
        end = None
        for trailer_at in range(start + 40, min(start + 1 << 16, len(data)) - 32 + 1):
            t = data[trailer_at:trailer_at + 32]
            ois, ors = t[6], t[7]
            if not (1 <= ois <= 8) or not (1 <= ors <= 8):
                continue
            num, _, oto = struct.unpack(">QQQ", t[8:32])
            if oto + 32 + num * ois == trailer_at + 32 - start:
                end = trailer_at + 32
                break
        if end is None:
            pos = start + 8
            continue
        try:
            plist = plistlib.loads(data[start:end])
        except Exception:
            pos = start + 8
            continue
        pos = end
        if not isinstance(plist, dict) or "$objects" not in plist:
            continue
        record = _decode_archiver(plist)
        if record.get("origin"):
            out.append(record)
    return out


def _decode_archiver(plist: dict):
    objs = plist["$objects"]

    def resolve(o, depth=0):
        if depth > 20:
            return None
        if isinstance(o, plistlib.UID):
            return resolve(objs[o.data], depth + 1)
        if isinstance(o, dict):
            if "NS.keys" in o:
                return {resolve(k, depth + 1): resolve(v, depth + 1)
                        for k, v in zip(o["NS.keys"], o["NS.objects"])}
            return {k: resolve(v, depth + 1) for k, v in o.items() if k != "$class"}
        if isinstance(o, list):
            return [resolve(x, depth + 1) for x in o]
        return o

    try:
        return resolve(plist["$top"]["root"])
    except Exception:
        return {}


def _dedupe(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        key = (r.get("word"), r.get("lang"), r.get("text"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def kv_match(text: str) -> list[dict]:
    from difflib import SequenceMatcher

    wanted = normalize(text)
    try:
        raw = open(KV_FILE, "rb").read()
    except OSError:
        return []
    if wanted.encode("utf-8") not in raw.lower():
        return []          # 快路径：文件里根本没这段文字

    hits = []
    for rec in kv_records():
        origin = rec.get("origin") or ""
        if normalize(origin) == wanted:
            hits.append({
                "source": "eudic-translate",
                "engine": rec.get("engineId"),
                "lang": rec.get("destLang"),
                "word": origin,
                "text": rec.get("result") or "",
                "html": "",
            })
    if not hits:  # 退一步：模糊匹配（App 有时会改写原文）
        for rec in kv_records():
            origin = rec.get("origin") or ""
            if SequenceMatcher(None, normalize(origin), wanted).ratio() > 0.92:
                hits.append({"source": "eudic-translate", "engine": rec.get("engineId"),
                             "lang": rec.get("destLang"), "word": origin,
                             "text": rec.get("result") or "", "html": ""})
    return _dedupe(hits)


# ---------------------------------------------------------------------- lookup
def cached(word: str, lang: str | None = None) -> list[dict]:
    """缓存优先：按 URL 编码后的形似匹配已有条目。"""
    quoted = urllib.parse.quote(word)
    sql = (
        "select r.request_key, r.time_stamp, d.receiver_data "
        "from cfurl_cache_response r join cfurl_cache_receiver_data d "
        "on d.entry_ID = r.entry_ID "
        "where (r.request_key like ? or r.request_key like ?) "
        "order by r.time_stamp desc limit 40"
    )
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, ("%" + quoted + "%", "%" + quoted.replace(" ", "+") + "%"))]
    out = []
    for row in rows:
        params = query_of(row["request_key"])
        if not is_definition(row["request_key"]):
            continue
        body = body_of(row)
        if not body:
            continue
        out.append(pack(row, params, body))
    return out


DEF_ENDPOINTS = ("/api/v2/dicts/search", "/dp/dp", "/api/v2/dicts/en/pronounce")


def is_definition(request_key: str) -> bool:
    path = urllib.parse.urlparse(request_key).path
    return any(path.endswith(e) or e in path for e in DEF_ENDPOINTS)


def pack(row: dict, params: dict, body: str) -> dict:
    return {
        "source": urllib.parse.urlparse(row["request_key"]).netloc,
        "endpoint": urllib.parse.urlparse(row["request_key"]).path,
        "word": params.get("word") or params.get("q"),
        "lang": params.get("lang"),
        "libid": params.get("libid"),
        "cached_at": row.get("time_stamp"),
        "html": body,
        "text": to_text(body),
    }


def lookup(word: str, timeout: float = 10.0, use_trigger: bool = True) -> dict:
    started = time.time()

    def collect(mark: int) -> tuple[list[dict], list[dict], int]:
        hits = []
        rows = rows_since(mark)
        for row in rows:
            if not is_definition(row["request_key"]):
                continue
            body = body_of(row)
            if body:
                hits.append(pack(row, query_of(row["request_key"]), body))
        return hits, kv_match(word), len(rows)

    def reply(hit: bool, hits: list[dict], tx: list[dict]) -> dict:
        out = {"word": word, "cache_hit": hit,
               "elapsed": round(time.time() - started, 3),
               "results": hits, "translations": tx}
        if not hits and not tx:
            out["hint"] = ("两处都没命中：词条看 ~/Library/Caches/com.eusoft.eudic/Cache.db，"
                           "句子翻译看 ~/Library/Eudb_en/kvCache/TranslationResult。"
                           "若已在 App 里查过仍为 0，说明缓存被清了或语言目录不同。")
        return out

    hits, tx = cached(word), kv_match(word)
    if hits or tx:
        return reply(True, hits, tx)
    if not use_trigger:
        return reply(False, [], [])

    with _LOCK:
        hits, tx = cached(word), kv_match(word)      # 等锁期间别人可能刚查过
        if hits or tx:
            return reply(True, hits, tx)

        mark = max_entry_id()

        def wait(deadline: float, idle_limit: float) -> tuple[list[dict], list[dict]]:
            """轮询到命中或超时。App 接受查询后会先打发音/联想等请求，
            若 idle_limit 前一条新行都没有，说明查询被丢弃了。"""
            hits, tx, seen_rows = [], [], 0
            while time.time() < deadline:
                time.sleep(0.15)
                try:
                    hits, tx, seen_rows = collect(mark)
                except sqlite3.OperationalError:
                    continue
                if hits or tx:
                    time.sleep(0.25)             # 让同一批请求都落盘
                    try:
                        more_hits, more_tx, _ = collect(mark)
                        hits = more_hits or hits
                        tx = more_tx or tx
                    except sqlite3.OperationalError:
                        pass
                    break
                if not seen_rows and time.time() > idle_limit:
                    break
            return hits, tx

        # App 偶尔会把第一次查询咽掉（窗口正在重绘/刚被唤起），重试一次。
        deadline = time.time() + timeout
        hits, tx = [], []
        for attempt in range(2):
            try:
                trigger_lookup(word)
            except (subprocess.SubprocessError, OSError) as exc:
                if attempt == 0:
                    time.sleep(0.3)
                    continue
                out = reply(False, [], [])
                out["error"] = "trigger failed: %s" % exc
                return out
            idle_limit = min(time.time() + min(2.5, timeout), deadline)
            hits, tx = wait(deadline, idle_limit)
            if hits or tx or time.time() >= deadline:
                break
            time.sleep(0.3)

    return reply(bool(hits or tx), hits, tx)


# ------------------------------------------------- Google Translation API v2
PREFS_PLIST = os.path.expanduser("~/Library/Preferences/com.eusoft.eudic.plist")
LANGS = {
    "af": "Afrikaans", "ar": "Arabic", "bg": "Bulgarian", "bn": "Bengali",
    "ca": "Catalan", "cs": "Czech", "da": "Danish", "de": "German",
    "el": "Greek", "en": "English", "es": "Spanish", "et": "Estonian",
    "fa": "Persian", "fi": "Finnish", "fr": "French", "he": "Hebrew",
    "hi": "Hindi", "hr": "Croatian", "hu": "Hungarian", "id": "Indonesian",
    "it": "Italian", "ja": "Japanese", "ko": "Korean", "lt": "Lithuanian",
    "lv": "Latvian", "ms": "Malay", "nl": "Dutch", "no": "Norwegian",
    "pl": "Polish", "pt": "Portuguese", "ro": "Romanian", "ru": "Russian",
    "sk": "Slovak", "sl": "Slovenian", "sv": "Swedish", "th": "Thai",
    "tr": "Turkish", "uk": "Ukrainian", "vi": "Vietnamese",
    "zh-CN": "Chinese (Simplified)", "zh-TW": "Chinese (Traditional)",
}

_DEST_LANG: str | None = None


def dest_lang() -> str:
    """欧路词典当前设置的译文语言（偏好设置里的 TRANS_DestLangItem）。"""
    global _DEST_LANG
    if _DEST_LANG is None:
        value = ""
        try:
            res = subprocess.run(["defaults", "read", "com.eusoft.eudic", "TRANS_DestLangItem"],
                                 capture_output=True, text=True, timeout=5)
            value = res.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass
        if not value:
            try:
                with open(PREFS_PLIST, "rb") as fd:
                    value = str(plistlib.load(fd).get("TRANS_DestLangItem") or "")
            except Exception:  # noqa: BLE001
                value = ""
        _DEST_LANG = value or "zh-CN"
    return _DEST_LANG


def lang_base(code: str | None) -> str:
    return re.split(r"[-_]", str(code or "").strip().lower())[0]


def dict_langs() -> set[str]:
    """本机装了哪些语言的词典：~/Library/Eudb_en、Eudb_zh …"""
    out = set()
    try:
        for name in os.listdir(os.path.expanduser("~/Library")):
            if name.startswith("Eudb_") and len(name) > 5:
                out.add(name[5:])
    except OSError:
        pass
    return out


_TARGETS_CACHE: tuple[float, set[str]] = (0.0, set())
_TARGETS_TTL = 30.0


def supported_targets() -> set[str]:
    """能产出的目标语言 = App 设置的译文语言 + KV 里出现过的 destLang + 已装词典语言。

    欧路词典是双向的：中→英的译文会以 destLang="en" 落进 KV，所以不能只看
    TRANS_DestLangItem（它只说明「默认」方向）。
    """
    global _TARGETS_CACHE
    stamp, codes = _TARGETS_CACHE
    if codes and time.time() - stamp < _TARGETS_TTL:
        return codes
    codes = {dest_lang()} | dict_langs()
    for rec in kv_records():
        if rec.get("destLang"):
            codes.add(str(rec["destLang"]))
    _TARGETS_CACHE = (time.time(), codes)
    return codes


def target_supported(target: str) -> bool:
    return any(lang_compatible(target, code) for code in supported_targets())


def lang_compatible(want: str | None, got: str | None) -> bool:
    if not want or want.lower() == "auto":
        return True
    return lang_base(want) == lang_base(got)


SCRIPT_TESTS = (
    ("ja", r"[\u3040-\u30ff]"),
    ("ko", r"[\uac00-\ud7af\u1100-\u11ff]"),
    ("zh-CN", r"[\u4e00-\u9fff]"),
    ("ru", r"[\u0400-\u04ff]"),
    ("ar", r"[\u0600-\u06ff]"),
    ("he", r"[\u0590-\u05ff]"),
    ("th", r"[\u0e00-\u0e7f]"),
    ("hi", r"[\u0900-\u097f]"),
    ("bn", r"[\u0980-\u09ff]"),
    ("ta", r"[\u0b80-\u0bff]"),
    ("te", r"[\u0c00-\u0c7f]"),
    ("el", r"[\u0370-\u03ff]"),
    ("ka", r"[\u10a0-\u10ff]"),
    ("km", r"[\u1780-\u17ff]"),
)


def detect_language(text: str) -> str:
    """极素的语言猜测：按字符集判断，认不出就当英语（本机装的就是英文词典）。"""
    for code, pattern in SCRIPT_TESTS:
        if re.search(pattern, text):
            return code
    return "en"


def escape_translation(text: str) -> str:
    """Google v2 的 translatedText 永远是 HTML 转义过的。"""
    return html.escape(text, quote=True)


def translate_one(text: str, target: str, source: str | None, fmt: str,
                  timeout: float, use_trigger: bool) -> tuple[str | None, str, str | None]:
    """返回 (译文本, detectedSourceLanguage, 错误信息)。"""
    res = lookup(text, timeout=timeout, use_trigger=use_trigger)
    detected = source if source and source.lower() != "auto" else detect_language(text)

    # App 偶尔把原文当译文回写（流式中间态/失败），这种不能当结果返回
    def usable(item: dict) -> str | None:
        value = item.get("text") or ""
        if not value or normalize(value) == normalize(text):
            return None
        return value

    translations = res.get("translations") or []
    best = None
    for item in translations:                        # 先挑语言方向对上的
        if lang_compatible(target, item.get("lang")):
            best = usable(item)
            if best:
                break
    if best is None:
        for item in translations:                    # 再退一步：有什么用什么
            best = usable(item)
            if best:
                break
    if best:
        return best, detected, None

    # 单词等走词典缓存：优先 HTML，其次纯文本
    for hit in res.get("results") or []:
        value = (hit.get("html") if fmt == "html" else None) or hit.get("text")
        if value:
            return value, detected, None

    return None, detected, "no cached translation: 这段文字还没在欧路词典里翻译过"


# ----------------------------------------------------------------------- http
def first(params: dict, key: str, default: str | None = None) -> str | None:
    values = params.get(key)
    if not values:
        return default
    return values[0] if isinstance(values, list) else values


class Handler(BaseHTTPRequestHandler):
    server_version = "eudic-api/0.3"

    # ------------------------------------------------------------- plumbing
    def _send(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, message: str, code: int = 400, reason: str = "invalidArgument") -> None:
        """Google 错误信封。"""
        self._send({"error": {"code": code, "message": message,
                               "errors": [{"message": message,
                                           "domain": "global", "reason": reason}]}}, code)

    def _params(self) -> tuple[dict, list[str]]:
        """合并 query string 与 POST body，返回 (参数表, 所有 q)。"""
        pairs = urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query,
                                       keep_blank_values=True)
        params: dict[str, list[str]] = {}
        texts: list[str] = []
        raw = b""
        if self.command == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""

        if raw and "json" in (self.headers.get("Content-Type") or "").lower():
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("malformed JSON body: %s" % exc)
            if not isinstance(body, dict):
                raise ValueError("JSON body must be an object")
            for key, value in body.items():
                if value is None:
                    continue
                if key == "q":
                    texts.extend(str(v) for v in (value if isinstance(value, list) else [value]))
                else:
                    params[key] = [str(v) for v in (value if isinstance(value, list) else [value])]
        elif raw:
            pairs += urllib.parse.parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True)

        for key, value in pairs:
            if key == "q":
                texts.append(value)
            else:
                params.setdefault(key, []).append(value)
        return params, texts

    # ------------------------------------------------------------- handlers
    def _translate(self, params: dict, texts: list[str]) -> None:
        target = first(params, "target")
        if not target:
            return self._send_error("Missing required parameter: target")
        if not target_supported(target):
            return self._send_error(
                "Unsupported target language: %s (supported: %s)"
                % (target, ", ".join(sorted(supported_targets()))), 400)

        fmt = (first(params, "format") or "text").lower()
        if fmt not in ("text", "html"):
            return self._send_error('Invalid value for format: must be "text" or "html"')

        source = first(params, "source")
        timeout = float(first(params, "timeout") or 10)
        use_trigger = str(first(params, "trigger") or "1").lower() not in ("0", "false", "no")

        texts = [t for t in texts if t.strip()]
        if not texts:
            return self._send_error("Missing required parameter: q")

        out = []
        for text in texts:
            translated, detected, err = translate_one(text, target, source, fmt, timeout, use_trigger)
            if translated is None:
                return self._send_error("Translation failed for %r: %s%s"
                                        % (text, err, self._direction_hint(target)),
                                        500, "translationError")
            out.append({"translatedText": escape_translation(translated),
                        "detectedSourceLanguage": detected})
        self._send({"data": {"translations": out}})

    def _direction_hint(self, target: str) -> str:
        """目标方向不是 App 当前译文语言时，告诉调用方为什么翻不出来。"""
        current = dest_lang()
        if lang_compatible(target, current):
            return ""
        return ("。提示：欧路词典当前译文语言是 %s（偏好设置 TRANS_DestLangItem），"
                "不重启 App 就只能翻成这个语言；要翻成 %s 需要先把 App 的译文语言改成 "
                "%s，再在 App 里翻一次（吃过这次之后会进缓存，之后就能直接查到）"
                % (current, target, target))

    def _detect(self, params: dict, texts: list[str]) -> None:
        texts = [t for t in texts if t.strip()]
        if not texts:
            return self._send_error("Missing required parameter: q")
        self._send({"data": {"detections": [
            [{"language": detect_language(t), "isReliable": False, "confidence": 0.85}] for t in texts
        ]}})

    def _languages(self, params: dict) -> None:
        target = first(params, "target")
        default = dest_lang()
        codes = sorted(supported_targets(), key=lambda c: (c != default, c))
        out = []
        for code in codes:
            item = {"language": code}
            if target:  # Google 只在带 target 时返回 name
                item["name"] = LANGS.get(code) or LANGS.get(lang_base(code)) or code
            out.append(item)
        self._send({"data": {"languages": out}})

    def _route(self) -> None:
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        if path == "/health":
            return self._send({"ok": os.path.exists(CACHE_DB), "cache_db": CACHE_DB,
                               "target": dest_lang(),
                               "targets": sorted(supported_targets())})

        params, texts = self._params()
        if path == "/language/translate/v2":
            return self._translate(params, texts)
        if path == "/language/translate/v2/detect":
            return self._detect(params, texts)
        if path == "/language/translate/v2/languages":
            return self._languages(params)
        if path == "/lookup":  # 调试入口：原始缓存条目
            word = first(params, "word") or ""
            word = word.strip()
            if not word:
                return self._send_error("Missing required parameter: word")
            return self._send(lookup(word, float(first(params, "timeout") or 10),
                                     str(first(params, "trigger") or "1").lower()
                                     not in ("0", "false", "no")))
        self._send_error("Not found: %s" % (url.path or "/"), 404, "notFound")

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route()
        except ValueError as exc:
            self._send_error(str(exc))
        except Exception as exc:  # noqa: BLE001
            self._send_error(repr(exc), 500, "internalError")

    def do_POST(self) -> None:  # noqa: N802
        self.do_GET()

    def log_message(self, fmt, *args) -> None:
        print("[api] %s" % (fmt % args), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    print("serving on http://127.0.0.1:%d" % args.port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
