# zlib-skill

> 想找一本电子书，不该先研究网站入口、镜像和下载按钮。

只要告诉 Agent 书名、作者，或者你记得的一点线索。`zlib-skill` 会帮你查找
Z-Library 和 Anna's Archive，整理出容易比较的版本，再按你的选择下载。

## 它能帮你做什么

- 按书名、作者、ISBN 或关键词找书。
- 把语言、格式、年份和来源整理清楚，方便比较不同版本。
- 记不清完整书名时，先给出最可能的结果供你确认。
- 选定版本后下载到本地，并告诉你文件保存在哪里。
- 无账号也会同时搜索 Z-Library 与 Anna's Archive；下载 Z-Library 结果时才可能需要登录。
- 已知地址失效时，自动切换经过验证的 Z-Library 域名与 Anna 官方备用地址。

搜索和下载是两步。Skill 会先让你看候选结果，不会因为搜到了书就直接下载。

## 快速安装

把这句话发给你的 Agent：

> 请帮我安装这个 Agent Skill：https://github.com/soluna/zlib-skill

Agent 会读取仓库并安装 `zlib-skill`。安装本身不会登录账号、搜索或下载电子书。

Agent 不支持直接安装时，请看[安装与迁移说明](INSTALL.md)。

## 装好后这样用

直接说你想做什么，不需要记命令：

```text
帮我找《百年孤独》的中文 EPUB，先把不同版本列出来。

找一下作者是 Robert C. Martin 的 Clean Code，优先英文版。

我只记得书名里有“设计心理学”，帮我看看可能是哪几本。

下载第 2 本到我的 Downloads 目录。

帮我看看为什么现在搜不到书。
```

如果多个版本都合理，Agent 会请你选择。你已经明确说了要下载哪一本时，它会直接继续。

## 没有 Z-Library 账号

没有账号也能使用。Skill 会匿名搜索 Z-Library，同时搜索 Anna's Archive；搜索本身不要求
Z-Library 账号。只有下载选定的 Z-Library 结果或使用账号功能时，才可能需要登录。

只有当你明确要求下载 Z-Library 结果时，Agent 才会按需引导你登录。登录在你自己的终端中
完成，密码不会要求你发到聊天里。

## 下载不一定每次都成功

Z-Library 的地址可能变化，Anna's Archive 也可能遇到失效镜像、验证码、会员页面或网络
限制。遇到这些情况，Agent 会说明卡在哪一步，并给出下一步建议，不会把“找到书”冒充成
“已经下载”。

Skill 会从 Z-Library 的域名发现接口合并可用地址，并在请求失败时继续尝试下一地址；Anna's
Archive 默认轮换其官方列出的 `.gl`、`.pk`、`.gd` 地址。自定义地址仍需由用户独立核验。

## 隐私与使用边界

- 安装和无账号搜索不会读取维护者账号。
- 登录信息只保存在用户本机，不应提交到仓库或 Issue。
- 不要在聊天、Issue 或日志中发送密码、token、cookie 或私人下载链接。
- 只下载你有权访问和使用的内容。

## 文档

- [安装、更新与旧版本迁移](INSTALL.md)
- [常见问题与支持](SUPPORT.md)
- [安全政策](SECURITY.md)
- [变更记录](CHANGELOG.md)
- [贡献指南](CONTRIBUTING.md)

---

## English

> Finding an ebook should not begin with figuring out mirrors, domains, and download buttons.

Tell your agent the title, author, or whatever detail you remember. `zlib-skill` searches
Z-Library and Anna's Archive, turns the results into clear choices, and downloads the edition
you select.

### What it can do

- Search by title, author, ISBN, or keywords.
- Compare language, format, year, and source across editions.
- Suggest likely matches when you do not remember the exact title.
- Download the edition you choose and report where the file was saved.
- Search both Z-Library and Anna's Archive without a Z-Library account; login is only needed for
  downloads or account-only features.
- Rotate through verified Z-Library domains and official Anna's Archive alternatives when an
  address fails.

Search and download are separate steps. Finding a book never starts a download by itself.

### Quick install

Send this to your agent:

> Please install this Agent Skill: https://github.com/soluna/zlib-skill

The agent reads the repository and installs `zlib-skill`. Installation does not log in, search,
or download anything. See [Installation and Migration](INSTALL.md) when direct installation is
not supported.

### Try these requests

```text
Find an English EPUB of Clean Code by Robert C. Martin and show me the editions first.

I only remember that the title contains “design psychology.” Show me the likely books.

Download result 2 to my Downloads folder.

Check why book search is not working right now.
```

The agent asks you to choose when several editions are plausible. It continues directly when
your download choice is already clear.

### No Z-Library account

You can still use the Skill. It searches Z-Library anonymously alongside Anna's Archive; search
itself does not require a Z-Library account. The agent only guides you through login when you
explicitly ask to download a Z-Library result or use an account-only feature. Login happens in
your terminal; never send your password in chat.

### Downloads can fail

Z-Library domains can change. Anna's Archive may encounter dead mirrors, captchas, member-only
pages, or network blocking. The agent explains where the attempt stopped and what you can do
next. It never reports a search result as a completed download.

The Skill merges domains returned by Z-Library's discovery endpoints and retries the next domain
when a request fails. For Anna's Archive it rotates through the officially listed `.gl`, `.pk`,
and `.gd` addresses. User-supplied mirrors still need independent verification.

### Privacy and responsible use

- Installation and account-free search do not use the maintainer's account.
- Login data stays on the user's machine and must not be posted to the repository or an Issue.
- Never share passwords, tokens, cookies, or private download links in chat or logs.
- Download only material you are authorized to access and use.

### Documentation

- [Installation, updates, and migration](INSTALL.md)
- [Support](SUPPORT.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)

`zlib-skill` is available under the [MIT License](LICENSE). Third-party attribution is listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
## Operational behavior

Search uses deterministic source-local failover and groups duplicate editions while
preserving downloadable alternatives. Every remote command accepts a total
`--deadline-seconds` budget; `doctor --json` distinguishes healthy, degraded, and
unavailable sources. Installable plugin metadata lives under `plugins/zlib-skill`.
