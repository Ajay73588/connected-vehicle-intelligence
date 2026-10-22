"""Static validation of deployment artefacts (Docker Compose, Kubernetes, Terraform, observability, CI). These checks do not
replace `docker compose up`, `kubectl apply --dry-run` or `terraform validate` (they run in CI) but catch drift and typos early."""

import glob
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_all(pattern: str):
    for f in sorted(glob.glob(str(ROOT / pattern), recursive=True)):
        with open(f, encoding="utf-8") as fh:
            yield f, [d for d in yaml.safe_load_all(fh) if d]


# ---------------------------------------------------------------- compose
def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_compose_services_have_healthchecks_and_dependencies_exist():
    c = compose()
    svc = c["services"]
    assert {"postgres", "redis", "mongo", "minio", "kafka", "api", "ingestion-edge", "validator", "stream-processor", "lifecycle", "simulator",
            "frontend", "prometheus", "grafana", "init"} <= set(svc)
    for name in ("postgres", "redis", "mongo", "minio", "kafka", "api"):
        assert "healthcheck" in svc[name], name
    for name, s in svc.items():
        deps = s.get("depends_on", {})
        for d in deps if isinstance(deps, (list, dict)) else []:
            assert d in svc, f"{name} depends on unknown service {d}"
    assert svc["init"]["restart"] == "no"
    assert svc["api"]["depends_on"]["init"]["condition"] == "service_completed_successfully"
    assert svc["simulator"]["profiles"] == ["sim"]


def test_compose_variables_are_declared_in_env_example():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_keys = {ln.split("=")[0] for ln in (ROOT / ".env.example").read_text().splitlines() if "=" in ln and not ln.startswith("#")}
    used = set(re.findall(r"\$\{([A-Z_]+)(?::-[^}]*)?\}", text))
    missing = used - env_keys
    assert not missing, f"variables used by docker-compose.yml but missing from .env.example: {missing}"


def test_env_example_contains_no_real_secret_shapes():
    text = (ROOT / ".env.example").read_text()
    assert not re.search(r"AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|-----BEGIN", text)
    assert "ANTHROPIC_API_KEY=\n" in text  # optional key is empty by default


def test_no_secrets_committed_in_repository_text_files():
    pat = re.compile(r"AKIA[0-9A-Z]{16}|-----BEGIN (RSA |EC )?PRIVATE KEY-----|xox[bp]-[0-9A-Za-z-]{10,}")
    for f in list((ROOT / "src").rglob("*.py")) + list((ROOT / "infrastructure").rglob("*")) + list((ROOT / "scripts").rglob("*.py")):
        if f.is_file() and f.suffix in {".py", ".yml", ".yaml", ".tf", ".conf", ".json", ""}:
            assert not pat.search(f.read_text(encoding="utf-8", errors="ignore")), f


# ---------------------------------------------------------------- kubernetes
def k8s_docs():
    for f, docs in load_all("infrastructure/kubernetes/base/*.yaml"):
        for d in docs:
            if d.get("kind") != "Kustomization":
                yield f, d


def test_every_workload_has_probes_limits_and_hardened_security_context():
    seen = 0
    for f, d in k8s_docs():
        if d["kind"] not in ("Deployment", "StatefulSet"):
            continue
        pod = d["spec"]["template"]["spec"]
        assert pod["securityContext"]["runAsNonRoot"] is True, f
        for c in pod["containers"]:
            seen += 1
            assert c["resources"]["limits"]["memory"] and c["resources"]["requests"]["cpu"], (f, c["name"])
            assert c["securityContext"]["allowPrivilegeEscalation"] is False, (f, c["name"])
            assert c["securityContext"]["capabilities"]["drop"] == ["ALL"], (f, c["name"])
            assert "readinessProbe" in c and "livenessProbe" in c, (f, c["name"])
    assert seen >= 6


def test_k8s_services_match_deployments_and_hpa_targets_exist():
    kinds: dict[str, set[str]] = {}
    for _, d in k8s_docs():
        kinds.setdefault(d["kind"], set()).add(d["metadata"]["name"])
    assert kinds["Service"] <= kinds["Deployment"]
    for _, d in k8s_docs():
        if d["kind"] == "HorizontalPodAutoscaler":
            assert d["spec"]["scaleTargetRef"]["name"] in kinds["Deployment"]
            assert d["spec"]["minReplicas"] <= d["spec"]["maxReplicas"]
    stream = next(d for _, d in k8s_docs() if d["kind"] == "HorizontalPodAutoscaler" and d["metadata"]["name"] == "stream-processor")
    cfg = next(d for _, d in k8s_docs() if d["kind"] == "ConfigMap")
    assert stream["spec"]["maxReplicas"] <= int(cfg["data"]["TOPIC_PARTITIONS"])  # replicas beyond partitions would idle
    assert {"Ingress", "NetworkPolicy", "PodDisruptionBudget", "ConfigMap", "Secret", "Job"} <= set(kinds)


def test_overlays_reference_existing_resources():
    for overlay in ("dev", "aws"):
        k = yaml.safe_load((ROOT / f"infrastructure/kubernetes/overlays/{overlay}/kustomization.yaml").read_text())
        for r in k["resources"]:
            assert (ROOT / f"infrastructure/kubernetes/overlays/{overlay}" / r).exists() or (ROOT / f"infrastructure/kubernetes/overlays/{overlay}" / r).is_dir(), r


def test_committed_secret_is_only_a_placeholder():
    sec = next(d for _, d in k8s_docs() if d["kind"] == "Secret")
    assert all("CHANGE_ME" in v for v in sec["stringData"].values())


# ---------------------------------------------------------------- terraform / observability / ci
def test_terraform_files_parse_and_cover_required_building_blocks():
    hcl2 = pytest.importorskip("hcl2")
    resources: set[str] = set()
    for f in (ROOT / "infrastructure/terraform/aws").glob("*.tf"):
        with open(f, encoding="utf-8") as fh:
            doc = hcl2.load(fh)
        for r in doc.get("resource", []):
            resources.update(k.strip('"') for k in r)
    needed = {"aws_db_instance", "aws_elasticache_replication_group", "aws_msk_cluster", "aws_docdb_cluster", "aws_s3_bucket", "aws_s3_bucket_lifecycle_configuration",
              "aws_secretsmanager_secret", "aws_iam_role", "aws_kms_key", "aws_security_group", "aws_cloudwatch_metric_alarm", "helm_release"}
    assert needed <= resources, needed - resources


def test_grafana_dashboard_and_prometheus_config_are_consistent_with_metrics():
    dash = json.loads((ROOT / "infrastructure/observability/grafana/dashboards/cvi-overview.json").read_text())
    exprs = " ".join(t["expr"] for p in dash["panels"] for t in p["targets"])
    for metric in ("events_received_total", "consumer_lag", "processing_latency_seconds", "alert_latency_seconds", "api_request_latency_seconds", "dlq_messages_total"):
        assert metric in exprs, metric
    source = (ROOT / "src/cvi/common/metrics.py").read_text()
    for series in ("events_received_total", "consumer_lag", "processing_latency_seconds", "alert_latency_seconds", "api_request_latency_seconds", "dlq_messages_total"):
        assert f'"{series}"' in source or f'"{series.removesuffix("_total")}' in source, series  # series exported by the code
    prom = yaml.safe_load((ROOT / "infrastructure/observability/prometheus.yml").read_text())
    jobs = {j["job_name"] for j in prom["scrape_configs"]}
    assert {"api", "ingestion-edge", "validator", "stream-processor", "lifecycle"} <= jobs
    yaml.safe_load((ROOT / "infrastructure/observability/alerts.yml").read_text())


def test_ci_workflow_has_every_required_stage():
    wf = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = wf["jobs"]
    assert {"lint", "unit", "integration", "security", "images", "deploy-validation", "e2e", "frontend"} <= set(jobs)
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    for tool in ("ruff", "mypy", "pytest", "--cov-fail-under=80", "bandit", "semgrep", "pip-audit", "trivy", "zap", "k6", "terraform validate", "chaos_compose"):
        assert tool in text, tool


def test_dockerfile_runs_as_non_root_and_has_healthcheck():
    d = (ROOT / "infrastructure/docker/Dockerfile.python").read_text()
    assert "USER 10001" in d and "HEALTHCHECK" in d and "FROM python:3.12-slim AS runtime" in d
    assert "USER" in (ROOT / "infrastructure/docker/Dockerfile.frontend").read_text() or "unprivileged" in (ROOT / "infrastructure/docker/Dockerfile.frontend").read_text()


def test_migrations_are_contiguous_and_documented():
    files = sorted(p.name for p in (ROOT / "migrations").glob("*.sql"))
    assert [f[:4] for f in files] == [f"{i:04d}" for i in range(1, len(files) + 1)]
    doc = (ROOT / "docs/database.md").read_text()
    for f in files:
        assert f in doc, f"{f} is not documented in docs/database.md"
