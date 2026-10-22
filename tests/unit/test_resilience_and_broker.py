import pytest

from cvi.common.resilience import CircuitBreaker, CircuitOpenError, backoff_delays, retry
from cvi.messaging.memory import MemoryBroker


def test_backoff_is_exponential_and_capped():
    d = backoff_delays(6, base=0.1, cap=1.0, jitter=False)
    assert d == [0.1, 0.2, 0.4, 0.8, 1.0, 1.0]
    jittered = backoff_delays(5, 0.1, 5, jitter=True)
    assert all(0 <= x <= y for x, y in zip(jittered, backoff_delays(5, 0.1, 5, jitter=False), strict=True))  # full jitter stays within the cap


def test_retry_succeeds_after_transient_failures_and_reraises_when_exhausted():
    calls, slept = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("x")
        return "ok"

    assert retry(flaky, retries=3, sleep=slept.append) == "ok" and len(calls) == 3 and len(slept) == 2
    with pytest.raises(ConnectionError):
        retry(lambda: (_ for _ in ()).throw(ConnectionError("always")), retries=2, sleep=lambda s: None)


def test_retry_does_not_retry_unlisted_exceptions():
    n = []
    with pytest.raises(ValueError):
        retry(lambda: n.append(1) or (_ for _ in ()).throw(ValueError("bug")), retries=3, retry_on=(ConnectionError,), sleep=lambda s: None)
    assert len(n) == 1


def test_circuit_breaker_opens_half_opens_and_closes():
    now = [0.0]
    cb = CircuitBreaker("t", failure_threshold=3, reset_timeout=10, clock=lambda: now[0])

    def fail():
        raise OSError("down")

    for _ in range(3):
        with pytest.raises(OSError):
            cb.call(fail)
    assert cb.state == "open"
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: "never runs")
    now[0] = 11
    assert cb.state == "half-open"
    with pytest.raises(OSError):  # probe fails -> re-open immediately
        cb.call(fail)
    assert cb.state == "open"
    now[0] = 22
    assert cb.call(lambda: "recovered") == "recovered" and cb.state == "closed"


def test_memory_broker_keyed_ordering_and_redelivery_until_commit():
    b = MemoryBroker(4)
    b.ensure_topics()
    for i in range(20):
        b.produce("t", f"vin{i % 3}", str(i).encode())
    c1 = b.subscribe("g", ["t"])
    first = c1.poll_batch(100, 0)
    assert len(first) == 20
    by_key = {}
    for m in first:
        by_key.setdefault(m.key, []).append(int(m.value))
    assert all(v == sorted(v) for v in by_key.values())  # per-key order preserved
    assert len({m.partition for m in first if m.key == "vin0"}) == 1  # same key -> same partition
    c1.commit(first[:5])
    c2 = b.subscribe("g", ["t"])  # restart: only committed progress survives
    redelivered = c2.poll_batch(100, 0)
    assert len(redelivered) == 20 - sum(1 for m in first[:5])  # at-least-once: everything uncommitted comes back
    assert c2.lag()["t"] == 15  # lag counts only the uncommitted messages


def test_memory_broker_pause_resume_and_outage():
    b = MemoryBroker(2)
    b.produce("t", "k", b"1")
    c = b.subscribe("g", ["t"])
    c.pause()
    assert c.poll_batch(10, 0) == []
    c.resume()
    assert len(c.poll_batch(10, 0)) == 1
    b.down = True
    with pytest.raises(ConnectionError):
        b.produce("t", "k", b"2")
    assert not b.healthy()
