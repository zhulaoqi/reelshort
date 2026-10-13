# 接口参考（cps.reelshort.com）

所有请求为 `POST` JSON，业务接口带 `authorization: Bearer <accessToken>`，成功时 `code == 0`。

| 用途 | 路径 | 关键参数 |
|------|------|----------|
| 登录 | `/api/v1/user/login` | `{"channel_account","password","channel_type":"email"}` → `data.accessToken` |
| 短剧列表/搜索 | `/api/v1/book/all-book` | `app:"reelshort"`, `sort:"time"\|"money"`, `language`(可选), `search`(可选), `limit`(≤100 可用), 首页 `page:1`，后续页 `cursor`(上页 `data.next_cursor`，空串=到底) |
| 详情 | `/api/v1/book/book-detail` | `{"app":"reelshort","book_id","book_type"}`，`book_type` 取列表项自带值 |

详情里取：`app_promotion_link`（App推广链接）、`book_promotion_link`（资源推广链接）。**每本剧的两条链接都不同**。

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
