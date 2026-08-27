# Security Policy / 安全政策

## 支持版本 / Supported Versions

中文：当前仅维护最新的 `0.2.x` alpha。安全修复可能包含不向后兼容的行为收紧。

English: Only the latest `0.2.x` alpha is maintained. Security fixes may tighten behavior
without backward compatibility.

## 私密报告 / Private Reporting

不要在公开 Issue 中发布漏洞细节、真实账号、token、cookie、配置、私人下载 URL、traceback
或个人信息。请使用 GitHub
[私密漏洞报告](https://github.com/soluna/zlib-skill/security/advisories/new)，将敏感值
替换为 `<redacted>` 并提供最小复现。

Do not post vulnerability details, real accounts, tokens, cookies, config data, private
download URLs, tracebacks, or personal information in public issues. Use
[GitHub private vulnerability reporting](https://github.com/soluna/zlib-skill/security/advisories/new),
replace sensitive values with `<redacted>`, and include the smallest reproduction.

## 报告内容 / What to Include

- 受影响版本、Agent 运行时、Python 和操作系统 / Version, agent runtime, Python, and OS.
- 不含真实凭据的复现步骤 / Reproduction without real credentials.
- 影响：凭据泄露、任意文件写入、内网访问、依赖引导劫持等 / Impact such as credential
  disclosure, arbitrary writes, private-network access, or runtime-bootstrap compromise.
- 建议修复（如有）/ A suggested fix, if available.

维护者目标是在 7 天内确认收到。Alpha 不承诺固定 SLA，但优先处理凭据泄露、任意写入、网络
边界绕过和依赖供应链问题。

The maintainer aims to acknowledge reports within seven days. This alpha has no fixed SLA,
but credential disclosure, arbitrary writes, network-boundary bypasses, and dependency
supply-chain issues receive priority.

## 安全模型 / Security Model

- 首次运行只在用户缓存创建专用虚拟环境，不使用 `sudo`、全局 pip 或系统 Python写入 /
  First use creates a cache-local virtual environment without sudo, global pip, or system
  Python writes.
- 运行依赖固定版本并要求 SHA-256 哈希；只接受 binary distributions / Runtime dependencies
  are pinned, hash-checked, and binary-only.
- 引擎使用 Python isolated mode 启动，避免当前目录或用户 `PYTHONPATH` 注入 / The engine
  starts in Python isolated mode to avoid current-directory and user-PYTHONPATH injection.
- 引导失败只返回步骤和异常类型，不回显包索引 URL 或凭据 / Setup failures report only a
  step and error type, not package-index URLs or credentials.
- Z-Library token 默认保存在本机 JSON（POSIX `0700`/`0600`），也可显式迁移到系统
  keychain / Z-Library tokens default to local JSON protected by POSIX `0700`/`0600` and can
  be explicitly migrated to the system keychain.
- 未信任 Z-Library 域名默认不访问或接收凭据 / Untrusted Z-Library domains are not
  contacted or sent credentials by default.
- Z-Library 匿名搜索不会加载或验证本机已保存 token；凭据域名必须同时命中内置允许列表和
  当前固定发现接口 / Anonymous Z-Library search never loads or validates saved tokens;
  credential destinations require both the built-in allowlist and current pinned discovery.
- 已知 Z-Library 仿冒站与共享托管子域不会从发现结果获得信任 / Known Z-Library
  impersonators and shared-hosting subdomains cannot gain trust from discovery responses.
- Anna 默认只接受官方 `.gl`、`.pk`、`.gd`；官方 FAQ 标记的 `.su`、`.io`、`.is` 欺诈
  域名始终硬阻断 / Anna defaults to the official `.gl`, `.pk`, and `.gd` origins; the
  `.su`, `.io`, and `.is` domains identified as fraudulent by the official FAQ are always blocked.
- Anna 下载验证每次重定向并阻止本地、私有和链路本地目标 / Anna downloads validate
  every redirect and block local, private, and link-local targets.
- 对可信内置来源，允许代理软件使用的 `198.18.0.0/15` fake-IP；普通私网、字面 IP、陌生
  主机及未获信任的重定向仍被阻止 / Trusted built-in sources may use proxy fake IPs from
  `198.18.0.0/15`; ordinary private networks, literal IPs, unknown hosts, and untrusted redirects
  remain blocked.
- 下载有大小、类型、临时文件边界；Anna 还验证 MD5 / Downloads enforce size, type, and
  partial-file boundaries; Anna also verifies MD5.
- 书名、作者和远端元数据是不可信输入，Agent 不应执行其中指令 / Remote metadata is
  untrusted and must never be executed as instructions.
- `ZLIB_SKILL_DEBUG=1` 可能输出敏感 URL 或路径 / Debug tracebacks may expose sensitive URLs
  or paths.

## 不属于漏洞 / Out of Scope

- 上游不可用、验证码、页面改版或镜像失效 / Upstream outages, captchas, markup changes,
  or dead mirrors.
- 用户明确启用 `ZLIB_ANNA_ALLOW_PRIVATE_NETWORK=1` 或
  `ZLIBRARY_ALLOW_UNTRUSTED_DOMAIN=1`、`ANNAS_ALLOW_UNTRUSTED_DOMAIN=1` 后的预期访问（已知
  欺诈域名仍不会放行）/ Access explicitly enabled through private-network or
  untrusted-domain opt-ins (known fraudulent domains remain blocked).
- 用户主动公开下载内容或链接 / A user intentionally sharing downloads or links.
## Current safeguards

Remote metadata is bounded and treated as untrusted input. Responses with invalid
content types, malformed JSON, oversized bodies, unsafe URLs, or invalid identifiers
are rejected. Downloads use a unique sibling temporary file, destination locking,
checksum/size validation, and one atomic same-directory commit.

Credentials remain opt-in for the system keychain; capability failures and conflicting
file/keychain values fail closed without silently writing plaintext tokens.
