"""Regression: a technical refusal must not receive task-correctness credit."""
import importlib
from pathlib import Path

import pytest


@pytest.mark.parametrize('reason',['model_timeout','model_unavailable','invalid_plan'])
def test_technical_refusals_never_count_as_out_of_scope_success(monkeypatch,reason):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    report=importlib.import_module('report_office_check')
    row={'id':'STARTER-en-09','language':'en',
         'proposed_labels':{'expected_issue':'out_of_scope','expected_steps':[]},
         'result':{'decision':'refuse','refuse':{'event':'cannot_do','reason':reason},'ask':None,'steps':[]}}
    result=report.score(row)
    assert not result['proposed_structure_match']
    assert not result['proposed_targets_match']
    assert not result['proposed_exact_plan_match']
    row['result']['refuse']['reason']='out_of_scope'
    assert report.score(row)['proposed_exact_plan_match']


def test_split_job_requires_split_question_not_generic_ask(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    report=importlib.import_module('report_office_check')
    row={'id':'STARTER-en-10','language':'en',
         'proposed_labels':{'expected_issue':'too_many_steps','expected_steps':[]},
         'result':{'decision':'ask','refuse':None,'ask':{'event':'clarify_request'},'steps':[]}}
    assert not report.score(row)['proposed_exact_plan_match']
    row['result']['ask']['event']='split_job'
    assert report.score(row)['proposed_exact_plan_match']
