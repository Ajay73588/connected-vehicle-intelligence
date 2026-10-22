#!/usr/bin/env python
"""Run the locally available static/dependency scanners and write their REAL output to docs/security/scan-results.md.
Tools that need Docker or network services (Trivy image scan, OWASP ZAP, Semgrep registry rules) are listed as 'CI only'."""

import datetime as dt
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(sys.executable).parent


def run(title: str, cmd: list[str], cwd: Path = ROOT, tail: int = 40) -> str:
    exe = shutil.which(cmd[0], path=str(BIN)) or shutil.which(cmd[0])
    if not exe:
        return f"### {title}\n\n`{' '.join(cmd)}` — tool not installed on this machine.\n"
    r = subprocess.run([exe, *cmd[1:]], cwd=cwd, capture_output=True, text=True, timeout=900, shell=False)
    out = (r.stdout + r.stderr).strip().splitlines()
    shown = "\n".join(out[-tail:])
    return f"### {title}\n\n`{' '.join(cmd)}` → exit code **{r.returncode}**\n\n```text\n{shown}\n```\n"


parts = [
    f"# Security scan results (generated {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC by `scripts/security_report.py`)\n",
    "Outputs below are verbatim tool output from the development machine. Scanners that need Docker, a container registry or the running stack "
    "(Trivy image/IaC scan, OWASP ZAP DAST, Semgrep rule packs) are configured in `.github/workflows/ci.yml` and were **not** run locally.\n",
    run("SAST — Bandit (Python), all severities", ["bandit", "-r", "src", "-q", "--exit-zero", "-f", "txt"], tail=60),
    run("SAST — Bandit, medium+ severity gate (CI fails on any finding)", ["bandit", "-r", "src", "-q", "-ll"], tail=10),
    run("Dependency scan — pip-audit (PyPI advisories)", ["pip-audit", "--skip-editable"], tail=10),
    run("Dependency scan — npm audit (frontend)", ["npm", "audit", "--audit-level=high"], cwd=ROOT / "frontend", tail=10),
    run("Lint — ruff", ["ruff", "check", "src", "tests", "scripts", "benchmarks"], tail=10),
    run("Types — mypy", ["mypy", "src/cvi"], tail=6),
    "### CI-only scanners\n\n| scanner | purpose | where |\n|---|---|---|\n| Semgrep (`p/python`, `p/owasp-top-ten`) | SAST rules | `ci.yml` job `security` |\n"
    "| Trivy fs (vuln, secret, misconfig) | repo, IaC, secrets | `ci.yml` jobs `security`, `deploy-validation` |\n"
    "| Trivy image | container CVEs (Python + frontend images) | `ci.yml` job `images` |\n"
    "| OWASP ZAP API scan | DAST against the running API's OpenAPI | `ci.yml` job `e2e`, `make zap` |\n",
]
out = ROOT / "docs" / "security" / "scan-results.md"
out.write_text("\n".join(parts), encoding="utf-8")
print("wrote", out)
