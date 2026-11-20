"""One fallback command using the existing session; no ROS initialization here.

Calibrated ask path (grounding calibration): when the rule-based parser cannot place a command
on exactly one approved location, the grounding pilot's calibrated token scorer (v2
with the split-conformal threshold from its committed real-registry run) decides:
several places -> a closed question, one place -> "Did you mean ...?", unknown ->
"I don't know that place", nothing -> "say that again". Nothing moves on a
question. The next command answers it: a place in the offered set (or "yes" to a
single-place question) is dispatched as a normal validated goal; anything else
sends nothing. The question is one-shot and lives only in this session.

Per-map profiles (18 Oct 2026): a navigation session on a map with named places passes its
profile (places file, approval key, grounding calibration file). Without one, the
session uses the default world's profile (config/worlds.yaml).
"""
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo

from registry import COMMAND_MAPS, read_approvals

CALIBRATION_PACKAGE = 'tools/grounding_calibration'
DEFAULT_MAP = next(iter(COMMAND_MAPS))
GROUNDING_RESULTS = COMMAND_MAPS[DEFAULT_MAP]['grounding_results']    # the default world's calibration
YES = frozenset({'yes', 'y', 'yeah', 'yep', 'yes please', 'correct', 'right', 'ok', 'okay',
                 'sure', 'confirm', 'yes go', 'go ahead', 'affirmative'})
NO = frozenset({'no', 'n', 'nope', 'no thanks', 'cancel', 'wrong', 'never mind', 'nevermind'})
# A session without a profile uses the default world's profile.
DEFAULT_PROFILE = dict(COMMAND_MAPS[DEFAULT_MAP], map_id=DEFAULT_MAP)


def resolve_profile(root, profile=None):
    """Return the session's named-place profile and its places file, kept inside the expected folder."""
    profile = dict(DEFAULT_PROFILE if profile is None else profile)
    root = Path(root).resolve()
    locations = (root / profile['locations']).resolve()
    if (not locations.is_relative_to((root / 'src/amr_physical_ai/config').resolve())
            or locations.suffix != '.yaml' or not re.fullmatch(r'[a-z0-9_]+_places', str(profile['approval']))):
        raise ValueError('The typed-command profile points outside its allowed files.')
    return profile, locations


def truth_sample(message, received):
    """Map (0,0) = Gazebo world (-2,-0.5): the spawn point of every world in config/worlds.yaml."""
    for transform in message.transforms:
        if transform.child_frame_id != 'turtlebot3_waffle':
            continue
        if transform.header.frame_id != 'default':
            return None
        position = transform.transform.translation
        stamp = transform.header.stamp.sec + transform.header.stamp.nanosec * 1e-9
        if not all(math.isfinite(v) for v in (position.x, position.y, stamp)):
            return None
        return {'x': position.x + 2., 'y': position.y + .5,
                'world_x': position.x, 'world_y': position.y,
                'simulation_time_s': stamp, 'received_monotonic': received,
                'source': '/amr/ground_truth', 'world_frame': 'default',
                'entity': 'turtlebot3_waffle', 'map_offset_xy': [2., .5]}
    return None


def grounding_decide(root, locations_path, text, results=GROUNDING_RESULTS):
    """Token scorer v2 + the committed conformal threshold. Raises if anything is inconsistent."""
    package = str(Path(root) / CALIBRATION_PACKAGE)
    if package not in sys.path:
        sys.path.insert(0, package)
    from grounding_calibration import NONE_LABEL
    from grounding_calibration.conformal import prediction_set
    from grounding_calibration.policies import conformal_policy
    from grounding_calibration.registry import load_registry
    from grounding_calibration.token_scorer import TokenScorer

    source = results
    results_path = (Path(root) / source).resolve()
    if (not results_path.is_relative_to((Path(root) / 'config/calibration').resolve())
            or results_path.name != 'results.json'):
        raise ValueError('The grounding calibration must be a results.json under config/calibration.')
    results = json.loads(results_path.read_text())
    entries = load_registry(str(locations_path))
    scorer = TokenScorer(entries, temperature=float(results['meta']['temperature']))
    if results['meta']['scorer'] != scorer.name:
        raise ValueError(f"The grounding threshold was computed with {results['meta']['scorer']!r}, not {scorer.name!r}.")
    live = [{'key': e.key, 'name': e.name, 'aliases': list(e.aliases)} for e in entries]
    saved = [{k: r[k] for k in ('key', 'name', 'aliases')} for r in results['registry']]
    if live != saved:
        raise ValueError('The location registry changed since the grounding threshold was computed.')
    q_hat = float(results['headline']['q_hat'])
    probabilities = scorer.probabilities(text)
    decision = conformal_policy(probabilities, scorer.labels, q_hat)
    by_name = {e.name: e.key for e in entries}
    in_set = prediction_set(probabilities, q_hat, scorer.labels)
    places = [by_name[label] for label in in_set if label != NONE_LABEL]
    return {'scorer': scorer.name, 'q_hat': q_hat, 'temperature': scorer.temperature,
            'source': source, 'source_sha256': hashlib.sha256(results_path.read_bytes()).hexdigest(),
            'probabilities': {k: round(v, 6) for k, v in probabilities.items()},
            'set': in_set, 'set_locations': places, 'none_in_set': NONE_LABEL in in_set,
            'decision': decision.kind, 'target': by_name.get(decision.target)}


def grounding_question(grounding_result, locations):
    """Turn the grounding decision into the reply and, for a question, the pending answer set."""
    names = {key: key.replace('_', ' ') for key in locations.by_name}
    places = grounding_result['set_locations']
    if grounding_result['decision'] in ('ask', 'act') and places:
        listed = [f'the {names[key]}' for key in places]
        if len(listed) == 1:
            question = f'Did you mean {listed[0]}'
        else:
            question = 'Which one: ' + ', '.join(listed[:-1]) + f' or {listed[-1]}'
        if grounding_result['none_in_set']:
            question += ', or somewhere else'
        question += '?'
        kind = 'confirm' if len(places) == 1 else 'choose'
        return question, {'kind': kind, 'options': places, 'question': question}
    if grounding_result['decision'] == 'reject':
        return ("I don't know that place. I can go to: "
                + ', '.join(names[key] for key in locations.by_name) + '.'), None
    return 'Sorry, could you say that again?', None


def resolve_answer(pending, text, parser, normalize):
    """Return (location or None, reason) for an answer to a pending grounding question."""
    value = normalize(text)
    if pending['kind'] == 'confirm':
        if value in YES:
            return pending['options'][0], 'confirmed'
        if value in NO:
            return None, 'declined'
    location = None
    # A bare answer ("the north station") is read as "go to the north station".
    for candidate in (text, 'go to ' + text):
        parsed = parser.parse(candidate)
        if not parsed.get('clarification_needed'):
            location = parsed.get('location')
            break
    if location is None:
        return None, 'not_understood'
    if location not in pending['options']:
        return None, 'outside_offered_set'
    return location, 'named_in_set'


def command(root, session, text, *, clock=time.monotonic, profile=None):
    from amr_physical_ai.command_pipeline import (
        ClarificationRequired, CommandParser, Locations, normalize, validate_parsed)

    root = Path(root)
    started = clock()
    record = {'type': 'command', 'timestamp': datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(),
              'robot': 'TurtleBot3 Waffle simulation', 'text': text,
              'parser': 'RULE-BASED FALLBACK (no LLM)', 'parsed_json': None,
              'validation': {'valid': False},
              'nav2': {'status': 'NOT_SENT', 'goal_sent': False},
              'truth_before': None, 'truth_after': None, 'position_change_m': None,
              'true_final_error_m': None}
    location = None
    path = None
    grounding_text = text
    # A pending grounding question is one-shot: this command answers it, whatever it says.
    pending = getattr(session, 'grounding_pending', None)
    session.grounding_pending = None
    try:
        if session.stop_requested.is_set():
            raise InterruptedError('Session STOP requested; no command dispatched.')
        if session.active_goal is not None:
            raise RuntimeError('A previous navigation goal is still active; wait or press STOP.')
        profile, path = resolve_profile(root, profile)
        record['command_map'] = profile.get('map_id')
        record['locations_file'] = profile['locations']
        locations = Locations(path)
        record['locations_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        record['map_sha256'] = hashlib.sha256(locations.map_path.read_bytes()).hexdigest()
        record['map_image_sha256'] = hashlib.sha256(
            (locations.map_path.parent / locations.map_metadata['image']).read_bytes()).hexdigest()
        if not read_approvals(root / 'tools/amr_console/approvals.yaml').get(profile['approval']):
            raise ClarificationRequired('Named-place approval is not enabled in approvals.yaml.')
        record['truth_before'] = session.truth_pose()
        parser = CommandParser(locations, mode='fallback')
        if pending is not None:
            resolved, reason = resolve_answer(pending, text, parser, normalize)
            record['grounding_answer'] = {'question': pending['question'], 'kind': pending['kind'],
                                   'offered': list(pending['options']), 'answer_text': text,
                                   'resolved_location': resolved, 'resolution': reason}
            if resolved is None:
                replies = {'declined': "OK, I won't move. Give me a new command when ready.",
                           'outside_offered_set': ("That wasn't one of the places I offered, so nothing was sent. "
                                                   'Please give the command again.'),
                           'not_understood': ("I didn't catch one of the offered places, so nothing was sent. "
                                              'Please give the command again.')}
                raise ClarificationRequired(replies[reason])
            # Dispatch exactly as a typed command naming that place would be.
            grounding_text = 'go to ' + resolved
            record['parsed_json'] = {'location': resolved, 'confidence': 1.0, 'clarification_needed': False}
        else:
            record['parsed_json'] = parser.parse(text)
            if record['parsed_json'].get('clarification_needed'):
                try:
                    record['grounding'] = grounding_decide(root, path, text, profile['grounding_results'])
                except Exception as exc:  # grounding unavailable: keep the original clarification
                    record['grounding'] = {'error': str(exc)}
                else:
                    question, ask = grounding_question(record['grounding'], locations)
                    record['grounding']['reply'] = question
                    record['grounding']['awaiting_answer'] = ask is not None
                    if ask is not None:
                        session.grounding_pending = ask
                    raise ClarificationRequired(question)
        location, record['validation'] = validate_parsed(
            record['parsed_json'], locations, grounding_text, session.command_grid())

        def before_send():
            # Recheck approval and live map after waiting for the action server.
            if (not read_approvals(root / 'tools/amr_console/approvals.yaml').get(profile['approval'])
                    or hashlib.sha256(path.read_bytes()).hexdigest() != record['locations_sha256']):
                raise ClarificationRequired('Location approval/configuration changed; request a fresh preview.')
            validate_parsed(record['parsed_json'], locations, grounding_text, session.command_grid())
            session.truth_pose()  # Missing/stale truth must block dispatch too.
            if session.stop_requested.is_set():
                raise InterruptedError('Session STOP requested; no command dispatched.')
            record['nav2']['goal_sent'] = True  # Dispatch attempt, including transport errors.

        nav_started = clock()
        outcome = session.navigate({'x': location.x, 'y': location.y, 'yaw': location.yaw},
                                   before_send=before_send)
        record['nav2'].update(status=outcome['navigation_outcome'], status_code=outcome['status'],
                              navigation_wall_s=round(clock() - nav_started, 4))
    except ClarificationRequired as exc:
        record['validation'] = {'valid': False, 'question': str(exc)}
        record['nav2']['status'] = 'CLARIFICATION'
    except Exception as exc:
        record['nav2']['status'] = 'ERROR'
        record['error'] = str(exc)
        if record['nav2']['goal_sent']:
            # A dispatch timeout may still have a pending acceptance. Audited
            # worker cleanup owns cancellation and prevents a second request.
            session.stop_requested.set()
    try:
        if record['truth_before'] is not None:
            after = session.truth_pose(after=clock() + .5)
            before = record['truth_before']
            if after['simulation_time_s'] <= before['simulation_time_s']:
                raise RuntimeError('Gazebo ground-truth simulation time did not advance.')
            record['truth_after'] = after
            record['position_change_m'] = math.hypot(after['x'] - before['x'], after['y'] - before['y'])
            if location is not None and record['nav2']['goal_sent']:
                record['true_final_error_m'] = math.hypot(after['x'] - location.x, after['y'] - location.y)
    except Exception as exc:
        record['evidence_error'] = str(exc)
    record['total_wall_s'] = round(clock() - started, 4)
    return record
