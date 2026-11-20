import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('bench_utils',Path('scripts/bench_utils.py'))
helpers=importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def test_wilson_keeps_zero_events_uncertain_and_no_denominator_undefined():
    assert helpers.wilson(0,0)['rate'] is None
    zero=helpers.wilson(0,10)
    assert zero['wilson95'][0]==pytest.approx(0.,abs=1e-12)
    assert zero['wilson95'][1]==pytest.approx(.2775327998628892)
    full=helpers.wilson(10,10)
    assert full['wilson95'][0]==pytest.approx(1-zero['wilson95'][1])
    assert full['wilson95'][1]==pytest.approx(1.)


def test_latency_percentile_does_not_hide_slowest_request():
    assert helpers.percentile([], .95) is None
    assert helpers.percentile([100.,2.,3.,1.,4.], .95)==100.


def test_benchmark_penalizes_extra_targets_and_failed_samples(monkeypatch,names,form):
    monkeypatch.syspath_prepend(str(Path('scripts').resolve()))
    import benchmark_synthetic as benchmark
    from amr_language.grounding import Names
    row={'text':'go to reception','expected_task_form':form(place='reception'),
         'expected_ids':[{'place':['reception']}]}
    roster=Names(names)
    refusal={'refusal':{'template':'uncalibrated'}}
    good=[{'form':form(place='reception')}]*7
    assert benchmark.assess(good,row,roster,refusal)['grounding_correct'] is True
    bad=[{'form':form(place='reception',person='Priya')}]*7
    assert benchmark.assess(bad,row,roster,refusal)['grounding_correct'] is False
    failed=good+[{'error_type':'BackendTimeout'}]
    assert benchmark.assess(failed,row,roster,refusal)['all_sampled_forms_valid'] is False
    assert benchmark.assess(failed,row,roster,refusal)['grounding_correct'] is False


def test_reference_grammar_baseline_is_not_extended_for_multistep(names,form,monkeypatch):
    monkeypatch.syspath_prepend(str(Path('scripts').resolve()))
    import benchmark_synthetic as benchmark
    from amr_language.grounding import Names
    roster=Names(names)
    grammar=benchmark.grammar_baseline(roster)
    row={'text':'go to reception','expected_task_form':form(place='reception'),
         'expected_ids':[{'place':['reception']}]}
    assert benchmark.evaluate_grammar(grammar,row,roster)['grounding_correct'] is True
    row['text']='take file to Priya then come back'
    row['expected_task_form']=form('deliver',person='Priya',item='file')
    row['expected_task_form']['steps'].append(form('return')['steps'][0])
    row['expected_ids']=[{'person':['Priya']},{}]
    result=benchmark.evaluate_grammar(grammar,row,roster)
    assert result['parse_rejected'] is True and result['action_or_scope_correct'] is False
