"""Offline tests for the grounding calibration pilot. They use fixtures only, never the project's real files."""
import math
import os

import pytest

from grounding_calibration.conformal import calibrate, coverage_bounds, prediction_set
from grounding_calibration.dataset import fingerprint, generate, split
from grounding_calibration.evaluate import summarise
from grounding_calibration.policies import greedy_policy, threshold_policy, conformal_policy, choose_tau, outcome
from grounding_calibration.registry import load_registry, parse_registry
from grounding_calibration.token_scorer import TokenScorer, content_tokens

HERE = os.path.dirname(__file__)
STANDIN = os.path.join(HERE, "..", "fixtures", "standin_named_locations.yaml")
STATIONS = ["east station", "north station", "south station"]
ALPHA = 0.10


@pytest.fixture(scope="module")
def reg():
    return load_registry(STANDIN)


@pytest.fixture(scope="module")
def scorer(reg):
    return TokenScorer(reg)


@pytest.fixture(scope="module")
def data(reg, scorer):
    cmds = generate(reg)
    probs = {c.id: scorer.probabilities(c.text) for c in cmds}
    return cmds, probs


@pytest.fixture(scope="module")
def q_hat(data):
    cmds, probs = data
    cal, _ = split(cmds)
    return calibrate([probs[c.id] for c in cal], [c.intent for c in cal], ALPHA)


# ---- registry ---------------------------------------------------------------
def test_standin_registry_reads_four_locations(reg):
    assert [e.name for e in reg] == ["home", "east station", "north station", "south station"]
    assert (reg[1].x, reg[1].y) == (0.8, 0.0)
    assert (reg[3].x, reg[3].y) == (0.4, -0.5)


def test_registry_list_schema_with_aliases_and_pose_list():
    reg = parse_registry({"approval_status": "confirmed", "locations": [
        {"name": "east_station", "aliases": ["dock A"], "pose": [0.8, 0.0]},
        {"id": "home", "aliases": "base", "x": 0, "y": 0}]})
    assert [e.name for e in reg] == ["east station", "home"]
    assert reg[0].aliases == ("dock a",) and reg[1].aliases == ("base",)
    assert (reg[0].x, reg[0].y) == (0.8, 0.0)


def test_registry_flat_mapping_skips_non_location_keys():
    reg = parse_registry({"approval_status": "confirmed", "frame_id": "map",
                          "home": {"x": 0, "y": 0}, "lab_door": {"position": {"x": 1, "y": 2}}})
    assert [e.name for e in reg] == ["home", "lab door"]


def test_registry_rejects_empty_and_duplicates():
    with pytest.raises(ValueError):
        parse_registry({"approval_status": "confirmed"})
    with pytest.raises(ValueError):
        parse_registry({"locations": [{"name": "home"}, {"name": "Home"}]})


# ---- token scorer -----------------------------------------------------------------
def test_tokens_drop_fillers_and_fold_plurals():
    assert content_tokens("Could you please go to one of the stations now?") == ["station"]


def test_probabilities_sum_to_one(scorer):
    for t in ["go to the east station", "go to the kitchen", "go to the station", ""]:
        p = scorer.probabilities(t)
        assert math.isclose(sum(p.values()), 1.0, rel_tol=1e-9)
        assert set(p) == set(scorer.labels)


def test_ambiguous_command_gives_exactly_the_three_stations(scorer, q_hat):
    for t in ["go to the station", "head to any station", "go to one of the stations"]:
        assert prediction_set(scorer.probabilities(t), q_hat, scorer.labels) == STATIONS
        d = conformal_policy(scorer.probabilities(t), scorer.labels, q_hat)
        assert d.kind == "ask" and d.options == tuple(STATIONS)
        assert d.question == "Which one: the east station, the north station or the south station?"


def test_unknown_place_is_rejected_without_motion(scorer, q_hat):
    d = conformal_policy(scorer.probabilities("go to the kitchen"), scorer.labels, q_hat)
    assert d.kind == "reject" and not d.moves


def test_hard_negative_never_moves(scorer, q_hat):
    d = conformal_policy(scorer.probabilities("go to the west station"), scorer.labels, q_hat)
    assert not d.moves


def test_exact_typo_partial_and_filler_commands_act_on_the_right_place(scorer, q_hat):
    cases = {"go to the east station": "east station", "navigate to the east staion": "east station",
             "go to north": "north station", "I need you at the south station now": "south station",
             "go home": "home"}
    for text, want in cases.items():
        d = conformal_policy(scorer.probabilities(text), scorer.labels, q_hat)
        assert (d.kind, d.target) == ("act", want), text


def test_alias_maps_to_its_location():
    reg = parse_registry({"locations": [{"name": "east_station", "aliases": ["charging bay"], "x": 0.8, "y": 0},
                                        {"name": "north_station", "x": 0.4, "y": 0.6}]})
    s = TokenScorer(reg)
    p = s.probabilities("take me to the charging bay")
    assert max(s.labels, key=p.get) == "east station"


# ---- conformal ------------------------------------------------------------------
def test_quantile_index_matches_the_formula():
    probs = [{"a": 1 - s} for s in [0.1 * i for i in range(1, 11)]]   # scores 0.1 .. 1.0
    intents = ["a"] * 10
    assert math.isclose(calibrate(probs, intents, 0.10), 1.0)   # k = ceil(11 * 0.9) = 10
    assert math.isclose(calibrate(probs, intents, 0.20), 0.9)   # k = ceil(11 * 0.8) = 9
    assert calibrate(probs[:5], intents[:5], 0.10) == 1.0       # k = 6 > n = 5: keep everything


def test_coverage_bounds():
    lo, hi = coverage_bounds(65, 0.1)
    assert lo == pytest.approx(0.9) and hi == pytest.approx(0.9 + 1 / 66)


def test_marginal_coverage_holds_over_random_splits(data, scorer):
    cmds, probs = data
    cov = []
    for r in range(300):
        cal, test = split(cmds, seed=1000 + r)
        q = calibrate([probs[c.id] for c in cal], [c.intent for c in cal], ALPHA)
        cov.append(sum(c.intent in prediction_set(probs[c.id], q, scorer.labels) for c in test) / len(test))
    assert sum(cov) / len(cov) >= 1 - ALPHA


# ---- policies and dataset -------------------------------------------------------
def test_greedy_moves_on_ambiguous_commands_and_conformal_does_not(scorer, q_hat):
    p = scorer.probabilities("go to the station")
    assert greedy_policy(p, scorer.labels).moves
    assert not conformal_policy(p, scorer.labels, q_hat).moves


def test_fixed_threshold_tau_reaches_target_on_calibration(data, scorer):
    cmds, probs = data
    cal, _ = split(cmds)
    tau = choose_tau([probs[c.id] for c in cal], [c.intent for c in cal], scorer.labels, 1 - ALPHA)
    ok = sum(outcome(threshold_policy(probs[c.id], scorer.labels, tau), c.intent)["success"] for c in cal)
    assert ok / len(cal) >= 1 - ALPHA


def test_conformal_makes_no_wrong_move_on_the_standin_dataset(data, scorer, q_hat):
    cmds, probs = data
    s = summarise([conformal_policy(probs[c.id], scorer.labels, q_hat) for c in cmds], cmds)
    assert s["wrong_move"] == 0.0


def test_dataset_is_deterministic_and_sized(reg):
    a, b = generate(reg), generate(reg)
    assert fingerprint(a) == fingerprint(b)
    assert len(a) >= 80
    cal, test = split(a)
    assert len(cal) >= 30 and len(cal) + len(test) == len(a)
    assert {c.cls for c in a} == {"valid_exact", "valid_typo", "valid_partial", "valid_extra",
                                  "ambiguous", "unknown"}


def test_ambiguous_intents_are_spread_evenly(reg):
    amb = [c for c in generate(reg) if c.cls == "ambiguous"]
    counts = {s: sum(c.intent == s for c in amb) for s in STATIONS}
    assert max(counts.values()) - min(counts.values()) <= 2


# ---- v2: "right away" is a filler phrase; "right station" stays an alias ---------
REAL_SHAPE = os.path.join(HERE, "..", "fixtures", "real_shape_named_locations.yaml")


def test_right_away_is_dropped_as_a_phrase_but_right_is_kept():
    assert content_tokens("go to north stop right away") == ["north", "stop"]
    assert content_tokens("go to the right station") == ["right", "station"]
    assert content_tokens("turn right away from the wall") == ["turn", "from", "wall"]


def test_aliased_registry_station_asks_and_right_station_is_east():
    reg = load_registry(REAL_SHAPE)
    s = TokenScorer(reg)
    cmds = generate(reg)
    probs = {c.id: s.probabilities(c.text) for c in cmds}
    cal, _ = split(cmds)
    q = calibrate([probs[c.id] for c in cal], [c.intent for c in cal], ALPHA)
    decide = lambda t: conformal_policy(s.probabilities(t), s.labels, q)
    d = decide("go to the station")
    assert d.kind == "ask" and d.options == tuple(STATIONS)
    assert (decide("go to the right station").kind, decide("go to the right station").target) == ("act", "east station")
    assert (decide("go to north stop right away").kind, decide("go to north stop right away").target) == ("act", "north station")
    assert decide("go to the kitchen").kind == "reject"
    assert summarise([conformal_policy(probs[c.id], s.labels, q) for c in cmds], cmds)["wrong_move"] == 0.0
