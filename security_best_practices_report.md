# Security and Service Availability Review

Review date: 2026-08-27 (Asia/Shanghai)

## Executive summary

The active `zlib-skill` repository now fails closed around ebook-service domains and reports
actual search capability instead of homepage reachability. The review found one critical
credential-isolation flaw, two high-risk domain-trust flaws, one material availability diagnostic
flaw, and one response-validation flaw. All five are fixed in the working tree and covered by
offline tests.

Live checks from the review network currently show **no usable source**:

- Z-Library's pinned discovery API responds, but every accepted content domain fails its bounded
  self-attesting health probe. A `workers.dev` entry returned by discovery is rejected as shared
  hosting.
- Anna's Archive's three official homepages respond, but all three real search endpoints redirect
  once and then return HTTP 403. `doctor` now reports `status: blocked`,
  `website_reachable: true`, and `can_search: false`.

This is an upstream availability result, not a local test failure. No code change can safely
bypass those access controls.

## Critical

### SEC-001 — Saved Z-Library credentials were loaded during nominally anonymous search (fixed)

Impact: a saved account token could be transmitted while the caller believed the request was
anonymous, magnifying the impact of a poisoned or stale domain decision.

The old `require_auth=False` path still constructed `Zlibrary` with saved tokens, whose constructor
immediately validated them remotely. The anonymous path now constructs a credential-free client
and authenticated callers cannot inject a pre-resolved domain. See
`plugins/zlib-skill/scripts/zlib_anna/engine.py:757` and
`plugins/zlib-skill/scripts/zlib_anna/engine.py:820`.

Verification: `test_anonymous_zlib_client_never_loads_saved_credentials` and
`test_authenticated_zlib_client_cannot_bypass_domain_resolution`.

## High

### SEC-002 — Discovery or cached state could grant credential trust to an arbitrary domain (fixed)

The previous union of discovery responses treated every returned content domain as trusted and
persisted that trust. A single compromised discovery endpoint, stale cache entry, or shared-hosting
proxy could therefore become a credential destination.

Credential operations now require the domain to be present in both the built-in allowlist and the
current pinned discovery response. Dynamically discovered domains remain usable only for
credential-free probing/search; explicit custom trust is per invocation and known fraudulent
domains cannot use the escape hatch. See
`plugins/zlib-skill/scripts/zlib_anna/engine.py:574` and
`plugins/zlib-skill/scripts/zlib_anna/engine.py:750`.

Verification: `test_credential_domain_requires_built_in_and_live_registry_confirmation` and
`test_known_fraudulent_zlib_override_is_never_contacted`.

### SEC-003 — Anna custom origins accepted known fraudulent lookalikes (fixed)

`ANNAS_BASE_URL` previously accepted any public HTTP(S) host. The official Anna FAQ currently lists
`.gl`, `.pk`, and `.gd` as official and labels `.su`, `.io`, and `.is` fraudulent. The new service
policy hard-blocks those fraudulent domains, restricts defaults to the official set, and requires
an explicit development opt-in for any other origin. Paths, queries, fragments, and embedded
credentials are not valid base origins. See
`plugins/zlib-skill/scripts/zlib_anna/source_trust.py:23` and
`plugins/zlib-skill/scripts/zlib_anna/source_trust.py:96`.

Verification: `test_known_fraudulent_anna_domains_remain_blocked_with_opt_in`,
`test_custom_anna_domain_requires_explicit_opt_in`, and
`test_anna_base_url_rejects_paths_and_queries_even_with_opt_in`.

## Medium

### AVAIL-001 — Anna homepage health produced a false-positive search status (fixed)

The previous `doctor` checked only the homepage. During this review the homepage returned HTTP 200
while all search endpoints returned HTTP 403, so the tool incorrectly advertised search as usable.
The probe now targets a benign real search URL and classifies upstream 401/403/429 responses as
`blocked`. See `plugins/zlib-skill/scripts/zlib_anna/engine.py:2072`.

Verification: `test_check_anna_probes_search_capability_and_reports_access_block` plus the dated
live `doctor` run.

### SEC-004 — Domain registry probes parsed unbounded or weakly attested JSON (fixed)

Discovery and health responses now use the existing bounded JSON parser, reject incompatible
content, close streamed responses, filter known-fraud/shared-hosting domains, and require a
candidate endpoint to list itself as available and non-redirecting. See
`plugins/zlib-skill/scripts/zlib_anna/engine.py:473` and
`plugins/zlib-skill/scripts/zlib_anna/engine.py:537`.

## Existing controls confirmed

- Every redirect target is revalidated against local/private/link-local network access policy.
- Downloads are size-bounded, staged in sibling partial files, and atomically committed.
- Anna downloads require the downloaded bytes to match the record MD5.
- Remote metadata is normalized as untrusted data and is not an instruction channel.
- Runtime dependencies are pinned with hashes and installed into an isolated cache-local virtual
  environment.
- Credentials can use a system keychain; file storage is permission-restricted and migration
  conflicts fail closed.

## Residual risks and project boundary

- A signed, auditable Z-Library registry does not yet exist. The new allowlist-plus-live-discovery
  rule intentionally sacrifices some availability until that stronger mechanism is implemented.
- Upstream anti-bot challenges, legal blocks, captchas, account limits, and mirror shutdowns remain
  outside this repository's control.
- PDFs and other ebook formats can themselves contain active content. Hash matching proves file
  identity, not that a document is harmless; users should keep readers patched and avoid enabling
  embedded scripts or external content.
- `../zlib-cli-internal` is a separate legacy Git repository and is not packaged, imported, or
  tested by the active `zlib-skill` release. It retains older trust behavior and should not be
  distributed as the current product.

## External verification references

- Anna's Archive FAQ, “What are your official mirrors?”: https://annas-archive.gl/faq#mirrors
- TorrentFreak, Z-Library copycat warning: https://torrentfreak.com/zlibrary-warns-against-fraudulent-and-unsafe-copycats-with-millions-of-users-230511/
- TorrentFreak, Z-Library scam email campaign: https://torrentfreak.com/z-library-scammers-use-email-campaigns-to-lure-users-and-extract-payments-240327/
