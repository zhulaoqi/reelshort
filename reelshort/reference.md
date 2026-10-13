# 接口参考（cps.reelshort.com）

所有请求为 `POST` JSON，业务接口带 `authorization: Bearer <accessToken>`，成功时 `code == 0`。

| 用途 | 路径 | 关键参数 |
|------|------|----------|
| 登录 | `/api/v1/user/login` | `{"channel_account","password","channel_type":"email"}` → `data.accessToken` |
| 短剧列表/搜索 | `/api/v1/book/all-book` | `app:"reelshort"`, `sort:"time"\|"money"`, `language`(可选), `search`(可选), `limit`(≤100 可用), 首页 `page:1`，后续页 `cursor`(上页 `data.next_cursor`，空串=到底) |
| 详情 | `/api/v1/book/book-detail` | `{"app":"reelshort","book_id","book_type"}`，`book_type` 取列表项自带值 |

详情里取：`app_promotion_link`（App推广链接）、`book_promotion_link`（资源推广链接）。**每本剧的两条链接都不同**。

## 收益接口（我的收益 → 短剧预估收益）

三个接口请求体基本一致：`{"app":"reelshort","book_ids":[],"min_date":"YYYY-MM-DD","max_date":"YYYY-MM-DD","promotion_value":"","promotion_type"?,"exist_order"?,"page","limit"}`

| 用途 | 路径 | 返回 |
|------|------|------|
| 明细列表 | `/api/v1/bill/app-estimate` | `data.list[]`（date/book_title/promotion_type/promotion_value/order_cnt/total_revenue/book_id）+ `data.total` |
| 汇总 | `/api/v1/bill/app-estimate-agg` | `data.book_cnt / order_cnt / total_revenue`（无需 page/limit） |
| **下载** | `/api/v1/bill/app-estimate-export` | 直接返回 `.xlsx` 二进制；出错时返回 JSON |

- `promotion_type` 取值：`link_book`(资源推广链接) / `link_app`(App推广链接) / `code`(资源推广口令) / `web_iframe`(内嵌播放器)；不传 = 全部。
- `book_ids` 为空 = 全部剧；`exist_order:true` = 仅含订单数据。
- 导出**忽略** `page/limit`，导出全部符合条件的数据；无数据时也返回一个只有表头的 xlsx（脚本据 `total==0` 不下载）。
- 日期：前端用 dayjs 固定时区 `America/Los_Angeles`，选择器限制近 30 天到昨天；服务端实测不拦截更长区间和“今天”。
- xlsx 两个工作表：汇总明细、订单明细。

## 短链解析

`GET https://reelslink.com/cps/<code>`（不接受 HEAD）返回 `302`，`Location` 形如：

```
https://realshortapp.onelink.me/Zof7?af_xp=custom&pid=cps&deep_link_value=<编码值>&af_dp=cmsvictor%3A%2F%2F
```

不跟随跳转，直接用正则 `(&deep_link_value=[^&\s]*&af_dp=[^&\s]*)` 截取 Location 即可。

## 语言代码

`en es pt th in zh de fr hi tr fil ja ko ru vi bg cs ar zh-TW pl it ro`。传未知语言接口不报错但返回空，所以由脚本先校验。

## 排错

- 退出码 3：账号或密码错误（接口 `code=300007` 用户不存在 / 密码错误），检查 `config/accounts.json`。
- 某部剧显示 `⚠ …解析失败`：短链临时失败，重跑即可；脚本已重试 3 次。
- 接口/字段变动：前端源码可在 `https://cps.reelshort.com/resource-square` 的 `_next/static/chunks/app/[locale]/resource-square/page-*.js` 中查到调用方式。
