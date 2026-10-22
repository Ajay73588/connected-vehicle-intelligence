# Algorithms

Every algorithm below is implemented in `src/cvi/algorithms` (or where noted) and unit-tested in `tests/unit/test_algorithms.py`.
Complexities are per operation unless stated; n = elements, m = bits, k = hash functions, V/E = graph vertices/edges.

## 1. Deduplication — exact gate (Redis) + Bloom filter fallback
* **Problem**: devices retry and networks duplicate; the same `event_id` must affect aggregates exactly once, across restarts and replicas.
* **Algorithm**: exact — `SADD seen:{vin}:{hour} event_id` inside the same Lua script that increments aggregates (atomic claim); fallback
  — `BloomFilter` (blake2b double hashing, m = −n·ln p / (ln 2)², k = (m/n)·ln 2) used **only while Redis is unavailable**.
* **Why appropriate**: Redis gives exact, restart-safe dedupe with TTL; a Bloom filter gives bounded memory (2M ids at p=0.001 ≈ 3.6 MB)
  and *no false negatives* when the exact store is down (a false positive drops one event, a false negative would double count).
* **Time/space**: Redis O(1)/O(window ids); Bloom O(k) time, O(m) space.
* **Alternative**: cuckoo filter (supports deletion), or a database unique index (too slow at 100K/s).
* **Why this choice**: simplest structure with the property we need during degradation (no false negatives); verified FP rate < 3% for a 1% target.

## 2. Event-time sliding window with out-of-order insertion (`algorithms/windowing.py`)
* **Problem**: counts like "harsh brakes in the last 30 minutes" must be correct when events arrive late or out of order.
* **Algorithm**: `SlidingWindow` keeps `(ts, value)` sorted by event time (append in the common in-order case, `bisect` insert otherwise);
  eviction is relative to the newest event time (watermark), not wall-clock; running sum for O(1) totals; least-squares slope for trends.
* **Time/space**: add O(1) amortised in order, O(log n + n) worst case for late events (n small: window of ≤ 1800 s); evict amortised O(1); O(window) space.
* **Alternative**: tumbling windows (cheap but boundary artefacts), Flink-style watermarks with allowed lateness (heavier infra).
* **Why**: per-vehicle windows are tiny, so sorted insertion is fast and exact; lateness cap (`ALLOWED_LATENESS_S`) bounds the cost.

## 3. EWMA z-score anomaly detection (`OnlineStats`)
* **Problem**: flag sensor values that deviate from *this vehicle's own* baseline without storing history.
* **Algorithm**: exponentially weighted mean/variance (`mean += α·d; var = (1−α)(var + α·d²)`), z = (x−mean)/σ evaluated before the update, no verdict during a 20-sample warm-up; alert needs ≥ 3 hits > 4σ within 2 minutes.
* **Time/space**: O(1)/O(1) per signal per vehicle.
* **Alternatives**: Isolation Forest/autoencoders (need training and more state), CUSUM (better for small shifts).
* **Why**: cheap enough for 100K vehicles in a Python worker and explainable ("4σ above this vehicle's norm").

## 4. Count-Min Sketch and Space-Saving Top-K (`algorithms/sketches.py`)
* **Problem**: fleet-wide heavy hitters (most frequent DTCs, noisiest vehicles) over an unbounded stream.
* **Algorithm**: CMS (width ⌈e/ε⌉, depth ⌈ln 1/δ⌉, never under-counts, over-count ≤ εN w.p. 1−δ); Space-Saving keeps k counters and replaces the minimum (guaranteed to contain every item with frequency > N/k); exact `heapq.nlargest` for small sets (`top_k_exact`, used by `/analytics/top-dtc`).
* **Time/space**: CMS O(depth) / O(width·depth); Space-Saving O(k) per update (k tiny) / O(k); heap Top-K O(n log k) / O(k).
* **Alternative**: exact `GROUP BY` (fine at current volumes, linear memory at millions of keys), HyperLogLog for distinct counts (used in Redis for active vehicles/hour).
* **Why**: sketch structures are the building block for streaming "trending" views when SQL group-by would be too heavy; validated by property tests.

## 5. Geohash and geofencing (`algorithms/geo.py`)
* **Problem**: viewport queries over live positions of 100K vehicles; "is the vehicle inside the service area/restricted zone?".
* **Algorithm**: geohash encode/decode (bit interleaving, base32), 8-neighbour lookup, Redis sets per precision-4 cell; viewport → covering cells → VINs → state hashes. Geofences: haversine circle test; ray-casting point-in-polygon.
* **Time/space**: encode O(precision); viewport O(cells + vehicles in view); polygon O(vertices).
* **Alternative**: Redis GEO (`GEOSEARCH`, needs ≥ 6.2), PostGIS/R-tree, H3 hexagons.
* **Why**: geohash cells are plain Redis sets → works on every Redis version, trivially shardable and cache-friendly; also drives nearest-vehicle lookup and privacy masking (geohash-5 centre).

## 6. Graph algorithms — Dijkstra and A* (`algorithms/graph.py`)
* **Problem**: ETA for "nearest vehicle" dispatch over a street network.
* **Algorithm**: binary-heap Dijkstra; A* with haversine heuristic (admissible because straight-line ≤ any road path). Used by the agent tool `find_nearby_vehicle` on a synthetic 61×61 street grid anchored on the tenant's depot (a real road graph is the production replacement).
* **Time/space**: O((V+E) log V) / O(V); A* expands far fewer nodes with a good heuristic. Tests assert A* = Dijkstra distances.
* **Alternative**: contraction hierarchies / OSRM for continent-scale routing.
* **Why**: textbook, correct, small; shortlist by geohash first (candidates = k nearest by cell), route only those.

## 7. Dynamic programming — maintenance scheduling knapsack (`knapsack_schedule`)
* **Problem**: the workshop has H hours; which vehicles should be serviced to remove the most expected failures?
* **Algorithm**: 0/1 knapsack, value = predicted failure probability (risk removed), weight = service hours (4 h ICE, 6 h EV); DP table with reconstruction.
* **Time/space**: O(n·H) / O(n·H) with n ≤ 60 candidates and H ≤ 200.
* **Alternative**: greedy by risk/hour (not optimal), ILP solver (overkill).
* **Why**: exact and instant at this size; property test compares to brute force.

## 8. VIN validation (`algorithms/vin.py`)
* **Problem**: reject spoofed/garbled identifiers before they reach storage.
* **Algorithm**: ISO 3779 / 49 CFR 565: length 17, charset without I/O/Q, transliteration table, weights `[8,7,6,5,4,3,2,10,0,9,8,7,6,5,4,3,2]`, check digit = sum mod 11 (`10 → X`). `make_vin` generates valid synthetic VINs.
* **Time/space**: O(17) / O(1).
* **Alternative**: lookup against a registry (needs external data).
* **Why**: free; detects every single-character substitution in the 16 weighted positions (11 is prime and all weights and character values are below 11); tested with the canonical valid VIN `1HGCM82633A004352`.

## 9. OBD-II parsing (`algorithms/obd.py`)
* **Problem**: telematics units may send raw J1979 frames.
* **Algorithm**: mode-01 PID decoding (`41 0C 1A F8` → rpm = (256·A+B)/4, coolant = A−40, …); mode-03 DTC bytes → `P0301` (2-bit system letter, 2-bit digit, 3 hex nibbles); `encode_*` inverses for the simulator.
* **Complexity**: O(frame length).
* **Alternative**: python-OBD (talks to hardware, not needed for offline decoding).
* **Why**: the validator fills missing signals from `obd_raw` and rejects malformed frames (`obd_parse_error`).

## 10. Streaming rule engine and health/safety scoring (`stream/detectors.py`)
Penalty model: health = 100 − Σ penalties (overheating 30, DTC by severity ×1.5 if repeated (cap 50), low oil 20, battery SoH deficit×1.2 (cap 40), vibration 10, sensor anomaly 10); driver risk = min(100, 12·harsh_brakes + 10·harsh_accels + min(40, speeding samples) + idle bonus). O(#signals) per event. Chosen for explainability: every point of score has a named cause shown to users and the agent.

## 11. Hashed n-gram embeddings (`storage/vector.py`)
Signed feature hashing of unigrams+bigrams into 256 dims, L2-normalised; cosine similarity via pgvector (`<=>`, HNSW) or client-side. O(tokens) to embed. Alternative: sentence-transformer. Chosen for offline determinism; limitation: lexical, not semantic.
