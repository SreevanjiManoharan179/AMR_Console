#!/usr/bin/env python3
"""Reanalyse preserved STARTER observations; never run inference or confirm labels."""
from collections import Counter
import json
from pathlib import Path
import shutil
import statistics
import sys

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.calibration import file_sha256
from amr_language.grounding import fingerprint
from amr_language.office_schema import normalized_plan,walk_steps
from bench_utils import percentile,wilson
from check_office_language import shape,targets


def score(row):
    result=row['result']; label=row['proposed_labels']; issue=label['expected_issue']
    expected=normalized_plan({'steps':label['expected_steps'],'issue':None},'Bharathwaj')['steps'] if label['expected_steps'] else []
    if issue=='unknown_name':
        match=result['decision']=='ask' and (result['ask'] or {}).get('event') in ('ask_which','unknown_name')
    elif issue=='too_many_steps':
        match=result['decision']=='ask' and (result['ask'] or {}).get('event')=='split_job'
    elif issue:
        match=result['decision']=='refuse' and (result['refuse'] or {}).get('reason')==issue
    else:
        match=result['decision']=='act'
    return {'id':row['id'],'language':row['language'],
      'proposed_structure_match':bool(match and (issue or shape(result['steps'])==shape(expected))),
      'proposed_targets_match':bool(match and (issue or targets(result['steps'])==targets(expected))),
      'proposed_exact_plan_match':bool(match and (issue or fingerprint(result['steps'])==fingerprint(expected))),
      'supported_long':not issue and sum(1 for _ in walk_steps(expected))>=3,
      'reason':(result['refuse'] or result['ask'] or {}).get('reason') or
               (result['refuse'] or result['ask'] or {}).get('event') or 'proposed plan differs'}


def proportion(value):
    n=value['total']
    if not n: return 'not rated (0 observations)'
    low,high=value['wilson95']
    return f"{value['successes']}/{n} ({low*100:.1f}–{high*100:.1f}%)"


def main():
    source={str(p.relative_to(ROOT)):file_sha256(p) for p in sorted((ROOT/'amr_language').glob('*')) if p.is_file()}
    code_hash=fingerprint(source)
    snapshot=ROOT/'records/office_checks/v3_evaluated_source'
    snapshot.mkdir(exist_ok=True)
    for relative,digest in source.items():
        dest=snapshot/relative;dest.parent.mkdir(parents=True,exist_ok=True)
        if dest.exists() and file_sha256(dest)!=digest: raise ValueError('Existing snapshot differs')
        shutil.copyfile(ROOT/relative,dest)
    (snapshot/'sha256.json').write_text(json.dumps({'code_sha256':code_hash,'files':source},indent=2)+'\n')
    lines=['# Office language v3 — small check, not a benchmark','',
      '[checked] Recommendation: **not good enough yet** for the requested four-language office workflow. '
      'A produced three act plans out of 40 authored starters (all English); B timed out on all 40 at the five-second setting. '
      'These are development observations, not real-world accuracy. Retain the console’s existing rule-based command path pending review.','',
      '[checked] The owner approved B and explicitly chose **Keep labels pending**. There are 40 authored STARTER requests '
      '(10 per language), 40 blank OWNER rows and 99 authored short-reply fixtures. Real owner/friend commands collected: **0**. '
      'Meaning, steps, names and long-request success have **no owner ratings**. No confirmed SHA freeze or calibrated coverage is claimed.','',
      '[checked] A and B used the same package, roster, starter sheets and provisional agreement rule: one greedy rewrite plus '
      'three at temperature .7, each planned greedily and grounded. All four must agree for act, followed by yes before rehearsal. '
      'The earlier two-second target is retired; this task uses p95 <=5 seconds. Tests do not establish model accuracy.','',
      '[checked] Each run was warmed beforehand. Latency below is from the understand call through its terminal result, **including '
      'refusals/timeouts**, and is not successful-readback latency for those cases. Median is the ordinary median; p95 is nearest-rank '
      '(with 10 cases it is the maximum). Initial file verification remains in timing. Synchronous operations can exceed the socket '
      'budget; the CLI/logging wall time can also exceed the reported function time. No hard real-time bound is claimed.','',
      '[checked] Proposed structure compares action order and branch shape; proposed targets compare grounded ID slots and alternate '
      'recipients. Exact plan additionally compares every normalized field, including content/policies. For expected asks/refusals, '
      'only the matching reason/event counts. All labels are unconfirmed. These machine comparisons are **not** owner judgments of '
      'meaning, steps or names. Wilson 95% intervals describe these small fixture proportions only, not calibrated coverage.','',
      '[checked] The raw run summaries used a loose decision-only score for expected refusals. This report and each analysis.json '
      'correct that bookkeeping using preserved outputs: timeouts/invalid plans are not credited as out-of-scope recognition. '
      'Raw requests, metadata and original summary files remain unchanged. The reproduction harness now uses reason-aware scoring.','',
      '## A/B observations','',
      '| Model | Language | Proposed structure + targets¹ | Exact proposed plan¹ | act / ask / refuse | Terminal p50 / p95 ms | Act-only p50 / p95 ms | Peak GPU MiB² |',
      '| --- | --- | --- | --- | --- | --- | --- | --- |']
    runs={}
    for contestant in ('A','B'):
        directory=ROOT/f'records/office_checks/{contestant}_v3_final'
        metadata=json.loads((directory/'metadata.json').read_text())
        if metadata['code_sha256']!=code_hash: raise ValueError('Evaluated package changed: '+contestant)
        for filename,key in [('office_names_example.yaml','names_sha256'),('owner_check.csv','commands_sha256'),('replies_check.csv','replies_sha256'),('owner_labels_proposed.json','labels_sha256')]:
            if file_sha256(ROOT/'data'/filename)!=metadata[key]: raise ValueError('Evaluated data changed')
        rows=[json.loads(line) for line in (directory/'requests.jsonl').read_text().splitlines()]
        summary=json.loads((directory/'summary.json').read_text())
        analysed=[score(r) for r in rows]
        analysis={'method':'reason-aware reanalysis of preserved outputs; owner labels pending',
                  'source_requests_sha256':file_sha256(directory/'requests.jsonl'),'by_language':{},'rows':analysed}
        for lang in ('en','ta','ml','te'):
            group=[r for r in rows if r['language']==lang]; scores=[r for r in analysed if r['language']==lang]
            times=[r['result']['latency_ms'] for r in group]; acts=[r['result']['latency_ms'] for r in group if r['result']['decision']=='act']
            both=wilson(sum(r['proposed_structure_match'] and r['proposed_targets_match'] for r in scores),len(scores))
            exact=wilson(sum(r['proposed_exact_plan_match'] for r in scores),len(scores))
            long=[r for r in scores if r['supported_long']]
            counts=Counter(r['result']['decision'] for r in group)
            analysis['by_language'][lang]={'proposed_structure_and_targets_match':both,'proposed_exact_plan_match':exact,
                'supported_long_exact_match':wilson(sum(r['proposed_exact_plan_match'] for r in long),len(long)),
                'meaning_owner_rating':wilson(0,0),'steps_owner_rating':wilson(0,0),'names_owner_rating':wilson(0,0),
                'long_owner_rating':wilson(0,0),'act_readback_count':len(acts),
                'act_p50_ms':statistics.median(acts) if acts else None,'act_p95_ms':percentile(acts,.95)}
            act_time=f'{statistics.median(acts):.1f} / {percentile(acts,.95):.1f} (n={len(acts)})' if acts else 'none'
            lines.append(f"| {contestant} | {lang} | {proportion(both)} | {proportion(exact)} | {counts['act']} / {counts['ask']} / {counts['refuse']} | {statistics.median(times):.1f} / {percentile(times,.95):.1f} | {act_time} | {summary['resources']['peak_total_gpu_mib']} / {summary['resources']['peak_all_compute_processes_gpu_mib']} |")
        (directory/'analysis.json').write_text(json.dumps(analysis,ensure_ascii=False,indent=2)+'\n')
        runs[contestant]=(rows,summary,analysis)
    lines+=['','[checked] ¹ Count / 10 with Wilson 95% interval; structure and targets scores happen to agree here. '
      '² Peak whole-device / all visible compute-process allocation across the **whole contestant run**, repeated by language; '
      'not a language-specific peak and not guaranteed to isolate model ownership. Sample interval target 100 ms; spikes may be missed. '
      'A total 2341 MiB (2.29 GiB), B total 3787 MiB (3.70 GiB). **[not checked] <=4 GB while Gazebo runs**: '
      'no console navigation session was established, and no model-generated job was dispatched.','',
      '## Owner ratings and short replies','',
      '| Language | A meaning / steps / names / long owner ratings | B same | R same | Word-list fixture match (shared, not model-specific) | A reply p50 / p95 ms | B reply p50 / p95 ms |',
      '| --- | --- | --- | --- | --- | --- | --- |']
    for lang in ('en','ta','ml','te'):
        a=runs['A'][1]['by_language'][lang];b=runs['B'][1]['by_language'][lang]
        lines.append(f"| {lang} | not rated (0) | not rated (0) | not run (0) | {proportion(a['replies_fixture_match'])} | {a['reply_p50_ms']:.4f} / {a['reply_p95_ms']:.4f} | {b['reply_p50_ms']:.4f} / {b['reply_p95_ms']:.4f} |")
    lines+=['','[checked] The same 99 fixed-list fixtures passed in both runs. They meet the one-second target for these typed '
      'phrases only; they do not measure microphone recognition, unlisted wording, or free-answer rewrite latency. '
      'All non-English templates, names and reply lists still need native-speaker check. With zero human ratings, Wilson intervals '
      'for meaning, actual step/name correctness and long-request success are undefined, not zero accuracy.','',
      '| Model | Language | Exact proposed supported-long plans (unconfirmed labels, Wilson 95%) |',
      '| --- | --- | --- |']
    for contestant in ('A','B'):
        for lang,data in runs[contestant][2]['by_language'].items():
            lines.append(f"| {contestant} | {lang} | {proportion(data['supported_long_exact_match'])} |")
    lines+=['','## Observed failures','',
      '[checked] Up to five observed failures per language and contestant follow. Intended English text below comes from '
      'authored expected_english notes, **not** a claim that the model produced it or that an owner approved it. '
      'Original inputs, rewrites, every pair and invalid-plan details are in the corresponding requests.jsonl.']
    for lang in ('en','ta','ml','te'):
        lines+=['',f'### {lang}','','| Model / ID | Starter intent | Observed reason |','| --- | --- | --- |']
        for contestant in ('A','B'):
            rows,_,analysis=runs[contestant]; by_id={r['id']:r for r in rows}
            failed=[r for r in analysis['rows'] if r['language']==lang and not r['proposed_exact_plan_match']][:5]
            for row in failed:
                original=by_id[row['id']]
                intent=original['expected_english'].replace('|','/').replace('\n',' ')
                details=next((pair.get('error_detail') for pair in original['trace'] if pair.get('error_detail')),None)
                reason=row['reason']+((': '+details) if details else '')
                lines.append(f"| {contestant} / {row['id']} | {intent} | {reason} |")
    lines+=['','## Additional evidence and open work','',
      '[checked] Model A completed a scripted English delivery and a code-mixed Tamil delivery with explicit yes and taken: '
      '`records/rehearsals/job_20261028_163849_667987.jsonl` and `job_20261028_164041_235231.jsonl`. '
      'These used a **30-second diagnostic limit**, not the target check, and scripted authored replies, not owner collection. '
      'A separate native-script Tamil command needed clarification (`command_20261028_163720_844198.json`). '
      'B’s first 30-second diagnostic delivery also timed out (`job_20261028_171529_527824.jsonl`); it is not a successful job. '
      'A subsequent simple hello diagnostic timed out after 15 seconds. After explicitly unloading/reloading B, hello completed '
      'in 311.3 ms and the same scripted delivery completed in **14,067.1 ms** '
      '(`job_20261028_172129_270522.jsonl`, 30-second diagnostic setting). These observations show a runner recovery issue after '
      'timeouts as well as unacceptable end-to-end latency; the precise cause is not established. The 40-request B run therefore '
      'measures this installed stack under repeated deadlines, not B’s standalone translation quality. '
      'Queue, alternatives, retrieval, branches, deferral and STOP have mock flow tests, not real console demonstrations.','',
      '[checked] Separate empty-prompt warm-up records (A_warmup.json / B_warmup.json) include load times and resources. '
      'These are not request-to-readback timings. The five-second small check remains unsuccessful regardless of diagnostic limits.','',
      '[not checked] R: **cloud reference, not the product**. Browser security rejected chatgpt.com because origin permission was '
      'declined. No workaround or cloud API was used. No model outputs/timings/ratings exist for any of the four languages, '
      'so no percentages or intervals can be computed. Same prompt text is prepared in `data/cloud_reference_prompts.md`; '
      '`data/cloud_reference_results.csv` is header-only. Browser sampling controls may not match local settings.','',
      '[not checked] B plus IndicTrans2 has not been run and cannot be recommended from evidence. The translation note describes '
      'an optional future install only; no speech or translation model was downloaded. No training is proposed.','',
      '[likely] Next: collect ordinary owner/friend requests through this typed rehearsal without training, review rewrites/steps '
      'and native templates, then decide whether a translation step or a revised sampling/runtime design merits another check. '
      'Do not promote these starter scores into deployment accuracy. The user requested labels stay pending, so no confirmation '
      'or real data is fabricated. A future microphone worker can supply transcripts after separate install approval and testing.','',
      '[checked] Reproduction artifacts: `records/office_checks/A_v3_final/` and B equivalent contain metadata, raw requests, '
      'replies, resources, original summary, corrected analysis and blank rating sheets. Both package code hashes match '
      '`records/office_checks/v3_evaluated_source/sha256.json`; code snapshot copied after the runs and checked against recorded '
      'pre-run hashes. Dataset hashes in metadata are pending drift checks, not an owner-confirmed freeze. '
      'Inventories record every installed model file SHA. `python3 scripts/report_office_check.py` regenerates this report '
      'from preserved observations; it performs no inference.']
    (ROOT/'docs/OFFICE_LANGUAGE_CHECK.md').write_text('\n'.join(lines)+'\n')
    print('Generated reason-aware report; labels remain pending; package hashes match both runs')


if __name__=='__main__': main()
