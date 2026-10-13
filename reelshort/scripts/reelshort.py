#!/usr/bin/env python3
"""ReelShort CPS：登录 -> 搜索短剧 -> 取详情 -> 解析 App/资源推广短链。

一次调用只处理一个账号。多账号由调用方逐个调用、逐个返回。
仅依赖 Python 3.8+ 标准库。

退出码: 0 成功 | 1 运行错误 | 2 参数/账号/语言无法确定(需用户澄清) | 3 登录失败
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = "https://cps.reelshort.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
PAGE_MAX = 100          # 接口单页 limit 上限（实测 100 可用）
MAX_CHARS = 16000       # 单次输出字符兜底（聊天卡片/消息长度限制）
WORKERS = 5             # 详情 + 短链解析并发数
DEEPLINK_RE = re.compile(r"(&deep_link_value=[^&\s]*&af_dp=[^&\s]*)")


class UserError(Exception):
    """需要用户澄清/修正的输入问题 -> 退出码 2。"""


class LoginError(Exception):
    """登录失败 -> 退出码 3。"""


# --------------------------------------------------------------------------- #
# 配置与模糊匹配
# --------------------------------------------------------------------------- #
def load_json(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        raise UserError(f"缺少配置文件: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise UserError(f"配置文件 {name} 不是合法 JSON: {e}")


def norm(text: str) -> str:
    """NFKC 归一 + 小写 + 去空白，用于所有模糊匹配。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).lower()


def strip_punct(text: str) -> str:
    return re.sub(r"[\W_]+", "", norm(text))


def resolve_account(query: str, accounts: list) -> dict:
    """精确邮箱 > 邮箱前缀 > 别名精确 > 子串(>=3字符)。多命中则报歧义。"""
    q = norm(query)
    if not q:
        raise UserError("未提供账号")
    emails = [a["email"] for a in accounts]

    def pick(pred):
        hits = [a for a in accounts if pred(a)]
        return hits

    steps = [
        lambda a: norm(a["email"]) == q,
        lambda a: norm(a["email"].split("@")[0]) == q,
        lambda a: q in [norm(x) for x in a.get("aliases", [])],
    ]
    if len(q) >= 3:
        steps.append(lambda a: q in norm(a["email"]) or
                     any(q in norm(x) for x in a.get("aliases", [])))
        # 用户输入里“包含”邮箱/别名的情况（如 “用 jenhu 账号”）
        steps.append(lambda a: any(len(norm(x)) >= 3 and norm(x) in q
                                   for x in [a["email"].split("@")[0]] + a.get("aliases", [])))
    for pred in steps:
        hits = pick(pred)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise UserError("账号不明确，命中多个: " + ", ".join(h["email"] for h in hits)
                            + "。请指定其中一个。")
    raise UserError(f"未找到账号 “{query}”。可用账号: " + ", ".join(emails))


def resolve_choice(text: str, table: dict, label: str) -> str:
    """把口语映射到接口取值。table: {code: {name, aliases}}。返回 code。"""
    q = norm(text)
    if not q:
        raise UserError(f"{label}为空")
    cand = {}  # alias(norm) -> code
    for code, meta in table.items():
        for a in [code, meta.get("name", "")] + list(meta.get("aliases", [])):
            if a:
                cand.setdefault(norm(a), code)
    if q in cand:
        return cand[q]
    best = None
    for a, code in cand.items():
        if a.isascii() and len(a) < 4:
            continue
        if len(a) >= 2 and a in q and (best is None or len(a) > len(best[0])):
            best = (a, code)
    if best:
        return best[1]
    opts = ", ".join(f"{m.get('name', c)}({c})" for c, m in table.items())
    raise UserError(f"无法识别{label} “{text}”。可选: {opts}")


def split_multi(values):
    out = []
    for v in values or []:
        out.extend(x.strip() for x in re.split(r"[,，、;；]", v) if x.strip())
    return out


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def api(path: str, body: dict, token: str = "", retries: int = 3) -> dict:
    headers = {
        "content-type": "application/json", "accept": "application/json",
        "accept-language": "zh", "origin": BASE, "user-agent": UA,
    }
    if token:
        headers["authorization"] = "Bearer " + token
    data = json.dumps(body).encode("utf-8")
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(BASE + path, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"请求 {path} 失败: {last}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def resolve_short_link(url: str, retries: int = 3) -> str:
    """GET 短链但不跟随跳转，从 302 Location 截取 &deep_link_value=...&af_dp=...。"""
    if not url:
        return ""
    opener = urllib.request.build_opener(_NoRedirect)
    last = "未知错误"
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"user-agent": UA})
            try:
                resp = opener.open(req, timeout=20)
                loc = resp.headers.get("Location", "")
            except urllib.error.HTTPError as e:   # 302 会走这里
                loc = e.headers.get("Location", "")
            m = DEEPLINK_RE.search(loc or "")
            if m:
                return m.group(1)
            last = "跳转地址中未找到 deep_link_value"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = str(e)
        time.sleep(1.2 * (i + 1))
    raise RuntimeError(last)


# --------------------------------------------------------------------------- #
# 业务
# --------------------------------------------------------------------------- #
def login(acct: dict) -> dict:
    d = api("/api/v1/user/login", {"channel_account": acct["email"],
                                   "password": acct["password"], "channel_type": "email"})
    if d.get("code") != 0 or not (d.get("data") or {}).get("accessToken"):
        raise LoginError(f"{acct['email']} - {d.get('msg') or d}")
    return d["data"]


def search_books(token: str, sort: str, lang: str, keyword: str, count: int) -> list:
    """游标翻页取够 count 本；空结果时用去标点关键词再试一次。"""
    def run(kw):
        books, cursor = [], ""
        while len(books) < count:
            body = {"app": "reelshort", "sort": sort, "limit": min(PAGE_MAX, count - len(books))}
            if lang:
                body["language"] = lang
            if kw:
                body["search"] = kw
            body.update({"cursor": cursor} if cursor else {"page": 1})
            d = api("/api/v1/book/all-book", body, token)
            if d.get("code") != 0:
                raise RuntimeError(f"搜索失败: {d.get('msg')}")
            data = d.get("data") or {}
            page = data.get("books") or []
            books.extend(page)
            cursor = data.get("next_cursor") or ""
            if not page or not cursor:
                break
        return books[:count]

    books = run(keyword)
    if not books and keyword and strip_punct(keyword) != norm(keyword):
        books = run(strip_punct(keyword))
    return books


def fetch_links(token: str, book: dict) -> dict:
    out = {"id": book["id"], "title": book.get("title", ""), "lang": book.get("lang", ""),
           "app_short": "", "app": "", "book_short": "", "book": "", "error": ""}
    try:
        d = api("/api/v1/book/book-detail",
                {"app": "reelshort", "book_id": book["id"], "book_type": book.get("book_type", 0)}, token)
        if d.get("code") != 0:
            raise RuntimeError(f"详情失败: {d.get('msg')}")
        det = d.get("data") or {}
        errs = []
        for key, field in (("app", "app_promotion_link"), ("book", "book_promotion_link")):
            out[key + "_short"] = det.get(field, "") or ""
            try:
                out[key] = resolve_short_link(out[key + "_short"])
            except RuntimeError as e:
                errs.append(f"{'App' if key == 'app' else '资源'}链接解析失败({e})")
        out["error"] = "; ".join(errs)
    except RuntimeError as e:
        out["error"] = str(e)
    return out


def run_account(acct: dict, titles, langs, sort, count, offset=0, page_size=15) -> dict:
    user = login(acct)
    token = user["accessToken"]
    info = user.get("user_info") or {}
    seen, books = set(), []
    for kw in titles or [""]:
        for lg in langs or [""]:
            for b in search_books(token, sort, lg, kw, count):
                if b["id"] not in seen:
                    seen.add(b["id"])
                    books.append(b)
    total = len(books)
    window = books[offset:offset + page_size]      # 只对本批展示的剧取详情/解析短链
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(lambda b: fetch_links(token, b), window))
    return {"email": acct["email"], "user_id": info.get("user_id"),
            "total": total, "offset": offset, "books": results}


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def md_escape(text: str) -> str:
    """转义标题里可能破坏 Markdown 的字符。"""
    return re.sub(r"([\\`*_{}\[\]<>#|])", r"\\\1", text or "")


def render_card(idx: int, b: dict) -> str:
    """一部剧 = 一个标题行 + 一个整体代码块（标签用注释行）。"""
    title = f"**{idx}. {md_escape(b['title'])}**" + (f"　`{b['lang']}`" if b["lang"] else "")
    code = [
        "# App推广链接", b["app_short"] or "无",
        "# App截取信息结果", b["app"] or "无",
        "# 资源推广链接", b["book_short"] or "无",
        "# 资源截取信息结果", b["book"] or "无",
    ]
    lines = ["---", "", title, "```", *code, "```"]
    if b["error"]:
        lines += ["", f"> ⚠ {b['error']}"]
    return "\n".join(lines)


def render(res: dict, max_chars: int = MAX_CHARS) -> str:
    """Markdown 卡片。超过本批上限或字符上限时截断，并提示还剩多少、下一批 offset。"""
    total, offset = res["total"], res["offset"]
    out = [f"## 👤 {res['email']} · 共 {total} 部"]
    if not total:
        return "\n".join(out + ["", "> 未找到符合条件的短剧。"])
    if not res["books"]:
        return "\n".join(out + ["", f"> 已无更多结果（共 {total} 部，offset={offset} 已超出）。"])

    size, shown = len(out[0]), 0
    for i, b in enumerate(res["books"], offset + 1):
        card = render_card(i, b)
        if shown and size + len(card) > max_chars:   # 字符兜底，至少展示 1 部
            break
        out += ["", card]
        size += len(card) + 2
        shown += 1

    end = offset + shown
    if end < total:
        out += ["", f"> 共 {total} 部，本次展示第 {offset + 1}–{end} 部，还剩 {total - end} 部。"
                    f"回复“继续”获取下一批（offset={end}）。"]
    elif offset:
        out += ["", f"> 共 {total} 部，本次展示第 {offset + 1}–{end} 部，已全部展示。"]
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description="ReelShort CPS 推广链接获取（单账号）")
    p.add_argument("--account", help="账号：邮箱 / 前缀 / 别名 / 公司名")
    p.add_argument("--title", action="append", help="剧名关键词，可重复或逗号分隔")
    p.add_argument("--lang", action="append", help="语言，口语或代码均可，可重复或逗号分隔")
    p.add_argument("--sort", help="排序：time(默认,按添加时间) / money(近30天充值)，口语亦可")
    p.add_argument("--count", type=int, help="获取个数（每个 剧名×语言 组合各取这么多）")
    p.add_argument("--offset", type=int, default=0, help="从第几部之后开始展示（分批取，默认 0）")
    p.add_argument("--page-size", type=int, help="单次最多展示几部（默认见 aliases.json，超出会截断并提示）")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--dry-run", action="store_true", help="只解析参数，不联网")
    p.add_argument("--list-accounts", action="store_true", help="列出可用账号邮箱(不含密码)")
    a = p.parse_args()

    try:
        accounts = load_json("accounts.json")["accounts"]
        if a.list_accounts:
            for x in accounts:
                print(x["email"])
            return 0
        if not a.account:
            raise UserError("缺少 --account")
        ali = load_json("aliases.json")
        dft = ali.get("defaults", {})
        acct = resolve_account(a.account, accounts)
        langs = [resolve_choice(x, ali["languages"], "语言") for x in split_multi(a.lang)]
        langs = list(dict.fromkeys(langs))
        titles = list(dict.fromkeys(split_multi(a.title)))
        sort = resolve_choice(a.sort, ali["sorts"], "排序") if a.sort else dft.get("sort", "time")
        count = a.count if a.count is not None else dft.get("count", 10)
        if count < 1:
            raise UserError("--count 必须 >= 1")
        count = min(count, dft.get("max_count", 200))
        if a.offset < 0:
            raise UserError("--offset 必须 >= 0")
        page_size = a.page_size if a.page_size is not None else dft.get("page_size", 15)
        if page_size < 1:
            raise UserError("--page-size 必须 >= 1")
        page_size = min(page_size, dft.get("max_page_size", 30))

        if a.dry_run:
            print(json.dumps({"account": acct["email"], "titles": titles, "langs": langs,
                              "sort": sort, "count": count, "offset": a.offset,
                              "page_size": page_size}, ensure_ascii=False))
            return 0

        res = run_account(acct, titles, langs, sort, count, a.offset, page_size)
        print(json.dumps(res, ensure_ascii=False, indent=2) if a.json else render(res))
        return 0
    except UserError as e:
        print(f"需要确认: {e}", file=sys.stderr)
        return 2
    except LoginError as e:
        print(f"登录失败: {e}", file=sys.stderr)
        return 3
    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
