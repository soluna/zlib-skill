# Troubleshooting

Read this file only after the normal search, login, source check, or download flow fails.

## Runtime Setup

When the runner returns `RUNTIME_SETUP_FAILED`, explain that the Skill could not prepare its
local runtime. Report the safe `details.step` and the provided suggestions. It needs Python 3.9+
and package-index access on first use.

Do not use `sudo`, global `pip`, or `pipx` as a fallback. Do not replace the bundled runner with
an improvised scraper.

## Check Source Availability

Run:

```bash
python3 {baseDir}/scripts/run.py doctor --json
```

Summarize which source is unavailable and what the user can do next. Do not dump the full JSON.
The check probes an actual search endpoint. `status: blocked` with `website_reachable: true`
means the homepage is online but automated search is rejected, commonly by an upstream challenge.

## Login Problems

- `AUTH_REQUIRED`: Z-Library search works anonymously. This error applies to a download or another
  account-only feature; offer login only when the user wants that action.
- `AUTH_INVALID`: explain that the saved login expired or is invalid. Offer `auth logout`, then
  a fresh terminal login.
- Never request or echo the password in chat.

## Source Address Problems

Z-Library domains can change. The runner merges domains from its pinned discovery endpoints and
tries verified alternatives automatically. Anonymous search never loads a saved token. Login,
download, and account operations require a domain to appear in both the built-in allowlist and the
current discovery response. Shared-hosting discovery entries are rejected. Only ask the user for
a manual domain after that pool is exhausted. Use `ZLIBRARY_DOMAIN` only with a value they
independently verified; never take a domain from an arbitrary search result, email, or social post
and send credentials to it. Known fraudulent domains remain blocked even with an opt-in.

For Anna's Archive, the runner automatically tries the official `.gl`, `.pk`, and `.gd` origins.
Its official FAQ identifies `.su`, `.io`, and `.is` as fraudulent; never suggest them. Prefer
`HTTPS_PROXY` or `ALL_PROXY` when all official origins fail. A custom origin is development-only,
must be an origin with no path/query, and requires both `ANNAS_BASE_URL` and the explicit
`ANNAS_ALLOW_UNTRUSTED_DOMAIN=1` opt-in. Known fraudulent domains remain blocked.

Unknown Z-Library domains require the user's explicit approval before
`ZLIBRARY_ALLOW_UNTRUSTED_DOMAIN=1`. Private-network and insecure-HTTP overrides also require
explicit approval and are only appropriate in a controlled environment.

## Failed Downloads

For a failed Anna download, explain that automatic download is best-effort. Report the stable
error code, the detail page when safe, available link kinds, and failed attempts. Captchas,
member-only pages, dead mirrors, and network blocking can prevent a download.

For every source, distinguish these outcomes clearly:

- Found: a matching edition exists.
- Download attempted: the Skill tried one or more links.
- Downloaded: a verified local file was saved.

Only the last outcome is a completed download.
