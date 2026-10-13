---
name: reelshort
description: Fetches ReelShort CPS (cps.reelshort.com) promotion links. Logs in with a configured account, searches the 短剧 tab by title, language, sort order and count, then returns each drama's App推广链接 and 资源推广链接 (the deep_link_value part). Use when the user mentions ReelShort, CPS, 资源广场, 短剧推广链接, App推广链接, 资源推广链接, or asks for promotion links of a drama by account email.
---

# ReelShort 推广链接获取

输入：账号 + 可选的 剧名 / 语言 / 排序 / 个数。输出：按剧名分开的 App推广链接、资源推广链接。

## 工作流

1. **确定账号**。用户给的邮箱 / 前缀 / 别名 / 公司名都传给 `--account`，由脚本去配置里模糊命中。
   - 没给账号：先问用户用哪个账号（可用 `--list-accounts` 列出，不含密码）。
   - 脚本退出码 `2` = 需要用户澄清（账号命中多个、语言无法识别等），把 stderr 里的提示原样转述并让用户选，**不要自己猜**。
2. **提取检索条件**（全部可选，可组合）：
   - 剧名 → `--title`（可重复，或逗号分隔）
   - 语言 → `--lang`（口语即可：日文/日语/日本/jp/繁体/台湾…，可多个）
   - 排序 → `--sort`（`time` 最新添加，默认；`money` 近30天充值金额高→低。口语：最新/最近→time，热门/收益/充值/排行→money）
   - 个数 → `--count`（默认 10，上限 200；多语言/多剧名时每个组合各取这么多）
3. **运行脚本**（在 skill 目录下，或用绝对路径）：

```bash
python3 scripts/reelshort.py --account <账号> [--title 剧名] [--lang 语言] [--sort time|money] [--count N] [--offset K]
```

4. **原样输出脚本结果**（脚本输出的就是 Markdown 卡片，直接作为回复正文，**不要再包进代码块**，也不加解释、不改动卡片内容）。

## 超长截断与分批（脚本自动处理）

- 单次最多展示 15 部（`config/aliases.json` 的 `page_size`，`--page-size` 可调，上限 30），另有 16000 字符兜底；超出的截断，末尾提示“共 N 部，本次展示第 a–b 部，还剩 M 部（offset=b）”。
- 用户说“继续 / 下一批 / 更多”时，**沿用上一次完全相同的参数**，只加 `--offset <提示里的 offset>`，再原样输出。
- 详情和短链只对本批展示的剧去请求，所以 `--count 200` 也不会一次拉几百条。

## 多个账号

一个账号一次调用，**逐个账号依次运行、依次回复**（每个账号的结果单独成段，先完成的先返回），不要合并成一张表。用户说“全部账号”时先 `--list-accounts` 再逐个跑。

## 输出格式（脚本已按此输出，渲染后每部剧是一张卡片）

~~~markdown
## 👤 jenhu@isummerads.com · 共 1 部

---

**1. 千億の御曹司と極秘結婚した愛妻**　`日语`
```
# App推广链接
https://reelslink.com/cps/GjGMAS
# App截取信息结果
&deep_link_value=...&af_dp=cmsvictor%3A%2F%2F
# 资源推广链接
https://reelslink.com/cps/RbwC00
# 资源截取信息结果
&deep_link_value=...&af_dp=cmsvictor%3A%2F%2F
```
~~~

- 一部剧 = 一个标题行（加粗剧名 + 语言标签）+ **一个整体代码块**，标签是代码块里的 `#` 注释行，便于整块一键复制。
- 每部剧各有自己的两组链接（不同剧的链接不同，不可复用）。“推广链接”= 接口返回的 `reelslink.com` 短链；“截取信息结果”= 访问该短链后 302 跳转地址中 `&deep_link_value=…&af_dp=…` 的整段，两者一一对应。
- 某条失败时该剧卡片末尾会多一行 `> ⚠ 原因`，其余照常输出；无结果时输出 `> 未找到符合条件的短剧。`

## 模糊输入处理

- **账号**：精确邮箱 > 邮箱前缀 > 别名 > 子串（≥3 字符）。多命中（如 `cloudmobi` 同时是两个账号）→ 退出码 2，让用户选。
- **语言**：`config/aliases.json` 的 `languages` 做归类，取“被输入包含的最长别名”（“繁体中文”不会误判成“中文”）。认不出 → 退出码 2 并列出可选语言。
- **排序**：同上，`sorts` 归类；没说就用 `time`。
- **剧名**：服务端关键词搜索，且是按该语言的剧名匹配。结果为空时脚本会自动去标点重试一次；仍为空则告诉用户“没找到”，并建议缩短关键词、换语言或确认原名，不要编造链接。
- 用户说“XX语的剧”但没给剧名 → 就是只按语言 + 排序 + 个数取。
- 用户的新说法没被识别 → 往 `config/aliases.json` 对应 `aliases` 里加一个词即可。

## 配置维护

- 账号：`config/accounts.json`（`email` / `password` / `aliases`）。新增账号按同结构追加；公司名等多账号共有的词**不要**放进 aliases，保留歧义确认。
- 该文件含明文密码，已通过 `.gitignore` 排除；模板见 `config/accounts.example.json`。

## 退出码

`0` 成功 · `1` 网络/接口错误（已自动重试 3 次） · `2` 需要用户澄清 · `3` 登录失败（账号或密码有误，转述 stderr）

接口细节与排错见 [reference.md](reference.md)。
