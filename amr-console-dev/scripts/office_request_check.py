#!/usr/bin/env python3
"""Office request check: a fixed list of test requests through the real language service, without the robot.

The same worker, model runner, sandbox and speech models as the console's Office tasks tab, driven
directly: every request in the cases file is sent, the robot's questions are answered from the case
(or with the usual reply for what it asks: yes, done, taken, an answer), and the result is checked
against what the case expects (decision, steps, question or refusal, language of the replies).
Cases with an "audio" file are first turned into text by the speech models, as the Speak button does.

Close the console first (both start the same model). Then, from this repository:

    python3 scripts/office_request_check.py                  # config/office_request_cases.json
    python3 scripts/office_request_check.py --only en         # only cases whose group starts so

Writes data/evaluation/office_requests/<time>/results.json and summary.md; prints one line per case.
"""
import argparse
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools/amr_console'))

FINAL = ('done', 'failed', 'cancelled', 'stopped', 'refused', 'rejected', 'asked', 'deferred')
ACTIVE = ('submitted', 'queued', 'understanding', 'awaiting_confirmation', 'running')


class Backend:
    """What OfficeTasks needs from the console when no robot session runs."""
    def __init__(self, root):
        self.root = Path(root)
        self.state, self.session, self.stopping = 'idle', None, False
        self.events = SimpleNamespace(publish=lambda event: None)
        self.office_cancel_generation = 0

    def office_cancel_goal(self):
        self.office_cancel_generation += 1
        return True

    def office_navigate(self, pose, **kw):
        raise RuntimeError('The request check runs without the robot.')


def target(step):
    # A collection names who it is collected from; its 'to' is the requester.
    if step.get('action') == 'retrieve':
        return step.get('from') or step.get('at') or ''
    return step.get('to') or step.get('from') or step.get('at') or ''


def generic_reply(expect, answer):
    expect = set(expect or ())
    for word in ('yes', 'done', 'taken'):
        if word in expect:
            return word
    if 'location' in expect:
        return 'reception'
    return answer or 'yes'


def check(case, job, lines, requester):
    """Problems found (empty list = pass)."""
    want, problems = case.get('expect', {}), []
    if want.get('decision') and job.get('decision') != want['decision']:
        problems.append(f"decision {job.get('decision')!r}, expected {want['decision']!r}")
    if 'steps' in want and job.get('decision') == 'act':
        got = [[s.get('action'), target(s)] for s in job.get('steps') or []]
        expected = [[a, requester if t == 'me' else t] for a, t in want['steps']]
        if [[a, str(t).lower()] for a, t in got] != [[a, str(t).lower()] for a, t in expected]:
            problems.append(f'steps {got}, expected {expected}')
    if want.get('ask') and (job.get('ask') or {}).get('event') != want['ask']:
        problems.append(f"question {(job.get('ask') or {}).get('event')!r}, expected {want['ask']!r}")
    if want.get('refuse') and want['refuse'] not in ((job.get('refuse') or {}).get('reason'), (job.get('refuse') or {}).get('event')):
        problems.append(f"refusal {job.get('refuse')}, expected {want['refuse']!r}")
    if want.get('language') and job.get('language_detected') != want['language']:
        problems.append(f"language {job.get('language_detected')!r}, expected {want['language']!r}")
    if want.get('language') and job.get('decision') == 'act':
        spoken = {l.get('language') for l in lines if l.get('kind') == 'robot' and l.get('event') in ('confirm_plan', 'start_step', 'report')}
        if spoken - {want['language']}:
            problems.append(f"requester heard languages {sorted(spoken)}, expected {want['language']!r}")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--cases', type=Path, default=ROOT / 'config/office_request_cases.json')
    parser.add_argument('--only', default='', help='comma-separated group prefixes, for example en,ta')
    parser.add_argument('--timeout', type=float, default=180.0, help='seconds per case')
    args = parser.parse_args()
    from office_tasks import OfficeTasks
    data = json.loads(args.cases.read_text(encoding='utf-8'))
    requester = data.get('requester', 'Bharathwaj')
    prefixes = tuple(p.strip() for p in args.only.split(',') if p.strip())
    cases = [c for c in data['cases'] if not prefixes or c['group'].startswith(prefixes)]
    out = ROOT / 'data/evaluation/office_requests' / time.strftime('%Y%m%d_%H%M%S')
    out.mkdir(parents=True, exist_ok=True)
    office = OfficeTasks(Backend(ROOT))
    results = []
    try:
        office.start('rehearsal')
        deadline = time.monotonic() + 240
        while office.snapshot()['service']['state'] != 'ready':
            if office.snapshot()['service']['state'] == 'error' or time.monotonic() > deadline:
                sys.exit('The language service did not start: ' + str(office.snapshot()['service']))
            time.sleep(.5)
        snap = office.snapshot()
        print('Language service ready:', snap['service'].get('detail', ''))
        if any(c.get('audio') for c in cases):
            deadline = time.monotonic() + 120
            while office.snapshot()['speech'].get('state') not in ('ready', 'off', 'unavailable', 'error') and time.monotonic() < deadline:
                time.sleep(.5)
        for number, case in enumerate(cases, 1):
            started = time.monotonic()
            text, heard = case.get('text', ''), None
            if case.get('audio'):
                audio = ROOT / case['audio']
                if not audio.exists():
                    results.append({**case, 'skipped': f'{case["audio"]} not found'})
                    print(f"[{number}/{len(cases)}] {case['id']}: skipped (no recording)")
                    continue
                heard = office.transcribe(audio.read_bytes(), case.get('language') or 'auto', case.get('language'))
                text = heard.get('text') or ''
                if not text:
                    results.append({**case, 'heard': heard, 'problems': ['speech gave no words'], 'passed': False})
                    print(f"[{number}/{len(cases)}] {case['id']}: FAIL speech gave no words {heard}")
                    continue
            job_id = office.submit(text, requester, case.get('language') or 'auto', None)['job_id']
            replies, sent, idle = list(case.get('replies', [])), [], None
            while time.monotonic() - started < args.timeout:
                snap = office.snapshot()
                job = next((j for j in snap['jobs'] if j['job_id'] == job_id), {})
                waiting = snap['awaiting']
                if waiting and waiting.get('job_id') == job_id:
                    closing = waiting.get('last_event') in ('anything_else', 'anything_else_what', 'arrive_visit', 'call_person', 'what_change')
                    answer = (replies.pop(0) if replies else 'no' if closing
                              else generic_reply(waiting.get('expect'), case.get('answer')))
                    if len(sent) >= 12:
                        answer = 'stop'
                    sent.append({'asked': waiting.get('last_event'), 'expect': waiting.get('expect'), 'reply': answer})
                    office.reply(waiting['input_id'], answer)
                elif job.get('state') not in ACTIVE and not snap['busy']:
                    # Finished only if it stays so: the robot may be about to ask one more thing.
                    idle = idle or time.monotonic()
                    if time.monotonic() - idle > .6:
                        break
                    time.sleep(.1)
                    continue
                idle = None
                time.sleep(.1)
            else:
                office.cancel()
            snap = office.snapshot()
            job = next((j for j in snap['jobs'] if j['job_id'] == job_id), {})
            lines = [l for l in snap['timeline'] if l.get('job_id') == job_id]
            problems = check(case, job, lines, requester)
            if time.monotonic() - started >= args.timeout:
                problems.append(f'did not finish within {args.timeout:.0f} s')
            result = {**case, 'sent_text': text, 'heard': heard, 'replies_sent': sent,
                      'job': {k: job.get(k) for k in ('state', 'decision', 'english', 'steps', 'readback', 'planner',
                                                     'latency_ms', 'language_detected', 'ask', 'refuse', 'outcomes',
                                                     'answers')},
                      'robot_lines': [(l.get('event'), l.get('language'), l.get('text')) for l in lines if l.get('kind') == 'robot'],
                      'seconds': round(time.monotonic() - started, 1), 'problems': problems, 'passed': not problems}
            results.append(result)
            mark = 'pass' if not problems else 'FAIL ' + '; '.join(problems)
            print(f"[{number}/{len(cases)}] {case['id']}: {mark} ({result['seconds']} s, "
                  f"{job.get('planner')}, {job.get('latency_ms') and round(job['latency_ms'])} ms)")
    finally:
        office.shutdown()
    (out / 'results.json').write_text(json.dumps(results, indent=1, ensure_ascii=False, default=str) + '\n', encoding='utf-8')
    groups = {}
    for r in results:
        g = groups.setdefault(r['group'], [0, 0, 0])
        g[0 if r.get('passed') else 1 if 'skipped' not in r else 2] += 1
    lines = ['# Office request check ' + out.name, '', '| Group | Passed | Failed | Skipped |', '| --- | --- | --- | --- |']
    lines += [f'| {g} | {p} | {f} | {s} |' for g, (p, f, s) in groups.items()]
    lines += ['', '| Case | Result | Sent | Decision | Steps / question / refusal | Read back |', '| --- | --- | --- | --- | --- | --- |']
    for r in results:
        job = r.get('job', {})
        detail = (json.dumps([[s.get('action'), target(s)] for s in job.get('steps') or []], ensure_ascii=False)
                  if job.get('decision') == 'act' else json.dumps(job.get('ask') or job.get('refuse'), ensure_ascii=False))
        status = 'skipped' if 'skipped' in r else 'pass' if r.get('passed') else 'FAIL: ' + '; '.join(r.get('problems', []))
        lines.append(f"| {r['id']} | {status} | {r.get('sent_text', '')} | {job.get('decision', '')} | {detail} | "
                     f"{(job.get('readback') or '').replace('|', '/')} |")
    (out / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    passed = sum(1 for r in results if r.get('passed'))
    print(f'{passed}/{len(results)} passed. Results: {out}')


if __name__ == '__main__':
    main()
