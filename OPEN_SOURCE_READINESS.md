# Open-Source Readiness Review / 开源就绪审查

Review date / 审查日期: 2026-08-06

## 结论 / Verdict

中文：`zlib-skill` 的代码、Skill 结构、自包含运行环境、测试、安全门禁和中英双语
材料达到 `0.3.1` alpha 开源发布标准。仓库保持独立、干净的公开历史，不包含前置私有仓库
历史或维护者凭据。当前没有开源阻塞项。

English: The code, Skill structure, self-contained runtime, tests, security gates, and
bilingual materials meet the open-source release bar for `0.3.1` alpha. The repository keeps
an independent clean public history with no predecessor private history or maintainer
credentials. No open-source blocker remains.

## 干净仓库边界 / Clean-Repository Boundary

- 只保留审查过的 tracked files，不导入私有 `.git`、refs、bundle、缓存、构建产物或本地
  配置 / Keep only audited tracked files; never import private history, refs, bundles, caches,
  build output, or local config.
- 提交使用 GitHub noreply 身份，`origin` 只指向公开仓库 / Commits use a GitHub noreply
  identity and `origin` points only to the public repository.
- 源码与公开历史中未发现真实 Z-Library token、密码或 cookie / No real Z-Library token,
  password, or cookie was found in source or public history.

## 关键问题与处理 / Material Findings and Resolutions

### OSR-001: 双安装面容易半安装 / Two installation surfaces caused partial setup

旧版需要同时注册 Skill 和安装全局命令。`0.2.0` 将执行代码、依赖锁和启动器放入同一 Skill
目录；用户只安装一个 Skill，首次命令自动准备缓存运行环境。

The old release required both Skill registration and a global command. `0.2.0` bundles the
runner, dependency lock, and engine in one Skill; first use prepares a cache-local runtime.

### OSR-002: 依赖引导与代码注入风险 / Runtime bootstrap and code-injection risk

运行依赖固定版本并要求 SHA-256 哈希及 binary distributions。缓存键绑定 Skill、Python 和
锁文件；内部引擎以 Python isolated mode 启动，不信任当前目录或用户 `PYTHONPATH`。

Runtime dependencies are pinned, SHA-256-checked, and binary-only. Cache keys bind the Skill,
Python, and lock file. The engine starts in Python isolated mode without trusting the current
directory or user `PYTHONPATH`.

### OSR-003: 不可信域名可能接收凭据 / Untrusted domains could receive credentials

非内置或非入口发现的 Z-Library 域名默认不访问，必须由用户显式 opt in；Agent 禁止猜测
登录域名。该授权不会持久化。

Z-Library domains outside the built-in or discovered trust set are blocked unless the user
explicitly opts in. The Skill forbids guessed login domains, and trust is not persisted.

### OSR-004: 搜索与下载可能访问内网 / Search and download could reach private networks

Anna 搜索、详情、下载和每次重定向均校验 URL，阻止 localhost、私有/链路本地地址、内嵌
凭据和解析到内网的域名。

Anna search, detail, download, and every redirect validate URLs and block local, private,
link-local, credential-bearing, and private-resolving destinations.

### OSR-005: 下载边界与 Agent 协议 / Download boundaries and Agent contract

下载使用大小上限、已知类型、`.part` 文件和不覆盖策略；Anna 校验 MD5。schema 2 响应包含
`skill_version`、稳定错误码和来源状态；默认错误不回显远端正文、完整私人 URL 或 traceback。

Downloads enforce size, known type, `.part`, and no-overwrite boundaries; Anna verifies MD5.
Schema 2 responses carry `skill_version`, stable errors, and source status. Default failures do
not echo remote text, complete private URLs, or tracebacks.

## 已知剩余风险 / Known Residual Risks

- Anna 依赖不稳定 HTML、第三方镜像和验证码 / Anna relies on unstable HTML, third-party
  mirrors, and captcha behavior.
- Z-Library 与 Anna 域名可能被封锁、吊销或接管；信任集合需要维护 / Domains may be
  blocked, revoked, or taken over; trust data needs maintenance.
- Credentials default to a protected local file and support explicit system-keychain
  migration; unavailable capabilities and conflicting values fail closed.
- 首次运行需要 Python 包索引网络；断网用户无法准备依赖 / First use needs package-index
  access; fully offline users cannot prepare dependencies.
- 默认 CI 不访问真实上游，无法提前发现所有页面变化 / Default CI avoids live services and
  cannot detect every upstream markup change.
- 执行引擎仍较大，后续应拆分来源适配器和下载策略 / The engine remains large and should be
  split into deeper source-adapter and download-policy modules.

这些是 alpha 维护风险，不是当前开源阻塞项。

These are alpha maintenance risks, not current publication blockers.

## 验证证据 / Validation Evidence

- Python 3.9 与 3.14：各 `113 passed`；3.9 仅有 Apple 系统 LibreSSL/urllib3 警告 /
  `113 passed` on Python 3.9 and 3.14; 3.9 emitted only the Apple system LibreSSL warning.
- 3.9 与 3.14 的真实首次引导、无账号 `auth status --json` 和缓存复用通过 / Real first-use
  bootstrap, account-free auth status, and cache reuse passed on 3.9 and 3.14.
- Ruff、format、compileall、Skill validator、YAML 与 Markdown 链接检查通过 / Ruff,
  formatting, compile, Skill, YAML, and Markdown link checks passed.
- `pip-audit` 无已知运行依赖漏洞；Bandit 无 medium/high findings / No known dependency
  vulnerabilities or medium/high Bandit findings.
- tracked files 与新增文件密钥扫描 0 findings / Secret scan reported zero findings.
- 六版本测试矩阵、`package` 自包含安装和 `security` 构成八个受保护 CI checks / Six Python
  tests plus self-contained `package` and `security` jobs form eight protected checks.
- 隔离安装验证不读取账号、不登录、不搜索、不下载 / Isolated install verification reads no
  account and performs no login, search, or download.

## 最终发布门槛 / Final Release Gate

- [x] 历史仅含 noreply 提交且无私人 remote / Noreply-only history and no private remote.
- [x] Skill 名称、目录、Agent 元数据与公开 URL 统一为 `zlib-skill` / Naming aligned.
- [x] 单次安装、首次引导、缓存复用和安全失败测试通过 / Self-contained runtime tests pass.
- [x] Python 3.9-3.14 与八项 GitHub checks 全绿 / Python matrix and all eight checks pass.
- [x] 分支保护、Private Vulnerability Reporting、secret scanning 和 push protection 启用 /
  Repository security settings remain enabled.
- [x] 未登录页面与新公开 URL 干净安装通过 / Logged-out pages and public install pass.
- [x] `v0.3.1` tag 与 Release 使用当前名称 / The tag and release use the current name.
