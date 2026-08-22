# Validator Vault root access requirement

`validate_create_note()` performs case-fold collision checks on every parent directory component before allowing a canonical create. That requires directory listing permission on the Vault root in addition to traversal permission.

Production ACLs therefore grant `r-x` on the Vault root only to the deterministic Validator and Executor identities. Reader remains traverse-only, and Generator/Reviewer retain no direct canonical Vault access.

This does not grant write permission. `11-Knowledge` remains read-only to Validator/Executor; canonical remote writes are performed only by Sync Transport through conditional WebDAV create.

Disposable orchestration E2E should use an existing test subdirectory under the canonical root, for example `11-Knowledge/.ai-e2e`, rather than broadening production ACLs to `98-System`.
