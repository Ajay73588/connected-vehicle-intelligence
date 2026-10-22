# Security scan results (generated 2026-10-01 07:22 UTC by `scripts/security_report.py`)

Outputs below are verbatim tool output from the development machine. Scanners that need Docker, a container registry or the running stack (Trivy image/IaC scan, OWASP ZAP DAST, Semgrep rule packs) are configured in `.github/workflows/ci.yml` and were **not** run locally.

### SAST — Bandit (Python), all severities

`bandit -r src -q --exit-zero -f txt` → exit code **0**

```text
	Total lines of code: 5162
	Total lines skipped (#nosec): 0
	Total potential issues skipped due to specifically being disabled (e.g., #nosec BXXX): 5

Run metrics:
	Total issues (by severity):
		Undefined: 0
		Low: 10
		Medium: 0
		High: 0
	Total issues (by confidence):
		Undefined: 0
		Low: 0
		Medium: 1
		High: 9
Files skipped (0):
[manager]	WARNING	Test in comment: fixed is not a test name or id, ignoring
[manager]	WARNING	Test in comment: fragments is not a test name or id, ignoring
[manager]	WARNING	Test in comment: values is not a test name or id, ignoring
[manager]	WARNING	Test in comment: bound is not a test name or id, ignoring
[tester]	WARNING	nosec encountered (B608), but no failed test on file src\cvi\agent\tools.py:154
[tester]	WARNING	nosec encountered (B608), but no failed test on file src\cvi\agent\tools.py:154
[manager]	WARNING	Test in comment: container is not a test name or id, ignoring
[manager]	WARNING	Test in comment: entrypoint is not a test name or id, ignoring
[manager]	WARNING	Test in comment: exposure is not a test name or id, ignoring
[manager]	WARNING	Test in comment: is is not a test name or id, ignoring
[manager]	WARNING	Test in comment: controlled is not a test name or id, ignoring
[manager]	WARNING	Test in comment: by is not a test name or id, ignoring
[manager]	WARNING	Test in comment: the is not a test name or id, ignoring
[manager]	WARNING	Test in comment: orchestrator is not a test name or id, ignoring
[manager]	WARNING	Test in comment: network is not a test name or id, ignoring
[manager]	WARNING	Test in comment: policy is not a test name or id, ignoring
[manager]	WARNING	Test in comment: HTTP is not a test name or id, ignoring
[manager]	WARNING	Test in comment: auth is not a test name or id, ignoring
[manager]	WARNING	Test in comment: scheme is not a test name or id, ignoring
[manager]	WARNING	Test in comment: name is not a test name or id, ignoring
[manager]	WARNING	Test in comment: not is not a test name or id, ignoring
[manager]	WARNING	Test in comment: a is not a test name or id, ignoring
[manager]	WARNING	Test in comment: credential is not a test name or id, ignoring
[tester]	WARNING	nosec encountered (B105), but no failed test on file src\cvi\api\security.py:81
[manager]	WARNING	Test in comment: container is not a test name or id, ignoring
[manager]	WARNING	Test in comment: entrypoint is not a test name or id, ignoring
[manager]	WARNING	Test in comment: exposure is not a test name or id, ignoring
[manager]	WARNING	Test in comment: is is not a test name or id, ignoring
[manager]	WARNING	Test in comment: controlled is not a test name or id, ignoring
[manager]	WARNING	Test in comment: by is not a test name or id, ignoring
[manager]	WARNING	Test in comment: the is not a test name or id, ignoring
[manager]	WARNING	Test in comment: orchestrator is not a test name or id, ignoring
[manager]	WARNING	Test in comment: network is not a test name or id, ignoring
[manager]	WARNING	Test in comment: policy is not a test name or id, ignoring
[manager]	WARNING	Test in comment: table is not a test name or id, ignoring
[manager]	WARNING	Test in comment: names is not a test name or id, ignoring
[manager]	WARNING	Test in comment: are is not a test name or id, ignoring
[manager]	WARNING	Test in comment: constants is not a test name or id, ignoring
[manager]	WARNING	Test in comment: table is not a test name or id, ignoring
[manager]	WARNING	Test in comment: names is not a test name or id, ignoring
[manager]	WARNING	Test in comment: are is not a test name or id, ignoring
[manager]	WARNING	Test in comment: constants is not a test name or id, ignoring
[tester]	WARNING	nosec encountered (B608), but no failed test on file src\cvi\lifecycle\erasure.py:55
[tester]	WARNING	nosec encountered (B608), but no failed test on file src\cvi\lifecycle\erasure.py:57
```

### SAST — Bandit, medium+ severity gate (CI fails on any finding)

`bandit -r src -q -ll` → exit code **0**

```text
[manager]	WARNING	Test in comment: table is not a test name or id, ignoring
[manager]	WARNING	Test in comment: names is not a test name or id, ignoring
[manager]	WARNING	Test in comment: are is not a test name or id, ignoring
[manager]	WARNING	Test in comment: constants is not a test name or id, ignoring
[manager]	WARNING	Test in comment: table is not a test name or id, ignoring
[manager]	WARNING	Test in comment: names is not a test name or id, ignoring
[manager]	WARNING	Test in comment: are is not a test name or id, ignoring
[manager]	WARNING	Test in comment: constants is not a test name or id, ignoring
[tester]	WARNING	nosec encountered (B608), but no failed test on file src\cvi\lifecycle\erasure.py:55
[tester]	WARNING	nosec encountered (B608), but no failed test on file src\cvi\lifecycle\erasure.py:57
```

### Dependency scan — pip-audit (PyPI advisories)

`pip-audit --skip-editable` → exit code **0**

```text
Name Skip Reason
---- -------------------------------
cvi  distribution marked as editable
No known vulnerabilities found
```

### Dependency scan — npm audit (frontend)

`npm audit --audit-level=high` → exit code **0**

```text
found 0 vulnerabilities
```

### Lint — ruff

`ruff check src tests scripts benchmarks` → exit code **0**

```text
All checks passed!
```

### Types — mypy

`mypy src/cvi` → exit code **0**

```text
src\cvi\simulator\sinks.py:19: note: By default the bodies of untyped functions are not checked, consider using --check-untyped-defs  [annotation-unchecked]
src\cvi\api\middleware.py:34: note: By default the bodies of untyped functions are not checked, consider using --check-untyped-defs  [annotation-unchecked]
src\cvi\agent\tools.py:166: note: By default the bodies of untyped functions are not checked, consider using --check-untyped-defs  [annotation-unchecked]
src\cvi\agent\tools.py:171: note: By default the bodies of untyped functions are not checked, consider using --check-untyped-defs  [annotation-unchecked]
Success: no issues found in 76 source files
```

### CI-only scanners

| scanner | purpose | where |
|---|---|---|
| Semgrep (`p/python`, `p/owasp-top-ten`) | SAST rules | `ci.yml` job `security` |
| Trivy fs (vuln, secret, misconfig) | repo, IaC, secrets | `ci.yml` jobs `security`, `deploy-validation` |
| Trivy image | container CVEs (Python + frontend images) | `ci.yml` job `images` |
| OWASP ZAP API scan | DAST against the running API's OpenAPI | `ci.yml` job `e2e`, `make zap` |
