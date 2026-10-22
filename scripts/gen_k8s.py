#!/usr/bin/env python
"""Generate the Kustomize manifests under infrastructure/kubernetes (kept as a script so replicas, resources, probes and
security contexts stay consistent across services).   python scripts/gen_k8s.py"""

import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
base = ROOT / "infrastructure" / "kubernetes" / "base"
ov = ROOT / "infrastructure" / "kubernetes" / "overlays"
for d in (base, ov / "dev", ov / "aws"):
    d.mkdir(parents=True, exist_ok=True)
IMG = "cvi-python"


def deployment(name, command, port, replicas, cpu_req, mem_req, cpu_lim, mem_lim, live="/health/live", ready=None):
    ready = ready or live
    return textwrap.dedent(f"""\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: {name}
          labels: {{ app.kubernetes.io/name: {name}, app.kubernetes.io/part-of: cvi }}
        spec:
          replicas: {replicas}
          revisionHistoryLimit: 3
          strategy: {{ type: RollingUpdate, rollingUpdate: {{ maxUnavailable: 0, maxSurge: 1 }} }}
          selector: {{ matchLabels: {{ app.kubernetes.io/name: {name} }} }}
          template:
            metadata:
              labels: {{ app.kubernetes.io/name: {name}, app.kubernetes.io/part-of: cvi }}
              annotations: {{ prometheus.io/scrape: "true", prometheus.io/port: "{port}", prometheus.io/path: /metrics }}
            spec:
              serviceAccountName: cvi
              securityContext: {{ runAsNonRoot: true, runAsUser: 10001, seccompProfile: {{ type: RuntimeDefault }} }}
              terminationGracePeriodSeconds: 45
              topologySpreadConstraints:
                - {{ maxSkew: 1, topologyKey: topology.kubernetes.io/zone, whenUnsatisfiable: ScheduleAnyway, labelSelector: {{ matchLabels: {{ app.kubernetes.io/name: {name} }} }} }}
              containers:
                - name: {name}
                  image: {IMG}:latest
                  imagePullPolicy: IfNotPresent
                  command: {command}
                  ports:
                    - {{ name: http, containerPort: {port} }}
                  env:
                    - {{ name: HEALTH_PORT, value: "{port}" }}
                  envFrom:
                    - configMapRef: {{ name: cvi-config }}
                    - secretRef: {{ name: cvi-secrets }}
                  resources:
                    requests: {{ cpu: {cpu_req}, memory: {mem_req} }}
                    limits: {{ cpu: "{cpu_lim}", memory: {mem_lim} }}
                  securityContext: {{ allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {{ drop: [ALL] }} }}
                  volumeMounts: [{{ name: tmp, mountPath: /tmp }}, {{ name: data, mountPath: /data }}]
                  startupProbe: {{ httpGet: {{ path: {live}, port: http }}, periodSeconds: 5, failureThreshold: 30 }}
                  livenessProbe: {{ httpGet: {{ path: {live}, port: http }}, periodSeconds: 10, timeoutSeconds: 3, failureThreshold: 3 }}
                  readinessProbe: {{ httpGet: {{ path: {ready}, port: http }}, periodSeconds: 5, timeoutSeconds: 3, failureThreshold: 3 }}
              volumes: [{{ name: tmp, emptyDir: {{}} }}, {{ name: data, emptyDir: {{}} }}]
        ---
        apiVersion: v1
        kind: Service
        metadata:
          name: {name}
          labels: {{ app.kubernetes.io/name: {name} }}
        spec:
          selector: {{ app.kubernetes.io/name: {name} }}
          ports: [{{ name: http, port: {port}, targetPort: http }}]
        ---
        apiVersion: policy/v1
        kind: PodDisruptionBudget
        metadata: {{ name: {name} }}
        spec:
          minAvailable: 1
          selector: {{ matchLabels: {{ app.kubernetes.io/name: {name} }} }}
        """)


services = {
    "api.yaml": deployment("api", '["python", "-m", "cvi.api"]', 8000, 3, "250m", "384Mi", "1", "768Mi", "/health/live", "/health/ready"),
    "ingestion-edge.yaml": deployment("ingestion-edge", '["python", "-m", "cvi.ingestion.gateway"]', 8001, 3, "500m", "256Mi", "2", "512Mi", "/health/live", "/health/ready"),
    "validator.yaml": deployment("validator", '["python", "-m", "cvi.ingestion.validator"]', 9102, 3, "500m", "256Mi", "2", "512Mi", "/"),
    "stream-processor.yaml": deployment("stream-processor", '["python", "-m", "cvi.stream.worker"]', 9101, 6, "1", "768Mi", "2", "1536Mi", "/"),
    "lifecycle.yaml": deployment("lifecycle", '["python", "-m", "cvi.lifecycle"]', 9103, 1, "250m", "512Mi", "1", "1Gi", "/"),
}
for n, c in services.items():
    (base / n).write_text(c)


def write(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text))


write(base / "frontend.yaml", """\
    apiVersion: apps/v1
    kind: Deployment
    metadata: { name: frontend, labels: { app.kubernetes.io/name: frontend, app.kubernetes.io/part-of: cvi } }
    spec:
      replicas: 2
      selector: { matchLabels: { app.kubernetes.io/name: frontend } }
      template:
        metadata: { labels: { app.kubernetes.io/name: frontend, app.kubernetes.io/part-of: cvi } }
        spec:
          securityContext: { runAsNonRoot: true, seccompProfile: { type: RuntimeDefault } }
          containers:
            - name: frontend
              image: cvi-frontend:latest
              ports: [{ name: http, containerPort: 8080 }]
              resources: { requests: { cpu: 50m, memory: 32Mi }, limits: { cpu: 250m, memory: 128Mi } }
              securityContext: { allowPrivilegeEscalation: false, capabilities: { drop: [ALL] } }
              readinessProbe: { httpGet: { path: /healthz, port: http }, periodSeconds: 5 }
              livenessProbe: { httpGet: { path: /healthz, port: http }, periodSeconds: 10 }
    ---
    apiVersion: v1
    kind: Service
    metadata: { name: frontend }
    spec:
      selector: { app.kubernetes.io/name: frontend }
      ports: [{ name: http, port: 80, targetPort: http }]
    """)

write(base / "config.yaml", """\
    # Non-secret configuration. Endpoints are provided per environment by the overlays (RDS/ElastiCache/MSK/... in AWS).
    apiVersion: v1
    kind: ConfigMap
    metadata: { name: cvi-config }
    data:
      ENV: production
      BROKER: kafka
      OBJECT_STORE: s3
      LOG_LEVEL: INFO
      KAFKA_BOOTSTRAP: kafka:9092
      REDIS_URL: redis://redis:6379/0
      MONGO_URL: mongodb://mongo:27017
      S3_BUCKET: cvi-telemetry
      TOPIC_PARTITIONS: "48"
      RATE_LIMIT_PER_MINUTE: "600"
      ALLOWED_LATENESS_S: "300"
      RAW_EVENTS_TTL_DAYS: "7"
      BATCH_SIZE: "500"
      LIFECYCLE_INTERVAL_S: "300"
      CORS_ORIGINS: https://cvi.example.com
    ---
    # PLACEHOLDER secret for local clusters. Production replaces this with an External Secrets object backed by
    # AWS Secrets Manager (see overlays/aws). Never commit real values.
    apiVersion: v1
    kind: Secret
    metadata: { name: cvi-secrets }
    type: Opaque
    stringData:
      DATABASE_URL: postgresql://cvi_app:CHANGE_ME@postgres:5432/cvi
      WORKER_DATABASE_URL: postgresql://cvi_worker:CHANGE_ME@postgres:5432/cvi
      ADMIN_DATABASE_URL: postgresql://cvi:CHANGE_ME@postgres:5432/cvi
      APP_DB_PASSWORD: CHANGE_ME
      WORKER_DB_PASSWORD: CHANGE_ME
      JWT_SECRET: CHANGE_ME_CHANGE_ME_CHANGE_ME_32CH
      INGEST_API_KEY: CHANGE_ME
    ---
    apiVersion: v1
    kind: ServiceAccount
    metadata:
      name: cvi
      annotations: {}   # overlays/aws adds the IRSA role ARN (S3 access without static keys)
    """)

write(base / "migrate-job.yaml", """\
    # Schema migrations + reference/seed data. Runs once per release (ArgoCD PreSync, or `kubectl apply`).
    apiVersion: batch/v1
    kind: Job
    metadata:
      name: cvi-init
      annotations: { argocd.argoproj.io/hook: PreSync }
    spec:
      backoffLimit: 3
      ttlSecondsAfterFinished: 3600
      template:
        spec:
          restartPolicy: OnFailure
          serviceAccountName: cvi
          securityContext: { runAsNonRoot: true, runAsUser: 10001, seccompProfile: { type: RuntimeDefault } }
          containers:
            - name: init
              image: cvi-python:latest
              command: ["python", "scripts/seed.py", "--vehicles", "100000", "--history-alerts", "200000"]
              envFrom: [{ configMapRef: { name: cvi-config } }, { secretRef: { name: cvi-secrets } }]
              resources: { requests: { cpu: 500m, memory: 1Gi }, limits: { cpu: "2", memory: 3Gi } }
              securityContext: { allowPrivilegeEscalation: false, capabilities: { drop: [ALL] } }
    """)

write(base / "ingress.yaml", """\
    apiVersion: networking.k8s.io/v1
    kind: Ingress
    metadata:
      name: cvi
      annotations:
        nginx.ingress.kubernetes.io/proxy-body-size: 8m
        nginx.ingress.kubernetes.io/limit-rps: "50"
    spec:
      ingressClassName: nginx
      tls: [{ hosts: [cvi.example.com], secretName: cvi-tls }]
      rules:
        - host: cvi.example.com
          http:
            paths:
              - { path: /v1/telemetry, pathType: Prefix, backend: { service: { name: ingestion-edge, port: { number: 8001 } } } }
              - { path: /api, pathType: Prefix, backend: { service: { name: api, port: { number: 8000 } } } }
              - { path: /auth, pathType: Prefix, backend: { service: { name: api, port: { number: 8000 } } } }
              - { path: /, pathType: Prefix, backend: { service: { name: frontend, port: { number: 80 } } } }
    """)


def hpa(name, mn, mx, cpu, note=""):
    return note + textwrap.dedent(f"""\
        apiVersion: autoscaling/v2
        kind: HorizontalPodAutoscaler
        metadata: {{ name: {name} }}
        spec:
          scaleTargetRef: {{ apiVersion: apps/v1, kind: Deployment, name: {name} }}
          minReplicas: {mn}
          maxReplicas: {mx}
          metrics: [{{ type: Resource, resource: {{ name: cpu, target: {{ type: Utilization, averageUtilization: {cpu} }} }} }}]
          behavior: {{ scaleDown: {{ stabilizationWindowSeconds: 300 }} }}
        """)


(base / "hpa.yaml").write_text("---\n".join([
    hpa("api", 3, 20, 60), hpa("ingestion-edge", 3, 30, 55), hpa("validator", 3, 24, 60),
    hpa("stream-processor", 6, 48, 65, "# maxReplicas = TOPIC_PARTITIONS (48): extra replicas would sit idle. overlays/aws scales on Kafka lag via KEDA.\n")]))

write(base / "networkpolicy.yaml", """\
    # Default deny ingress; only the ingress controller reaches api/frontend/edge, only Prometheus scrapes metrics.
    apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    metadata: { name: default-deny-ingress }
    spec:
      podSelector: {}
      policyTypes: [Ingress]
    ---
    apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    metadata: { name: allow-ingress-controller }
    spec:
      podSelector:
        matchExpressions: [{ key: app.kubernetes.io/name, operator: In, values: [api, frontend, ingestion-edge] }]
      policyTypes: [Ingress]
      ingress:
        - from: [{ namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: ingress-nginx } } }]
        - from: [{ podSelector: { matchLabels: { app.kubernetes.io/name: frontend } } }]
    ---
    apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    metadata: { name: allow-prometheus }
    spec:
      podSelector: { matchLabels: { app.kubernetes.io/part-of: cvi } }
      policyTypes: [Ingress]
      ingress:
        - from: [{ namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: monitoring } } }]
    """)

write(base / "kustomization.yaml", """\
    apiVersion: kustomize.config.k8s.io/v1beta1
    kind: Kustomization
    resources:
      - config.yaml
      - api.yaml
      - ingestion-edge.yaml
      - validator.yaml
      - stream-processor.yaml
      - lifecycle.yaml
      - frontend.yaml
      - migrate-job.yaml
      - ingress.yaml
      - hpa.yaml
      - networkpolicy.yaml
    """)

write(ov / "dev" / "kustomization.yaml", """\
    # Local cluster (kind / minikube / k3d): single replicas, in-cluster dependencies from dev-deps.yaml.
    apiVersion: kustomize.config.k8s.io/v1beta1
    kind: Kustomization
    resources: [../../base, dev-deps.yaml]
    images:
      - { name: cvi-python, newName: cvi-python, newTag: local }
      - { name: cvi-frontend, newName: cvi-frontend, newTag: local }
    patches:
      - target: { kind: HorizontalPodAutoscaler }
        patch: |-
          - { op: replace, path: /spec/minReplicas, value: 1 }
          - { op: replace, path: /spec/maxReplicas, value: 3 }
      - target: { kind: Deployment, labelSelector: "app.kubernetes.io/part-of=cvi" }
        patch: |-
          - { op: replace, path: /spec/replicas, value: 1 }
    """)

write(ov / "dev" / "dev-deps.yaml", """\
    # Minimal in-cluster dependencies for development ONLY (production uses managed services, see overlays/aws).
    apiVersion: apps/v1
    kind: StatefulSet
    metadata: { name: postgres }
    spec:
      serviceName: postgres
      replicas: 1
      selector: { matchLabels: { app: postgres } }
      template:
        metadata: { labels: { app: postgres } }
        spec:
          containers:
            - name: postgres
              image: pgvector/pgvector:pg16
              env: [{ name: POSTGRES_USER, value: cvi }, { name: POSTGRES_PASSWORD, value: CHANGE_ME }, { name: POSTGRES_DB, value: cvi }]
              ports: [{ containerPort: 5432 }]
              readinessProbe: { exec: { command: [pg_isready, -U, cvi] }, periodSeconds: 5 }
              resources: { requests: { cpu: 250m, memory: 512Mi }, limits: { memory: 2Gi } }
    ---
    apiVersion: v1
    kind: Service
    metadata: { name: postgres }
    spec: { selector: { app: postgres }, ports: [{ port: 5432 }] }
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata: { name: redis }
    spec:
      replicas: 1
      selector: { matchLabels: { app: redis } }
      template:
        metadata: { labels: { app: redis } }
        spec: { containers: [{ name: redis, image: "redis:7-alpine", ports: [{ containerPort: 6379 }], resources: { limits: { memory: 512Mi } } }] }
    ---
    apiVersion: v1
    kind: Service
    metadata: { name: redis }
    spec: { selector: { app: redis }, ports: [{ port: 6379 }] }
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata: { name: mongo }
    spec:
      replicas: 1
      selector: { matchLabels: { app: mongo } }
      template:
        metadata: { labels: { app: mongo } }
        spec: { containers: [{ name: mongo, image: "mongo:7", ports: [{ containerPort: 27017 }], resources: { limits: { memory: 1Gi } } }] }
    ---
    apiVersion: v1
    kind: Service
    metadata: { name: mongo }
    spec: { selector: { app: mongo }, ports: [{ port: 27017 }] }
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata: { name: kafka }
    spec:
      replicas: 1
      selector: { matchLabels: { app: kafka } }
      template:
        metadata: { labels: { app: kafka } }
        spec:
          containers:
            - name: kafka
              image: apache/kafka:3.8.0
              ports: [{ containerPort: 9092 }]
              env:
                - { name: KAFKA_NODE_ID, value: "1" }
                - { name: KAFKA_PROCESS_ROLES, value: "broker,controller" }
                - { name: KAFKA_LISTENERS, value: "PLAINTEXT://:9092,CONTROLLER://:9093" }
                - { name: KAFKA_ADVERTISED_LISTENERS, value: "PLAINTEXT://kafka:9092" }
                - { name: KAFKA_CONTROLLER_LISTENER_NAMES, value: CONTROLLER }
                - { name: KAFKA_LISTENER_SECURITY_PROTOCOL_MAP, value: "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT" }
                - { name: KAFKA_CONTROLLER_QUORUM_VOTERS, value: "1@localhost:9093" }
                - { name: KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR, value: "1" }
                - { name: KAFKA_AUTO_CREATE_TOPICS_ENABLE, value: "false" }
              resources: { limits: { memory: 1Gi } }
    ---
    apiVersion: v1
    kind: Service
    metadata: { name: kafka }
    spec: { selector: { app: kafka }, ports: [{ port: 9092 }] }
    """)

write(ov / "aws" / "kustomization.yaml", """\
    # AWS (EKS): endpoints of RDS / ElastiCache / MSK / DocumentDB come from Terraform outputs; secrets from AWS Secrets Manager.
    apiVersion: kustomize.config.k8s.io/v1beta1
    kind: Kustomization
    namespace: cvi
    resources: [../../base, external-secret.yaml, keda-scaledobject.yaml]
    images:
      - { name: cvi-python, newName: 000000000000.dkr.ecr.us-east-1.amazonaws.com/cvi-python, newTag: "1.0.0" }
      - { name: cvi-frontend, newName: 000000000000.dkr.ecr.us-east-1.amazonaws.com/cvi-frontend, newTag: "1.0.0" }
    patches:
      - target: { kind: ConfigMap, name: cvi-config }
        patch: |-
          - { op: replace, path: /data/KAFKA_BOOTSTRAP, value: "b-1.cvi.xxxxxx.kafka.us-east-1.amazonaws.com:9092,b-2.cvi.xxxxxx.kafka.us-east-1.amazonaws.com:9092" }
          - { op: replace, path: /data/REDIS_URL, value: "rediss://cvi-redis.xxxxxx.use1.cache.amazonaws.com:6379/0" }
          - { op: replace, path: /data/MONGO_URL, value: "mongodb://cvi-docdb.cluster-xxxxxx.us-east-1.docdb.amazonaws.com:27017/?tls=true&retryWrites=false" }
      - target: { kind: ServiceAccount, name: cvi }
        patch: |-
          - { op: replace, path: /metadata/annotations, value: { "eks.amazonaws.com/role-arn": "arn:aws:iam::000000000000:role/cvi-app" } }
      # KEDA owns autoscaling of the stream processors in AWS, so drop the CPU-based HPA to avoid two controllers fighting
      - patch: |-
          $patch: delete
          apiVersion: autoscaling/v2
          kind: HorizontalPodAutoscaler
          metadata: { name: stream-processor }
    """)

write(ov / "aws" / "external-secret.yaml", """\
    # Requires the External Secrets Operator + a ClusterSecretStore named aws-secrets-manager (see docs/deployment.md).
    apiVersion: external-secrets.io/v1beta1
    kind: ExternalSecret
    metadata: { name: cvi-secrets }
    spec:
      refreshInterval: 1h
      secretStoreRef: { name: aws-secrets-manager, kind: ClusterSecretStore }
      target: { name: cvi-secrets, creationPolicy: Merge }
      dataFrom: [{ extract: { key: cvi/prod/app } }]   # JSON secret created by Terraform (modules/data)
    """)

write(ov / "aws" / "keda-scaledobject.yaml", """\
    # Scale stream processors on Kafka consumer lag (needs KEDA). Replaces CPU-based scaling in production.
    apiVersion: keda.sh/v1alpha1
    kind: ScaledObject
    metadata: { name: stream-processor }
    spec:
      scaleTargetRef: { name: stream-processor }
      minReplicaCount: 6
      maxReplicaCount: 48
      triggers:
        - type: kafka
          metadata:
            bootstrapServers: b-1.cvi.xxxxxx.kafka.us-east-1.amazonaws.com:9092
            consumerGroup: stream-processor
            topic: telemetry.validated
            lagThreshold: "5000"
    """)
print("kubernetes manifests written to", base.parent)
