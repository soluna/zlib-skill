# Support / 支持

## 获取帮助 / Getting Help

先运行 Skill 自带诊断，并阅读 README 的“域名变化与网络边界”：

Run the bundled diagnosis first and read the README section on domains and network boundaries.
The diagnosis probes actual search capability, not merely whether a homepage returns HTTP 200:

```bash
python3 {baseDir}/scripts/run.py doctor --json
```

确认是可复现项目问题后，使用 GitHub Bug Report 模板。提交前删除或替换：

After confirming a reproducible project problem, use the GitHub bug template. Remove or replace:

- 真实邮箱、账号、token、cookie 和密码 / Real emails, accounts, tokens, cookies, passwords.
- 配置内容和可识别用户的本地路径 / Config contents and identifying local paths.
- 带 token、key 或私人参数的 URL / URLs containing tokens, keys, or private parameters.
- 下载的电子书及内容 / Downloaded ebooks and their contents.

## 支持范围 / Support Scope

项目处理 Skill 安装、运行环境引导、解析回归、JSON 协议、来源适配、安全边界和文档问题。
上游封锁、账号限制、验证码和镜像下线不由本项目控制，但诊断不清楚仍可反馈。

The project can address Skill installation, runtime bootstrap, parser regressions, JSON
contracts, source adapters, safety boundaries, and documentation. Upstream blocking, account
restrictions, captchas, and mirror shutdowns are outside project control, but unclear
diagnostics remain valid project issues.

安全问题不要走普通 Issue，按 [SECURITY.md](SECURITY.md) 私密报告。

Do not use a normal issue for vulnerabilities; follow [SECURITY.md](SECURITY.md).
