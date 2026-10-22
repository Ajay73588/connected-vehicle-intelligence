# ADR-001: Apache Kafka (not RabbitMQ / managed queues) as the event backbone

- Status: accepted
- Date: 2026-10-01

## Context
The platform ingests millions of small telemetry events per minute from 100,000+ vehicles. Requirements that shape the choice:
per-vehicle ordering, replay after consumer bugs or outages, several independent consumers of the same stream (validator, stream
processor, future analytics), horizontal scale-out of consumers, a 3x burst for 5 minutes without losing data, and
at-least-once delivery that downstream idempotency can turn into effectively-once effects.

## Decision
Use **Apache Kafka** (KRaft mode locally, Amazon MSK in AWS) with four topics:

| topic | key | purpose |
|---|---|---|
| `telemetry.raw` | VIN | everything the edge accepted, unvalidated (durable buffer between devices and validation) |
| `telemetry.validated` | VIN | schema/VIN/OBD-validated events, input of the stream processor |
| `telemetry.dlq` | VIN | rejected messages with a machine-readable `reason` |
| `alerts` | VIN | alerts as they are persisted (for notification/integration consumers) |

* Partition key = **VIN**: all events of a vehicle land in one partition (per-vehicle order, simple per-vehicle state) and the
  100,000 distinct VINs hash uniformly over partitions, so there are no hot partitions. (A tenant key would make a large tenant a
  hot spot; a time key would make "now" a hot spot.)
* Consumer groups (`validator`, `stream-processor`) give horizontal scaling up to `TOPIC_PARTITIONS` (12 locally, 48 in K8s).
* Offsets are committed **only after** all side effects of a batch succeeded (at-least-once); duplicates are neutralised downstream
  (see ADR-003).
* The code talks to a small `Broker` interface (`src/cvi/messaging`) with a Kafka implementation (confluent-kafka) and an in-memory
  implementation with the same semantics (keyed partitions, committed offsets, pause/resume, lag). The in-memory broker is used in
  unit/integration tests and in the Docker-free embedded dev mode only.

## Alternatives considered
* **RabbitMQ**: excellent for work queues and routing, but consumed messages are deleted, so replay and multi-consumer fan-out of a
  high-volume stream need extra machinery (streams plugin); per-key ordering with competing consumers requires consistent-hash
  exchanges. Throughput per node is lower for this append-only, replayable workload.
* **MQTT only**: right protocol for devices (we do use Mosquitto as a front door), wrong durability/replay model for the processing tier.
* **Cloud-native queues (SQS/Kinesis/PubSub)**: viable, but tie application code to a vendor; Kafka protocol is available on every
  cloud (MSK, Confluent, Event Hubs Kafka endpoint, Redpanda) which satisfies the cloud-portability requirement.
* **Redpanda** (Kafka API, single binary): attractive for laptops; the compose file uses Apache Kafka itself so judges see the real thing,
  and the code is identical for Redpanda.

## Trade-offs
* Operational weight: Kafka is heavier than a queue. Mitigated by KRaft (no ZooKeeper) locally and a managed service in the cloud.
* Ordering is per partition only; cross-vehicle ordering is neither needed nor provided.
* Consumer parallelism is capped by the partition count; the count is chosen up front (48 in the cloud profile, derived from the capacity model in docs/performance/benchmarks.md) with headroom and can be raised later
  (re-keying moves vehicles between partitions, which the per-vehicle state rebuild tolerates).

## Consequences
* Replay = reset the consumer-group offset; the idempotent pipeline makes replays safe.
* Back-pressure is natural: the stream processor pauses consumption and stops committing when a sink is down; lag becomes the
  signal (`consumer_lag` metric, KEDA scaling, alert rule).
* **Verification status:** the Kafka adapter is exercised by compose and by the CI integration job (service container); on the
  development laptop used to build this repository Docker was not available, so the Kafka path was not executed locally (see
  README "Known limitations"). All pipeline logic above the `Broker` interface was tested against the in-memory implementation.
