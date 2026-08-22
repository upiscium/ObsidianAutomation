# Disposable canonical E2E root

Production orchestration E2E must not temporarily grant Validator or Executor access to `98-System`.

Use an existing disposable subdirectory under the canonical root instead:

```text
11-Knowledge/.ai-e2e/
```

Create this directory through the normal Vault owner authority before the E2E and pull it into the local mirror. The default ACL inherited from `11-Knowledge` gives Validator and Executor read/traverse only, while Sync remains the only local mirror writer.

For E2E only, pass `--allowed-root 11-Knowledge/.ai-e2e`. Production service policy remains `11-Knowledge` and is not widened to `98-System`.

After the test, delete the remote E2E note through the normal Vault owner authority and refresh the pull-only mirror.
