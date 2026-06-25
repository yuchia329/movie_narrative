"""gpud on-demand pool: placement by free vRAM, bounded pools, warm reuse, idle reap,
heartbeat-TTL reclaim (all pure logic, with mocked NVML + fake launcher + injected clock),
plus the orchestrator `lease()` context manager against an in-process fake gpud.

Guarded on grpc so the suite stays green where the web extra isn't installed."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("grpc")  # yapper_rpc stubs + the gpud client need grpcio

# gpud lives in server/ (deployed to the GPU box), not an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import gpud  # noqa: E402


# --- fakes -----------------------------------------------------------------
class Clock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeProbe:
    def __init__(self, free: dict[int, int]):
        self.free = dict(free)
        self.total = {g: 24000 for g in free}

    def gpus(self):
        return sorted(self.free)

    def free_mb(self, i):
        return self.free[i]

    def total_mb(self, i):
        return self.total[i]


class FakeLauncher:
    """Instant launch; optionally decrements the probe's free vRAM to mimic a loaded model."""

    def __init__(self, probe: FakeProbe | None = None, vram: dict[str, int] | None = None):
        self.launched: list[tuple[str, int, int]] = []
        self.terminated: list = []
        self.probe = probe
        self.vram = vram or {"asr": 7600, "tts": 4500}

    def launch(self, service, gpu_index, port):
        self.launched.append((service, gpu_index, port))
        if self.probe is not None:
            self.probe.free[gpu_index] -= self.vram.get(service, 0)
        return ("handle", service, gpu_index, port)

    def wait_ready(self, handle, port, timeout):
        return None

    def terminate(self, handle):
        self.terminated.append(handle)
        if self.probe is not None and isinstance(handle, tuple):
            _, svc, gpu, _port = handle
            self.probe.free[gpu] += self.vram.get(svc, 0)


def make_cfg(**kw) -> "gpud.Config":
    base = dict(
        services={
            "asr": gpud.ServiceSpec("asr", kw.pop("asr_max", 3), 7600),
            "tts": gpud.ServiceSpec("tts", kw.pop("tts_max", 3), 4500),
        },
        port_range=kw.pop("port_range", (50060, 50069)),
        headroom_mb=kw.pop("headroom_mb", 1024),
        idle_timeout_s=kw.pop("idle_timeout_s", 60.0),
        lease_ttl_s=kw.pop("lease_ttl_s", 120.0),
        acquire_timeout_s=kw.pop("acquire_timeout_s", 180.0),
    )
    base.update(kw)
    return gpud.Config(**base)


def sup(cfg=None, free=None, launcher=None, clock=None):
    probe = FakeProbe(free or {0: 24000, 1: 24000})
    ln = launcher or FakeLauncher(probe)
    return gpud.Supervisor(cfg or make_cfg(), probe, ln, clock or Clock()), probe, ln


# --- placement + pool admission --------------------------------------------
def test_acquire_launches_and_places_on_gpu_with_room():
    s, probe, ln = sup(free={0: 1000, 1: 9000})  # asr needs 7600+1024=8624 -> only gpu 1
    lease = s.acquire("asr")
    assert len(ln.launched) == 1
    assert lease.instance.gpu_index == 1
    assert 50060 <= lease.instance.port <= 50069


def test_acquire_reuses_warm_idle_instance():
    s, probe, ln = sup()
    l1 = s.acquire("asr")
    assert s.release(l1.lease_id) is True
    l2 = s.acquire("asr")
    assert len(ln.launched) == 1            # reused, no second launch
    assert l2.instance is l1.instance


def test_no_capacity_raises_unavailable():
    # neither GPU fits an ASR instance -> Acquire gives up immediately (timeout 0)
    s, probe, ln = sup(cfg=make_cfg(acquire_timeout_s=0.0), free={0: 1000, 1: 1000})
    with pytest.raises(gpud.Unavailable):
        s.acquire("asr")
    assert ln.launched == []


def test_pool_capped_at_max_then_unavailable():
    s, probe, ln = sup(cfg=make_cfg(asr_max=2, acquire_timeout_s=0.0), free={0: 24000, 1: 24000})
    l1 = s.acquire("asr")
    l2 = s.acquire("asr")
    assert len(ln.launched) == 2
    assert l1.instance.port != l2.instance.port          # distinct ports from the range
    with pytest.raises(gpud.Unavailable):                # MAX reached, none free
        s.acquire("asr")


def test_asr_and_tts_are_independent_pools():
    s, probe, ln = sup(cfg=make_cfg(asr_max=1, tts_max=1))
    a = s.acquire("asr")
    t = s.acquire("tts")
    assert a.instance.service == "asr" and t.instance.service == "tts"
    assert {svc for svc, _g, _p in ln.launched} == {"asr", "tts"}


# --- reaping + crash reclaim ------------------------------------------------
def test_idle_instance_reaped_after_grace():
    clock = Clock(0.0)
    s, probe, ln = sup(cfg=make_cfg(idle_timeout_s=60.0), clock=clock)
    l1 = s.acquire("asr")
    s.release(l1.lease_id)
    assert s.reap_once() == 0          # released just now -> within grace
    clock.t = 61.0
    assert s.reap_once() == 1          # idle past grace -> SIGTERM


def test_shutdown_terminates_all_instances():
    # shutdown() must terminate every instance (leased OR idle) so none orphan + pin vRAM
    # when gpud stops. Regression guard for the gpud SIGTERM-cleanup fix.
    s, _probe, ln = sup(cfg=make_cfg(asr_max=2, tts_max=2))
    a = s.acquire("asr")               # leased
    t = s.acquire("tts")
    s.release(t.lease_id)              # idle but still alive
    assert len(ln.launched) == 2
    assert s.shutdown() == 2
    assert len(ln.terminated) == 2     # both subprocesses SIGTERM'd
    insts, _ = s.status()
    assert insts == []                 # pool emptied
    assert s.heartbeat(a.lease_id) is False   # leases cleared
    assert ln.terminated and s.status()[0] == []


def test_missed_heartbeat_reclaims_lease():
    clock = Clock(0.0)
    s, probe, ln = sup(cfg=make_cfg(lease_ttl_s=120.0, idle_timeout_s=60.0), clock=clock)
    l1 = s.acquire("asr")
    clock.t = 130.0                    # past the lease TTL with no heartbeat
    s.reap_once()
    assert l1.lease_id not in s._leases            # lease reclaimed
    assert s._instances[0].leased is False          # instance freed for reuse
    # heartbeat keeps a lease alive: re-acquire then refresh past the old TTL
    l2 = s.acquire("asr")
    clock.t = 260.0
    assert s.heartbeat(l2.lease_id) is True
    s.reap_once()
    assert l2.lease_id in s._leases


# --- orchestrator lease() context manager ----------------------------------
@pytest.fixture
def fake_gpud():
    import grpc
    from concurrent import futures

    from yapper_rpc import gpud_pb2, gpud_pb2_grpc

    class Servicer(gpud_pb2_grpc.GpudServicer):
        def __init__(self):
            self.released: list[str] = []
            self.heartbeats: list[str] = []

        def Acquire(self, request, context):
            return gpud_pb2.Lease(lease_id="L1", target="localhost:50061",
                                  service=request.service, ready=True)

        def Heartbeat(self, request, context):
            self.heartbeats.append(request.lease_id)
            return gpud_pb2.HeartbeatReply(ok=True)

        def Release(self, request, context):
            self.released.append(request.lease_id)
            return gpud_pb2.ReleaseReply(ok=True)

    servicer = Servicer()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    gpud_pb2_grpc.add_GpudServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    yield f"127.0.0.1:{port}", servicer
    server.stop(0)


def _settings(target: str):
    from yapper_web.settings import Settings
    return Settings(gpu_supervisor_target=target, gpud_heartbeat_s=100.0, gpud_acquire_timeout_s=5.0)


def test_lease_yields_rewritten_target_and_releases(fake_gpud):
    from yapper_web import gpu_supervisor

    target, servicer = fake_gpud
    with gpu_supervisor.lease("asr", settings=_settings(target)) as dial:
        # host comes from the supervisor target, port from the lease (gpud said localhost:50061)
        assert dial == f"{target.rsplit(':', 1)[0]}:50061"
    assert servicer.released == ["L1"]


def test_lease_releases_on_exception(fake_gpud):
    from yapper_web import gpu_supervisor

    target, servicer = fake_gpud
    with pytest.raises(RuntimeError):
        with gpu_supervisor.lease("asr", settings=_settings(target)):
            raise RuntimeError("boom")
    assert servicer.released == ["L1"]


def test_lease_disabled_yields_none_when_flag_unset():
    from yapper_web import gpu_supervisor

    with gpu_supervisor.lease("asr", settings=_settings("")) as dial:
        assert dial is None


# --- lease_many: multi-instance leasing for parallel TTS --------------------
@pytest.fixture
def fake_gpud_pool():
    """gpud fake whose Acquire grants distinct leases up to ``max_acquires`` and then aborts
    UNAVAILABLE (mimics a pool capped / contended) so the degrade path can be exercised."""
    import threading
    from concurrent import futures

    import grpc

    from yapper_rpc import gpud_pb2, gpud_pb2_grpc

    servers = []

    def _make(max_acquires: int = 99):
        class Servicer(gpud_pb2_grpc.GpudServicer):
            def __init__(self):
                self.released: list[str] = []
                self._n = 0
                self._lock = threading.Lock()

            def Acquire(self, request, context):
                with self._lock:
                    self._n += 1
                    i = self._n
                if i > max_acquires:
                    context.abort(grpc.StatusCode.UNAVAILABLE, "pool full")
                return gpud_pb2.Lease(lease_id=f"L{i}", target=f"localhost:{50060 + i}",
                                      service=request.service, ready=True)

            def Heartbeat(self, request, context):
                return gpud_pb2.HeartbeatReply(ok=True)

            def Release(self, request, context):
                with self._lock:
                    self.released.append(request.lease_id)
                return gpud_pb2.ReleaseReply(ok=True)

        servicer = Servicer()
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
        gpud_pb2_grpc.add_GpudServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        servers.append(server)
        return f"127.0.0.1:{port}", servicer

    yield _make
    for s in servers:
        s.stop(0)


def test_lease_many_leases_multiple_distinct_and_releases_all(fake_gpud_pool):
    from yapper_web import gpu_supervisor

    target, servicer = fake_gpud_pool(max_acquires=3)
    with gpu_supervisor.lease_many("tts", 3, settings=_settings(target)) as dials:
        assert len(dials) == 3
        assert len(set(dials)) == 3                      # distinct instances
    assert sorted(servicer.released) == ["L1", "L2", "L3"]   # every lease freed


def test_lease_many_degrades_to_whats_available(fake_gpud_pool):
    from yapper_web import gpu_supervisor

    # pool can satisfy only 2; the 3rd Acquire aborts UNAVAILABLE and is dropped.
    target, servicer = fake_gpud_pool(max_acquires=2)
    with gpu_supervisor.lease_many("tts", 3, settings=_settings(target)) as dials:
        assert len(dials) == 2                           # degraded, not failed
    assert len(servicer.released) == 2                   # only the leased ones released


def test_lease_many_disabled_yields_single_none():
    from yapper_web import gpu_supervisor

    with gpu_supervisor.lease_many("tts", 3, settings=_settings("")) as dials:
        assert dials == [None]
