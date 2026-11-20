import json

import pytest

from tools.amr_task.executor import EventLog, Executor, NavigationResult


class Clock:
    def __init__(self): self.now = 0.
    def __call__(self): return self.now
    def advance(self, delta): self.now += delta


class Navigator:
    def __init__(self):
        self.goals, self.result, self.cancels = [], None, 0
        self.fail_start = self.fail_cancel = False
    def start(self, destination):
        self.goals.append(destination)
        if self.fail_start: raise RuntimeError('uncertain dispatch')
    def poll(self):
        result, self.result = self.result, None
        return result
    def cancel(self):
        self.cancels += 1
        if self.fail_cancel: raise RuntimeError('transport lost')
    def arrive(self, status='SUCCEEDED', offset=0.):
        goal = self.goals[-1]
        self.result = NavigationResult(status, goal.x+offset, goal.y)


@pytest.fixture
def task(tmp_path, conversation):
    clock, nav = Clock(), Navigator()
    log = EventLog(tmp_path/'session', clock)
    gate = {'ok': True}
    def check():
        if not gate['ok']: raise RuntimeError('approval revoked')
    spoken = []
    e = Executor(conversation, nav, log, clock=clock, before_goal=check, announce=spoken.append,
                 navigation_timeout=10.)
    yield e, nav, clock, log, gate, spoken
    log.close()


def arrive(task, command='take this to Priya'):
    e, nav, clock, log, gate, spoken = task
    e.command(command)
    assert not nav.goals
    e.answer('yes')
    assert e.state == 'NAVIGATE'
    nav.arrive()
    e.tick()
    return e, nav, clock, log, gate, spoken


@pytest.mark.parametrize('ack,outcome', [('taken', 'DELIVERED'), ('not here', 'RETURNED_NOT_HERE'),
                                        ('wrong person', 'RETURNED_NOT_HERE')])
def test_delivery_ack_and_checked_return(task, ack, outcome):
    e, nav, clock, log, gate, spoken = arrive(task)
    assert e.state == 'WAIT_ACK' and spoken == ["I'm looking for Priya"]
    e.acknowledge(ack)
    assert e.state == 'RETURNING' and e.outcome is None
    assert nav.goals[-1].key == 'room:home'
    nav.arrive()
    e.tick()
    assert e.outcome == outcome and e.state == 'DONE'
    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert {'command', 'question', 'answer', 'confirm', 'goal', 'arrival', 'announce', 'ack', 'outcome', 'return'} <= {r['type'] for r in records}
    assert [r['seq'] for r in records] == list(range(1, len(records)+1))
    assert records[-1]['returned_home'] is True


def test_three_announcements_and_60_second_timeout(task):
    e, nav, clock, log, gate, spoken = arrive(task)
    clock.advance(14.99); e.tick(); assert len(spoken) == 1
    clock.advance(.01); e.tick(); assert len(spoken) == 2
    clock.advance(15); e.tick(); assert len(spoken) == 3
    clock.advance(29.99); e.tick(); assert len(spoken) == 3 and e.state == 'WAIT_ACK'
    clock.now = 60.; e.tick(); assert e.state == 'RETURNING'
    nav.arrive(); e.tick()
    assert e.outcome == 'RETURNED_TIMEOUT'


def test_late_ack_cannot_beat_deadline(task):
    e, nav, clock, *_ = arrive(task)
    clock.advance(60)
    e.acknowledge('taken')
    nav.arrive(); e.tick()
    assert e.outcome == 'RETURNED_TIMEOUT'


@pytest.mark.parametrize('status,offset', [('ABORTED', 0.), ('CANCELED', 0.), ('SUCCEEDED', .301),
                                         ('SUCCEEDED', float('nan'))])
def test_bad_arrival_never_announces_or_returns(task, status, offset):
    e, nav, clock, log, gate, spoken = task
    e.command('take this to Ravi'); e.answer('yes')
    nav.arrive(status, offset); e.tick()
    assert e.state == 'FAILED' and not spoken and len(nav.goals) == 1 and nav.cancels == 1


def test_plain_go_finishes_without_delivery_or_return(task):
    e, nav, clock, log, gate, spoken = arrive(task, 'go to the open office')
    assert e.state == 'DONE' and e.outcome == 'NAVIGATED' and not spoken and len(nav.goals) == 1


def test_return_failure_is_not_a_delivered_claim(task):
    e, nav, *_ = arrive(task)
    e.acknowledge('taken'); nav.arrive('ABORTED'); e.tick()
    assert e.outcome == 'RETURN_FAILED' and e.pending_outcome == 'DELIVERED'


def test_navigation_timeout_cancels_only_active_task(task):
    e, nav, clock, *_ = task
    e.command('take this to Priya'); e.answer('yes')
    clock.advance(10); e.tick()
    assert e.state == 'FAILED' and nav.cancels == 1 and len(nav.goals) == 1


def test_approval_revoked_before_confirmation_or_return(task):
    e, nav, clock, log, gate, spoken = task
    e.command('take this to Priya'); gate['ok'] = False; e.answer('yes')
    assert not nav.goals and e.state == 'FAILED'
    gate['ok'] = True
    arrive(task)
    gate['ok'] = False
    e.acknowledge('taken')
    assert len(nav.goals) == 1 and e.state == 'FAILED'


def test_uncertain_dispatch_and_failed_cancel_latch(task):
    e, nav, *_ = task
    nav.fail_start = nav.fail_cancel = True
    e.command('go to home'); e.answer('yes')
    assert e.cancellation_uncertain and nav.cancels == 1
    with pytest.raises(RuntimeError, match='Cancellation'):
        e.command('go to reception')


def test_no_motion_without_yes_and_no_second_command_during_motion(task):
    e, nav, *_ = task
    e.command('go to the office')
    e.answer('team office')
    assert not nav.goals and e.state == 'CONFIRM'
    e.answer('yes')
    with pytest.raises(ValueError): e.command('go to home')
    with pytest.raises(ValueError): e.acknowledge('taken')
    e.cancel()
    assert len(nav.goals) == 1 and nav.cancels == 1


def test_event_log_never_overwrites_session(tmp_path):
    log = EventLog(tmp_path/'same')
    log.emit('command', text='test')
    with pytest.raises(FileExistsError): EventLog(tmp_path/'same')
    log.close()
