# Connections contract — bounded first slice of #301

**Experimental, not feature-complete, accepted, or deployed.** This branch adds
only a typed non-secret registry of four provider IDs (`nextcloud`, `gitea`,
`github`, `ai`), pure input syntax validators, and a read-only dashboard
`GET /api/v1/connections` response. No live credentials, secret files, private
config, environment variables, or remote provider state are consulted. The
response has `version: 1`, `authority: observation_only`, and a fixed list of
`name`, `provider`, `configured: false`, `verification: unknown`. `false` means
**not configured by this registry**, not a claim that existing private service
configuration is missing. `unknown` is never a claim of a verified connection.

Validation is pure and not exposed as a write API. Provider IDs are exact and
allowlisted; names are bounded (1–64 ASCII letters, digits, dots, underscores,
hyphens; alphanumeric ends, no `..`). Endpoint URL syntax requires lowercase
`https://`, a bounded ASCII DNS hostname with valid labels (private hostnames
are allowed), optional valid port and bounded path. Auth/userinfo, query,
fragment, whitespace/control characters, IP literals, malformed encoding,
invalid or overlong hosts are rejected. **Syntax validation is not SSRF
protection**: any later connection/probe feature must separately review DNS,
redirects, address pinning, permissions and timeout policy before network use.

No probe, outbound traffic, secret read/write, configured-state detection,
POST endpoint, form, credential management, migration, or service mutation is
implemented. Existing loopback bind, exact Host/Origin checks, no-store/CSP,
and 405 responses for POST/PUT/PATCH/DELETE/OPTIONS remain in force. The
larger issue's secret handling, verification and transactional update
acceptance criteria remain open and require separate review and authorization.
