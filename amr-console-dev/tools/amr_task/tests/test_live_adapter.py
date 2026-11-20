"""Adapter boundaries with in-memory ROS-shaped values; no ROS imported."""
from types import SimpleNamespace as NS
import queue
import sys
import threading
import time

import pytest

from tools.amr_task.run_live import CallbackPump, CommanderNavigator


class Future:
    def __init__(self, value=None): self.value = value
    def done(self): return True
    def result(self): return self.value


def status(uuid, code=2):
    return NS(status=code, goal_info=NS(goal_id=NS(uuid=uuid)))


def adapter(tags):
    a = object.__new__(CommanderNavigator)
    a.tags, a.map = tags, tags.grid
    a.statuses = []
    a.audit = lambda *args, **kwargs: None
    a.last_pose_age_s = .01
    a.own_uuid, a.completed_uuid = [1], None
    a.pose = lambda: (0., 0.)
    a.spin = lambda: None
    a.send_future = a.result_future = a.handle = None
    return a


def test_other_clients_block_but_completed_own_status_can_lag(tags):
    a = adapter(tags)
    a.statuses = [status([2])]
    with pytest.raises(RuntimeError, match='Another'): a.check_scene()
    a.statuses = [status([1])]
    a.check_scene()
    with pytest.raises(RuntimeError): a.check_scene(idle=True)
    a.completed_uuid = [1]
    a.check_scene(idle=True)


def test_wrong_live_map_blocks(tags):
    a = adapter(tags); a.map = None
    with pytest.raises(RuntimeError, match='/map'): a.check_scene()


def test_rejected_send_is_not_arrival(tags):
    a = adapter(tags)
    a.send_future = Future(NS(accepted=False))
    result = a.poll()
    assert result.status == 'REJECTED' and a.completed_uuid == [1]


def test_success_uses_final_pose_and_nav_result(tags):
    a = adapter(tags)
    a.nav = NS(getResult=lambda: NS(name='SUCCEEDED'))
    a.result_future = Future(NS(status=4))
    a.pose = lambda: (2., 3.)
    result = a.poll()
    assert (result.status, result.x, result.y) == ('SUCCEEDED', 2., 3.)
    assert 'not Gazebo truth' in result.pose_source


def test_cancel_pending_acceptance_targets_only_own_handle(tags):
    a = adapter(tags)
    calls = []
    class PendingResult:
        def done(self): return bool(calls)
        def result(self): return NS(status=5)
    own_result = PendingResult()
    def cancel():
        calls.append('own')
        return Future(NS(return_code=0))
    handle = NS(accepted=True, get_result_async=lambda: own_result, cancel_goal_async=cancel)
    a.send_future = Future(handle)
    a.cancel()
    assert calls == ['own'] and a.handle is None and a.result_future is None


def test_50hz_tf_stays_fresh_while_runner_idles_five_seconds(monkeypatch):
    """Exercise the production callback owner and pose guard without ROS."""
    monkeypatch.setitem(sys.modules, 'rclpy.time', NS(Time=lambda: None))
    callbacks = queue.Queue()
    stopped = threading.Event()
    latest = [time.monotonic()-10.]
    processed = []

    class FakeExecutor:
        def add_node(self, node): pass
        def remove_node(self, node): pass
        def spin(self):
            while not stopped.is_set():
                callback = callbacks.get()
                if callback is not None:
                    callback()
        def shutdown(self, timeout_sec):
            stopped.set()
            callbacks.put(None)

    def transform(*args):
        seconds = latest[0]
        return NS(header=NS(stamp=NS(sec=int(seconds), nanosec=int(seconds % 1 * 1e9))),
                  transform=NS(translation=NS(x=2., y=3.)))

    nav = object.__new__(CommanderNavigator)
    nav.buffer = NS(lookup_transform=transform)
    nav.nav = NS(get_clock=lambda: NS(now=lambda: NS(nanoseconds=int(time.monotonic()*1e9))))
    with pytest.raises(RuntimeError, match='observed age='):
        nav.pose()
    pump = CallbackPump(FakeExecutor(), nav.nav)
    feed_stop = threading.Event()

    def feed():
        while not feed_stop.is_set():
            stamp = time.monotonic()
            def deliver(stamp=stamp):
                latest[0] = stamp
                processed.append(stamp)
            callbacks.put(deliver)
            feed_stop.wait(.02)

    producer = threading.Thread(target=feed)
    producer.start()
    try:
        time.sleep(5.)  # No foreground spin, input processing or queue draining.
        pump.check()
        assert nav.pose() == (2., 3.)
        assert 0 <= nav.last_pose_age_s < 1.
        assert len(processed) >= 200
        print(f'Idle TF regression: callbacks={len(processed)}, age={nav.last_pose_age_s:.6f} s')
    finally:
        feed_stop.set()
        producer.join(timeout=1.)
        pump.close()
    assert not pump.thread.is_alive()


def test_callback_failure_is_fail_closed():
    class BrokenExecutor:
        def add_node(self, node): pass
        def remove_node(self, node): pass
        def spin(self): raise ValueError('callback failed')
        def shutdown(self, timeout_sec): pass
    pump = CallbackPump(BrokenExecutor(), object())
    pump.thread.join(timeout=1.)
    with pytest.raises(RuntimeError, match='callback processing failed'):
        pump.check()
    pump.close()
