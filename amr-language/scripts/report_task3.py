#!/usr/bin/env python3
"""Generate task3 report from preserved live-run observations, never infer owner ratings."""
from collections import Counter
import json
from pathlib import Path
import statistics
import sys
import unicodedata
import html
import csv

from bench_utils import percentile,wilson
from measure_office import summarize

ROOT=Path(__file__).resolve().parent.parent


def ratio(value):
    if not value['total']: return 'not measured'
    lo,hi=value['wilson95']
    return f"{value['successes']}/{value['total']} ({lo*100:.1f}–{hi*100:.1f}%)"


def number(value,digits=2):
    return f'{value:.{digits}f}' if value is not None else 'not measured'


def escaped(text): return html.escape(str(text)).replace('|','/').replace('\n','<br>')


def raw_rewrite(row,pair):
    if 'rewrite_raw' in pair: return pair['rewrite_raw']
    call=next((c for c in row['calls'] if c['stage']=='rewrite' and c['index']==pair['index']),None)
    if call and call.get('output_text') is not None: return call['output_text']
    return pair.get('english','(no returned rewrite)')


def load_run(name):
    directory=ROOT/'records/task3'/name
    rows=[json.loads(l) for l in (directory/'requests.jsonl').read_text().splitlines()]
    resources=json.loads((directory/'summary.json').read_text())['resources']
    summary=summarize(rows,resources)
    (directory/'report_summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    # Include raw truncated/rejected outputs retained in call telemetry as well.
    with (directory/'rewrite_review.csv').open('w',newline='') as stream:
        writer=csv.writer(stream)
        writer.writerow(['id','source','language','status','draw','intended_english','model_raw_rewrite','validation_error','owner_meaning_yes_no','notes'])
        for row in rows:
            for pair in row['trace']:
                if 'index' in pair:
                    writer.writerow([row['id'],row['source'],row['language'],row['status'],pair['index'],row['intended_english'],
                                     raw_rewrite(row,pair),pair.get('error_detail',''),'',row['notes']])
    return rows,summary,json.loads((directory/'metadata.json').read_text())


def main():
    runs={c:load_run('final_'+c) for c in ('A','B')}
    a_sources={k:v for k,v in runs['A'][2]['source_sha256'].items() if k.startswith('amr_language/')}
    b_sources={k:v for k,v in runs['B'][2]['source_sha256'].items() if k.startswith('amr_language/')}
    if a_sources!=b_sources: raise ValueError('A/B package hashes differ')
    if runs['A'][2]['data_sha256']!=runs['B'][2]['data_sha256']: raise ValueError('A/B datasets differ')
    (ROOT/'records/task3/evaluated_package_hashes.json').write_text(json.dumps(a_sources,indent=2)+'\n')
    lines=['# Office language — trustworthy recheck, 2 November 2026','',
      '[checked] **The October stalled-server scores are superseded as model-quality evidence.** '
      'The owner review identified A from STARTER-ml-08 onward (13 requests) and B from STARTER-en-04 onward (37 requests) '
      'as unmeasured stalled-server periods. Their zero percentages are not model accuracy. '
      'Raw October files remain intact; see `records/task3/prior_run_audit.json` and the archived '
      '`OFFICE_LANGUAGE_CHECK_2026-10-28_HISTORICAL.md`. The attribution of B’s first old call to hashing remains likely, not proven.','',
      '[checked] New runs use the same 40 originals plus 30 authored STARTER-MIXED variants, separated below. '
      'No owner/friend rows have been added; none are invented. Labels, names/profile translations, reply lists and non-English '
      'templates remain pending native/owner review. These are small authored checks, not real-world accuracy.','',
      '[checked] Startup hashing/manifest checks, model loading and generation liveness probes are outside request timing. '
      'Each actual model call has a 60-second safety limit; the five-second product deadline is not used to abort this measurement. '
      'Whole request wall time includes four rewrites, distinct planning, source validation, grounding, identity checks and fixed readback. '
      'Before/after generation probes establish liveness; failed attempts are logged, restarted and retried once. '
      'Unmeasured cases are omitted from quality/latency denominators. See `TASK3_MEASUREMENT.md` for the contract.','',
      '[checked] Both final runs used identical package and data hashes, sequential execution, duplicate-plan reuse, context 4096, '
      'plan cap 768 and the existing 4-GiB reservation. Four rewrite draws remain; normalized duplicates are one distinct pair '
      'and do not supply independent votes. All draws must be valid and distinct plans agree before act, still requiring yes '
      'before a text rehearsal. Agreement is provisional and uncalibrated.','',
      '## True request latency and liveness','',
      '| Model | Source | Language | Measured / total | p50 / p95 ms | Within 5s (Wilson 95%) | Restarts | act / ask / refuse |',
      '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for c,(rows,summary,metadata) in runs.items():
        for key,r in summary['groups'].items():
            source,language=key.split(':');decisions=r['decisions']
            timing=f"{r['p50_ms']:.1f} / {r['p95_ms']:.1f}" if r['measured'] else 'not measured'
            lines.append(f"| {c} | {source} | {language} | {r['measured']}/{r['total']} | {timing} | {ratio(r['within_5s'])} | {r['restarts']} | {decisions.get('act',0)} / {decisions.get('ask',0)} / {decisions.get('refuse',0)} |")
    lines+=['','[checked] Median is the ordinary median, p95 nearest-rank (10 cases means the maximum). '
      'Within-five-second includes fast asks/refusals and therefore is **not** successful-job completion. '
      'An act result is not an owner correctness rating. Restart and missing counts are explicit rather than counted as model failures.','',
      '## Plan validity, empty plans and proposed grounding','',
      '| Model | Source | Language | Schema-valid / completed plan calls (Wilson 95%) | Empty steps + null issue | Proposed target match (Wilson 95%) | Exact proposed plan (Wilson 95%) |',
      '| --- | --- | --- | --- | --- | --- | --- |']
    for c,(_,summary,_) in runs.items():
        for key,r in summary['groups'].items():
            source,lang=key.split(':')
            lines.append(f"| {c} | {source} | {lang} | {ratio(r['plan_validity'])} | {r['empty_null_issue_plans']} | {ratio(r['proposed_targets_match'])} | {ratio(r['proposed_exact_plan_match'])} |")
    lines+=['','[checked] Plan validity is schema validation over completed plan-call responses, counting invalid parse/truncation '
      'as invalid. Empty steps with null issue is invalid; empty steps with an explicit supported refusal issue may be valid. '
      'Duplicates count once. Grounding/exact comparisons use **unconfirmed** proposed labels and require the expected decision/reason; '
      'technical refusals get no correctness credit. Exact match includes payload wording, so a different phrasing can fail exact '
      'match without establishing a meaning error. Owner meaning/step/name/long-request ratings remain **not rated**, with no '
      'human-score Wilson interval. Automated counts are not substitutes.','',
      '## Stage timings and reused plans','',
      '| Model | Source | Language | Rewrite wall p50 ms | Plan + grounding wall p50 ms | Grounding work p50 ms | Planned calls / 4-draw rewrite count | Reused plans |',
      '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for c,(rows,summary,_) in runs.items():
        for key,r in summary['groups'].items():
            source,lang=key.split(':');metrics=[p for row in rows if row['status']=='measured' and row['source']==source and row['language']==lang for p in row['trace'] if p.get('kind')=='pipeline_metrics']
            grounding=statistics.median(p['grounding_total_ms'] for p in metrics) if metrics else 0
            lines.append(f"| {c} | {source} | {lang} | {number(r['rewrite_p50_ms'])} | {number(r['plan_grounding_p50_ms'])} | {grounding:.3f} | {r['plan_call_responses']}/{r['rewrites']} | {r['plans_reused']} |")
    lines+=['','[checked] Every request and draw has rewrite_ms, plan_ms and grounding_ms in requests.jsonl. '
      'Stage wall time is distinct from summed per-call work during the parallel pilot. Startup/hash times below are separate; '
      'a cold caller that omits prepare_office still pays initialization inside its own elapsed call.','',
      '| Model | Startup hash + manifest ms | Model load ms | Peak total / visible compute GPU MiB | /api/ps size / size_vram bytes |',
      '| --- | --- | --- | --- | --- |']
    for c,(_,summary,metadata) in runs.items():
        directory=ROOT/'records/task3'/('final_'+c)
        events=[json.loads(l) for l in (directory/'runner_events.jsonl').read_text().splitlines()]
        load=next(e for e in events if e['kind']=='model_load')
        ps=json.loads((directory/'allocation.json').read_text())['ps']['models'][0];resource=summary['resources']
        lines.append(f"| {c} | {metadata['startup']['startup_hash_and_manifest_ms']:.3f} | {load['elapsed_ms']:.1f} | {resource['peak_total_gpu_mib']} / {resource['peak_all_compute_processes_gpu_mib']} | {ps['size']} / {ps['size_vram']} |")
    lines+=['','[checked] B’s size equals size_vram in /api/ps: this reports full model allocation on GPU, not CPU offload. '
      'It does not mean no CPU work occurs. There is no measured reason here to reduce the 4-GiB reservation, so it is unchanged. '
      'Resource sampling target 100 ms may miss brief spikes and includes other visible allocations. '
      '**[not checked] These are standalone measurements, not coexistence with a navigating Gazebo session.**','',
      '## Speed experiments','',
      '| Model | Configuration | Four-case wall p50 / p95 ms | Peak total GPU MiB | Reused plans | Requested / effective slots |',
      '| --- | --- | --- | --- | --- | --- |']
    for c in ('A','B'):
        for variant in ('sequential_full','sequential_dedup','parallel_dedup'):
            p=ROOT/'records/task3/execution_comparison'/(c+'_'+variant)
            rows=[json.loads(l) for l in (p/'requests.jsonl').read_text().splitlines()];summary=json.loads((p/'summary.json').read_text())
            times=[r['request_wall_ms'] for r in rows if r['status']=='measured']
            reused=sum(m.get('plans_reused',0) for r in rows for m in r['trace'] if m.get('kind')=='pipeline_metrics')
            allocation=json.loads((p/'allocation.json').read_text())
            actual=','.join(str(x['effective_parallel_slots']) for x in allocation.get('runner_processes',[])) or 'not captured'
            requested=4 if variant=='parallel_dedup' else 1
            lines.append(f"| {c} | {variant} | {statistics.median(times):.1f} / {percentile(times,.95):.1f} | {summary['resources']['peak_total_gpu_mib']} | {reused} | {requested} / {actual} |")
    lines+=['','[checked] Pilot cases: STARTER-en-01, en-07, STARTER-MIXED-ta-01 and STARTER-MIXED-ml-06. '
      'Sequential_full performs all four planner calls even for duplicate rewrites as a baseline; this is not evidence of four '
      'independent votes. Deduplication saves actual calls. Parallel requests were sent concurrently, but the installed runner '
      'still launched llama-server with **-np 1**, context 4096, despite OLLAMA_NUM_PARALLEL=4 and queue 8. '
      'The process arguments are captured for B_parallel_dedup. Similar timings therefore do not establish four-slot GPU '
      'performance. Pilot timings include invalid/refused outputs and do not establish better meaning or successful jobs. '
      'Production stays sequential/queue 1. Changing the runner version/backend would need a separate approved plan.','',
      '[checked] The valid supported six-delivery capacity probe used **327 generated tokens on A and 326 on B**, '
      'recorded as eval_count in `records/task3/plan_capacity/results.json`. Its post-call liveness passed. '
      'The 256-token cap would truncate it; retaining 768 is evidence-based. No short cap was used to hide long/invalid output.','',
      '## Cancellation and stall investigation','',
      '| Model | Stream | Queue | Deliberate abort after ms | Next hello ms | Recovered within 2s |',
      '| --- | --- | --- | --- | --- | --- |']
    cancel=json.loads((ROOT/'records/task3/cancellation_summary.json').read_text())
    for trial in cancel['experiments']:
        lines.append(f"| {trial['contestant']} | {trial['stream']} | {trial['queue']} | 1000 | {trial['probe_ms']:.3f} | {trial['recovered_under_2s']} |")
    lines+=['','[checked] All eight single-abort trials actually interrupted unfinished generations; streaming trials received '
      'partial chunks first. Every setup recovered without restart. The first diagnostic client had a concurrent-close race '
      '(AttributeError); that incomplete trial is preserved under cancellation_A, excluded from these results, and rerun using '
      'socket shutdown with HTTP close confined to the reader thread. This is a client-test correction, not a claimed server fix.']
    stress=ROOT/'records/task3/cancellation_stress/results.json'
    if stress.exists():
        trials=json.loads(stress.read_text())
        lines+=['',f"[checked] Additional real-adapter cancellation stress alternated rewrite/planner contexts: {len(trials)} trials, "
                 f"{sum(t['liveness']['ok'] for t in trials)} immediate liveness passes. See cancellation_stress/results.json. "
                 'These intentional cancellations are diagnostic only and excluded from quality scores.']
    lines+=['','[not checked] The precise cause of the October persistent stall remains unknown. The new evidence rules out '
      'a universal failure after a single one-second client abort; it does not rule out a context/cache/history-dependent bug. '
      'Streaming or a larger queue is not proven to fix the earlier stall. Accordingly, transport defaults and memory reservation '
      'were not changed on speculation. Measurement avoids five-second aborts and independently verifies generation liveness.','',
      '[checked] These experiments use owned server processes with stderr in project-local files, rather than the October '
      'long-lived tool-output session. The request ordering also changed to a rewrite phase followed by distinct planning. '
      '**[not checked] Whether logging backpressure or cache/history differences contributed to the old stall is not established.** '
      'Healthy new runs alone do not identify a causal server fix.','',
      '## Rewrite meaning review','',
      '[checked] Actual rewrites appear beside intended English for **every request**, with all unique draws and draw indices. '
      'Human rating cells are blank. These authored intended meanings are not an owner-approved gold set. '
      'Code-mixed inputs retain English office nouns/names and notes for known ambiguities; no training or owner data is invented.','',
      '| Model | en | ta | ml | te |','| --- | --- | --- | --- | --- |']
    recovery_path=ROOT/'records/task3/recovery_control/results.json'
    if recovery_path.exists():
        recovery=json.loads(recovery_path.read_text())
        lines[lines.index('## Rewrite meaning review'):lines.index('## Rewrite meaning review')]=[
          '[checked] Automatic recovery was exercised separately by terminating an **idle, owned** server, with no generation '
          'in flight. A and B each recovered through ensure_live with exactly one logged runner_restart and a successful '
          'generation afterward. These control-fault restarts are separate from the zero restart counts in the quality runs. '
          'Evidence: records/task3/recovery_control/results.json and its runner events.','']
    review_dir=ROOT/'docs/task3_rewrites';review_dir.mkdir(exist_ok=True)
    for c,(rows,_,_) in runs.items():
        for lang in ('en','ta','ml','te'):
            review=[f'# {c} / {lang} rewrite review','',
              '[checked] Authored STARTER/STARTER-MIXED material. Actual model outputs are review data, not speech. '
              'Owner meaning ratings remain blank; no semantic correctness is inferred.','',
              '| ID / source | Typed input | Intended English | Actual rewrites (draw indices) | Status / decision |',
              '| --- | --- | --- | --- | --- |']
            for r in rows:
                if r['language']!=lang: continue
                unique={}
                for pair in r['trace']:
                    if 'index' in pair:
                        raw=raw_rewrite(r,pair)
                        unique.setdefault(raw,[]).append(pair['index'])
                actual='<br>'.join(f"{indices}: {escaped(text)}" for text,indices in unique.items()) or '(no completed rewrite)'
                decision=r['result']['decision'] if r.get('result') else ''
                review.append(f"| {r['id']} / {r['source']} | {escaped(r['text'])} | {escaped(r['intended_english'])} | {actual} | {r['status']} / {decision} |")
            (review_dir/f'{c}_{lang}.md').write_text('\n'.join(review)+'\n')
        lines.append('| '+c+' | '+' | '.join(f'[{lang}](task3_rewrites/{c}_{lang}.md)' for lang in ('en','ta','ml','te'))+' |')
    lines+=['','[checked] Full four-draw CSVs: `records/task3/final_A/rewrite_review.csv` and B equivalent. '
      'Raw plans, grounding errors and stage times are in each requests.jsonl. Example review remains essential: '
      'agreement can repeat a wrong translation, and a schema-valid plan can still have the wrong meaning.','',
      '## Recommendation and C gate','',
      '[checked] The corrected harness and duplicate reuse make A/B measurements usable and reduce repeated planning work. '
      'They do not establish the four-language product target or correct rewrite meaning. **Do not deploy this as a seamless '
      'console replacement.** Keep the console’s existing rule-based path and explicit confirmation. Owner/native review, '
      'calibration, microphone and shared-load checks remain open.','',
      '[checked] C status: **waiting for approved C**. `docs/INSTALL_REQUEST_C.md` specifies an isolated project-local '
      'environment, all 75 package versions/wheel hashes and the exact distilled-200M model revision, download sizes, '
      'personal Hugging Face gate acceptance and removal. Only metadata was queried; no C package/model was downloaded '
      'or installed. CPU/GPU translation/planning quality, speed and memory are not measured. The task explicitly requires '
      'stopping for approval before this next stage.','',
      '[checked] Reproduction: `scripts/measure_office.py`, `compare_office_execution.py`, `check_runner_cancellation.py`, '
      '`check_plan_capacity.py`, `stress_runner_cancellation.py` and `report_task3.py`. Final request/source/data hashes '
      'are recorded under records/task3. Preliminary recheck_A/B are retained, but final_A/B are authoritative: rejected raw '
      'rewrites are preserved alongside validated English. This report recomputes both summaries from preserved calls using '
      'the same function and writes report_summary.json. Inference package hashes match across both final runs.']
    (ROOT/'docs/OFFICE_LANGUAGE_CHECK.md').write_text('\n'.join(lines)+'\n')
    print('Task3 report and all per-language rewrite review pages generated')


if __name__=='__main__': main()
