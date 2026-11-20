import ast
from pathlib import Path
import math
import pytest

from amr_language.conformal import calibrate, calibration_score, prediction_set


def reference_functions():
    tree=ast.parse(Path('reference/current_clarify.py').read_text())
    nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef)
           and node.name in ('prediction_set','calibration_score','calibrate')]
    scope={'math':math,'EPS':1e-12}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'reference functions','exec'),scope)
    return scope


def test_matches_reference_max_slot_and_finite_sample_rank():
    reference=reference_functions()
    rows=[({'place':{'a':p,'b':1-p},'person':{'x':.8,'y':.2}}, {'place':'a','person':'x'})
          for p in (.1,.2,.3,.4,.5,.6,.7,.8,.9,1.)]
    assert calibration_score(*rows[0])==reference['calibration_score'](*rows[0])
    assert calibrate(rows)==reference['calibrate'](rows)
    for q in (0.,.1,.5,.9,1.):
        assert prediction_set(rows[0][0]['place'],q)==list(reference['prediction_set'](rows[0][0]['place'],q))
    assert calibrate(rows[:1])==1.


def test_zero_vote_label_included_at_threshold_one():
    assert prediction_set({'a':1.,'b':0.,'<none>':0.},1.)==['a','b','<none>']


@pytest.mark.parametrize('distribution', [{},{'a':float('nan')},{'a':-.1,'b':1.1},{'a':.1}])
def test_invalid_probability(distribution):
    with pytest.raises(ValueError):
        prediction_set(distribution,.2)


def test_all_truth_slots_required():
    with pytest.raises(ValueError):
        calibration_score({'room':{'a':1.}}, {'person':'x'})
