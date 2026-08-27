# Roadmap / 路线图

## 0.3.x

中文：

- 稳定 schema 2 常用字段并记录兼容策略。
- 拆分当前执行引擎为命令协调、来源适配器和下载策略深模块。
- 改进 Anna 验证码、会员页、镜像模式和失败分类。
- 缓存 Z-Library 域名探测，提供可审计、可签名的域名注册表更新；签名注册表完成前，凭据
  操作采用“内置允许列表 + 当前发现确认”的 fail-closed 策略。
- 已完成可选系统 keychain 存储及双向迁移。
- 增加 Windows 与 Linux ARM 的干净 Skill 引导验证。
- 为旧版本化运行缓存提供明确、安全的清理命令。

English:

- Stabilize common schema 2 fields and document compatibility policy.
- Split the engine into command coordination, source adapters, and download-policy modules.
- Improve Anna captcha/member-page detection, mirror patterns, and failure categories.
- Cache Z-Library domain probes and design an auditable, signed registry update path; until that
  registry exists, credential operations fail closed unless both the built-in allowlist and live
  discovery confirm the domain.
- Optional OS-keychain storage and bidirectional migration are complete.
- Add clean Skill-bootstrap verification on Windows and Linux ARM.
- Provide an explicit safe cleanup command for old versioned runtime caches.

## Later / 后续

- 可选 MCP wrapper / Optional MCP wrapper.
- 可恢复、可重试、可导出的批量任务 / Resumable, retryable, exportable batch jobs.
- 更强的作者、语言、年份和格式规范化 / Stronger metadata normalization.
