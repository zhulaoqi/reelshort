#!/usr/bin/env python3
"""ReelShort CPS：导出「短剧预估收益」明细（.xlsx）。

流程：匹配账号 -> 登录 -> 按条件汇总统计 -> 调导出接口下载 xlsx -> 保存到本地并输出摘要。
一次调用只处理一个账号；多账号由调用方逐个调用。
依赖 Python 3.9+ 标准库（时区用 zoneinfo）。

退出码: 0 成功 | 1 运行错误 | 2 参数/账号/日期无法确定(需用户澄清) | 3 登录失败
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from reelshort import (BASE, UA, LoginError, UserError, api, load_json, login, norm,
                       resolve_account, resolve_choice, search_books, split_multi)

PLATFORM_TZ = "America/Los_Angeles"      # 平台前端 dayjs.tz.setDefault 的时区，决定“昨天/今天”
OUT_DIRNAME = "reelshort_earnings"        # 我们自己的临时子目录名（只有这个名字的目录才会被自动清理）
OPENCLAW_TMP = Path("/tmp/openclaw")      # OpenClaw/飞书插件允许发送文件的白名单临时目录
FILE_PREFIX = "Drama_Estimated_Earnings_"
FILE_TTL_MIN = 60                         # 兜底：临时目录里超过这个时长的旧文件，下次运行自动清理


def default_out_dir() -> Path:
    """默认输出目录，优先选“聊天插件允许发送附件”的位置，否则附件会被插件静默拦截：
    1. 环境变量 REELSHORT_OUT_DIR（部署时指向 agent 工作区/白名单目录）
    2. /tmp/openclaw（OpenClaw 飞书插件的白名单临时目录，存在即用）
    3. 系统临时目录
    """
    env = os.environ.get("REELSHORT_OUT_DIR", "").strip()
    if env:
        return Path(env).expanduser() / OUT_DIRNAME
    if OPENCLAW_TMP.is_dir():
        return OPENCLAW_TMP / OUT_DIRNAME
    return Path(tempfile.gettempdir()) / OUT_DIRNAME


def purge_stale(directory: Path, ttl_min: int = FILE_TTL_MIN) -> int:
    """删除目录中本脚本生成、且已过期的 xlsx。只清理自己的文件，返回删除个数。"""
    n, now = 0, time.time()
    if not directory.is_dir():
        return 0
    for f in directory.glob(FILE_PREFIX + "*.xlsx"):
        try:
            if now - f.stat().st_mtime > ttl_min * 60:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


def cleanup_file(path_text: str) -> str:
    """发送成功后立即删除。仅允许删除本脚本生成的文件（名称前缀 + .xlsx），防止误删。"""
    p = Path(path_text).expanduser()
    if not (p.name.startswith(FILE_PREFIX) and p.suffix == ".xlsx"):
        raise UserError(f"拒绝清理：不是本工具生成的收益文件 {p.name}")
    if not p.exists():
        return "文件已不存在，无需清理"
    p.unlink()
    try:
        p.parent.rmdir()                  # 目录空了就一并删除（非空会抛错，忽略）
    except OSError:
        pass
    return "已清理"
WINDOW_DAYS = 30                          # 页面日期选择器只允许近 30 天
MAX_BOOKS = 20                            # 剧名搜索最多取多少部作为筛选


# --------------------------------------------------------------------------- #
# 日期解析（按平台时区）
# --------------------------------------------------------------------------- #
def platform_today() -> dt.date:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(PLATFORM_TZ)).date()
    except Exception:  # 无 tzdata 时退化为 UTC-7
        return (dt.datetime.utcnow() - dt.timedelta(hours=7)).date()


_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def cn_int(s: str) -> int:
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + _CN[s[1:]]
    if s.endswith("十"):
        return _CN[s[0]] * 10
    if "十" in s:
        a, b = s.split("十")
        return _CN[a] * 10 + _CN[b]
    return _CN[s]


def month_range(y: int, m: int):
    if not 1 <= m <= 12:
        raise UserError(f"月份不合法: {m}")
    start = dt.date(y, m, 1)
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return start, nxt - dt.timedelta(days=1)


def parse_date_range(text: str, today: dt.date):
    """把口语日期转成 (start, end)。支持：昨天/今天/前天、最近N天/周/个月、上周/本周、
    上月/本月、M月、M月D日、YYYY-MM-DD、MM-DD、以及 “A 到/至/~ B” 区间。"""
    t = norm(text)
    if not t:
        raise UserError("日期为空")
    y = today - dt.timedelta(days=1)
    fixed = {"今天": (today, today), "今日": (today, today), "昨天": (y, y), "昨日": (y, y),
             "前天": (today - dt.timedelta(days=2),) * 2}
    if t in fixed:
        return fixed[t]

    m = re.fullmatch(r"(?:最近|近|过去)([0-9]+|[零一二两三四五六七八九十]+)(天|日|周|个?星期|个?月)", t)
    if m:
        n = cn_int(m.group(1))
        unit = m.group(2)
        days = n if unit in ("天", "日") else n * 7 if unit in ("周",) or "星期" in unit else n * 30
        return y - dt.timedelta(days=days - 1), y          # 以昨天为终点
    if t in ("上周", "上星期", "上一周"):
        mon = today - dt.timedelta(days=today.weekday() + 7)
        return mon, mon + dt.timedelta(days=6)
    if t in ("本周", "这周", "这星期", "本星期"):
        return today - dt.timedelta(days=today.weekday()), today
    if t in ("上月", "上个月", "上一个月"):
        first = today.replace(day=1) - dt.timedelta(days=1)
        return month_range(first.year, first.month)
    if t in ("本月", "这个月", "这月", "当月"):
        return today.replace(day=1), today

    m = re.fullmatch(r"(?:(\d{4})年)?(\d{1,2})月份?", t)
    if m:
        s, e = month_range(int(m.group(1) or today.year), int(m.group(2)))
        if not m.group(1) and s > today:                    # 未来月份 -> 去年
            s, e = month_range(today.year - 1, int(m.group(2)))
        return s, min(e, today)                             # 当月只到今天，不额外告警

    # 具体日期（单个或区间）
    tok = re.compile(r"(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?")
    dates = []
    for g in tok.finditer(re.sub(r"(\d{4})(\d{2})(\d{2})", r"\1-\2-\3", t)):
        yr, mo, da = g.group(1), int(g.group(2)), int(g.group(3))
        try:
            d = dt.date(int(yr), mo, da) if yr else dt.date(today.year, mo, da)
            if not yr and d > today:
                d = d.replace(year=today.year - 1)
        except ValueError:
            raise UserError(f"日期不合法: {g.group(0)}")
        dates.append(d)
    if len(dates) == 1:
        tail = re.search(r"(?:到|至|~|—|–|-)(\d{1,2})[日号]$", t)   # “10月1日-7日”
        if tail:
            try:
                dates.append(dates[0].replace(day=int(tail.group(1))))
            except ValueError:
                raise UserError(f"日期不合法: {text}")
    if len(dates) == 1:
        return dates[0], dates[0]
    if len(dates) == 2:
        a, b = sorted(dates)
        return a, b
    raise UserError(f"无法识别日期 “{text}”。可用：昨天 / 最近7天 / 上周 / 本月 / 10月 / "
                    "2026-10-01~2026-10-07 / 10月1日到10月7日")


def resolve_range(date_text, start_text, end_text, today: dt.date):
    """优先级：--start/--end > --date > 默认昨天。返回 (start, end, notes)。"""
    notes = []
    if start_text or end_text:
        s = parse_date_range(start_text, today)[0] if start_text else None
        e = parse_date_range(end_text, today)[1] if end_text else None
        s, e = s or e, e or s
    elif date_text:
        s, e = parse_date_range(date_text, today)
    else:
        s = e = today - dt.timedelta(days=1)
    if s > e:
        s, e = e, s
    if s > today:
        raise UserError(f"开始日期 {s} 晚于平台今天 {today}（洛杉矶时间），没有数据")
    if e > today:
        e = today
        notes.append(f"结束日期超出平台今天，已按 {today} 处理")
    if (e - s).days + 1 > WINDOW_DAYS:
        notes.append(f"区间超过 {WINDOW_DAYS} 天，页面仅支持近 {WINDOW_DAYS} 天，超出部分可能无数据")
    return s, e, notes


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
def download_xlsx(token: str, body: dict, retries: int = 3) -> bytes:
    headers = {"content-type": "application/json", "accept": "application/json",
               "accept-language": "zh", "origin": BASE, "user-agent": UA,
               "authorization": "Bearer " + token}
    data = json.dumps(body).encode("utf-8")
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(BASE + "/api/v1/bill/app-estimate-export",
                                         data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as r:
                blob = r.read()
            if blob[:2] == b"PK":                           # xlsx 就是 zip
                return blob
            try:                                            # 业务错误会以 JSON 返回
                msg = json.loads(blob.decode("utf-8")).get("msg")
            except Exception:
                msg = blob[:120]
            raise RuntimeError(f"导出失败: {msg}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"导出请求失败: {last}")


def run(acct: dict, a, ali: dict, start: dt.date, end: dt.date, book_filter: dict) -> dict:
    user = login(acct)
    token = user["accessToken"]

    # 剧名 / 语言 -> book_ids
    book_ids, matched = [], []
    if book_filter["titles"] or book_filter["langs"]:
        seen = set()
        for kw in book_filter["titles"] or [""]:
            for lg in book_filter["langs"] or [""]:
                for b in search_books(token, "time", lg, kw, MAX_BOOKS):
                    if b["id"] not in seen:
                        seen.add(b["id"])
                        book_ids.append(b["id"])
                        matched.append(b)
        if not book_ids:
            return {"email": acct["email"], "empty_reason": "未搜到符合条件的剧，无法筛选收益", "matched": []}

    body = {"app": "reelshort", "book_ids": book_ids,
            "min_date": start.isoformat(), "max_date": end.isoformat(),
            "promotion_value": (a.promotion_value or "").strip()}
    if book_filter["ptype"]:
        body["promotion_type"] = book_filter["ptype"]
    if a.has_order:
        body["exist_order"] = True

    lst = api("/api/v1/bill/app-estimate", {**body, "page": 1, "limit": 1}, token)
    if lst.get("code") != 0:
        raise RuntimeError(f"查询收益失败: {lst.get('msg')}")
    rows = (lst.get("data") or {}).get("total", 0)
    agg = api("/api/v1/bill/app-estimate-agg", body, token)
    agg = agg.get("data") or {} if agg.get("code") == 0 else {}

    res = {"email": acct["email"], "rows": rows, "matched": matched,
           "book_cnt": agg.get("book_cnt"), "order_cnt": agg.get("order_cnt"),
           "revenue": agg.get("total_revenue"), "file": ""}
    if rows == 0:
        return res

    blob = download_xlsx(token, {**body, "page": 1, "limit": 10})
    out_dir = Path(a.out_dir).expanduser() if a.out_dir else default_out_dir()
    if out_dir.name == OUT_DIRNAME:       # 我们自己的临时目录：顺手清掉过期文件，防止堆积
        purge_stale(out_dir)              # 用户自定义的其他目录不会被自动清理
    out_dir.mkdir(parents=True, exist_ok=True)
    who = re.sub(r"[^A-Za-z0-9._-]", "_", acct["email"].split("@")[0])
    # 带筛选条件时加短后缀，避免同账号同日期的不同筛选互相覆盖
    flt = {k: body[k] for k in ("book_ids", "promotion_type", "promotion_value", "exist_order")
           if body.get(k)}
    sfx = "_" + hashlib.md5(json.dumps(flt, sort_keys=True).encode()).hexdigest()[:6] if flt else ""
    path = out_dir / f"{FILE_PREFIX}{start}_{end}_{who}{sfx}.xlsx"
    path.write_bytes(blob)
    res["file"] = str(path)
    return res


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def render(res: dict, start, end, tags: list, notes: list) -> str:
    out = [f"## 📊 {res['email']} · 短剧预估收益", ""]
    out.append("　".join([f"`{start} ~ {end}（洛杉矶时间）`" if start != end else f"`{start}（洛杉矶时间）`"]
                         + [f"`{t}`" for t in tags]))
    if res.get("empty_reason"):
        return "\n".join(out + ["", f"> {res['empty_reason']}"] + [f"> ⚠ {n}" for n in notes])
    m = res.get("matched") or []
    if m:
        shown = "、".join(f"{b.get('title', '')}（{b.get('lang', '')}）" for b in m[:5])
        out += ["", f"> 剧名筛选命中 {len(m)} 部：{shown}" + ("…" if len(m) > 5 else "")]
    if not res["rows"]:
        out += ["", "> 该条件下没有收益数据。"]
    else:
        rev = res.get("revenue")
        rev_s = f"${float(rev):.2f}" if rev is not None else "-"
        out += ["", f"推广资源 {res.get('book_cnt', '-')} 个　·　订单 {res.get('order_cnt', '-')} 笔　·　"
                    f"预估收益 {rev_s}　·　明细 {res['rows']} 行"]    # 摘要不提文件/路径：文件本身就是附件
    for n in notes:
        out += ["", f"> ⚠ {n}"]
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description="ReelShort CPS 短剧预估收益导出（单账号）")
    p.add_argument("--account", help="账号：邮箱 / 前缀 / 别名 / 公司名")
    p.add_argument("--date", help="日期，口语或区间：昨天 / 最近7天 / 上周 / 10月 / 2026-10-01~2026-10-07；默认昨天")
    p.add_argument("--start", help="开始日期（与 --end 搭配）")
    p.add_argument("--end", help="结束日期（与 --start 搭配）")
    p.add_argument("--title", action="append", help="剧名关键词（按剧筛选），可重复或逗号分隔")
    p.add_argument("--lang", action="append", help="剧的语言，用于缩小剧名搜索范围")
    p.add_argument("--ptype", help="推广方式：资源链接 / App链接 / 口令 / 内嵌播放器")
    p.add_argument("--promotion-value", help="推广信息：链接或口令（最长 100 字符）")
    p.add_argument("--has-order", action="store_true", help="仅包含有订单数据的记录")
    p.add_argument("--out-dir", help="文件保存目录。默认自动选聊天插件允许发送的目录"
                                     "（环境变量 REELSHORT_OUT_DIR > /tmp/openclaw > 系统临时目录）下的 reelshort_earnings/，"
                                     f"发送后用 --cleanup 删除，超 {FILE_TTL_MIN} 分钟的旧文件下次运行自动清理")
    p.add_argument("--cleanup", metavar="文件路径", help="删除已发送完成的收益文件（仅限本工具生成的文件）")
    p.add_argument("--dry-run", action="store_true", help="只解析参数，不联网")
    p.add_argument("--today", help=argparse.SUPPRESS)       # 测试用：覆盖“平台今天”
    a = p.parse_args()

    try:
        if a.cleanup:                                       # 不需要账号，也不联网
            print(cleanup_file(a.cleanup))
            return 0
        if not a.account:
            raise UserError("缺少 --account")
        accounts = load_json("accounts.json")["accounts"]
        ali = load_json("aliases.json")
        acct = resolve_account(a.account, accounts)
        today = dt.date.fromisoformat(a.today) if a.today else platform_today()
        start, end, notes = resolve_range(a.date, a.start, a.end, today)
        langs = list(dict.fromkeys(resolve_choice(x, ali["languages"], "语言")
                                   for x in split_multi(a.lang)))
        titles = list(dict.fromkeys(split_multi(a.title)))
        ptype = resolve_choice(a.ptype, ali["promotion_types"], "推广方式") if a.ptype else ""
        if a.promotion_value and len(a.promotion_value.strip()) > 100:
            raise UserError("推广信息最长 100 字符")
        if langs and not titles:
            raise UserError("按语言筛选收益需要同时给出剧名；单独按语言没有对应的收益筛选条件")

        pname = ali["promotion_types"][ptype]["name"] if ptype else ""
        tags = ([f"剧名: {'/'.join(titles)}"] if titles else []) + \
               ([f"语言: {'/'.join(ali['languages'][x]['name'] for x in langs)}"] if langs else []) + \
               ([f"推广方式: {pname}"] if ptype else []) + \
               ([f"推广信息: {a.promotion_value.strip()}"] if a.promotion_value else []) + \
               (["仅有订单"] if a.has_order else [])

        if a.dry_run:
            print(json.dumps({"account": acct["email"], "start": str(start), "end": str(end),
                              "titles": titles, "langs": langs, "ptype": ptype,
                              "promotion_value": a.promotion_value or "", "has_order": a.has_order,
                              "notes": notes, "today(LA)": str(today)}, ensure_ascii=False))
            return 0

        res = run(acct, a, ali, start, end, {"titles": titles, "langs": langs, "ptype": ptype})
        print(render(res, start, end, tags, notes))
        if res.get("file"):               # 内部信息走 stderr，不属于要展示给用户的内容
            print(f"ATTACH_FILE={res['file']}", file=sys.stderr)
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
