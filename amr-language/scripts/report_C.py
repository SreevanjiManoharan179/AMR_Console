#!/usr/bin/env python3
"""Report the completed frozen C campaign; human meaning ratings remain empty."""
from collections import Counter
from datetime import datetime
import csv
import hashlib
import html
import json
from pathlib import Path
import statistics
import sys

from bench_utils import percentile,wilson
ROOT=Path(__file__).resolve().parent.parent
BASE=ROOT/'records/task4_C_2026-11-03'
sys.path.insert(0,str(ROOT))
from amr_language.office import _agreement_key


def read(path):return json.loads(path.read_text())
def rows(path):return [json.loads(l) for l in path.read_text().splitlines()]
def escape(value):return html.escape(str(value)).replace('|','&#124;').replace('\n','<br>')
def timing(a,b):return 'not measured' if a is None else f'{a/1000:.2f}/{b/1000:.2f}'
def ratio(v):
    if not v['total']:return 'not measured'
    lo,hi=v['wilson95'];return f"{v['successes']}/{v['total']} ({lo*100:.1f}–{hi*100:.1f}%)"
def agreed(record):
    pairs=[p for p in record['trace'] if 'index' in p]
    return len(pairs)==2 and all(p.get('plan') and not p.get('error') and not p.get('uncertain') and p.get('issue') is None for p in pairs) and len({_agreement_key(p['plan']) for p in pairs})==1


def aggregates(records):
    measured=[r for r in records if r['status']=='measured'];times=[r['request_wall_ms'] for r in measured]
    translations=[r['C_call']['translation_ms'] for r in measured if r.get('C_call') and 'translation_ms' in r['C_call']]
    comparisons=[r['proposed_comparison'] for r in measured if r.get('proposed_comparison')]
    return {'rows':len(records),'measured':len(measured),'missing':len(records)-len(measured),
        'p50_ms':statistics.median(times) if times else None,'p95_ms':percentile(times,.95),
        'translation_p50_ms':statistics.median(translations) if translations else None,'translation_p95_ms':percentile(translations,.95),
        'within_5s':wilson(sum(t<=5000 for t in times),len(times)),
        'target_match':wilson(sum(c['proposed_targets_match'] for c in comparisons),len(comparisons)),
        'decisions':dict(Counter(r['result']['decision'] for r in measured if r.get('result'))),
        'draws':dict(Counter(r['result']['draws'] for r in measured if r.get('result'))),
        'questions':dict(Counter(r['result']['ask']['event'] for r in measured if (r.get('result') or {}).get('ask'))),
        'invalid_C_generation':sum(not bool((r.get('C_call') or {}).get('ok')) for r in measured),
        'supported_grounded_agreement':sum(agreed(r) for r in measured if r.get('result')),
        'C_validation_errors':dict(Counter(p.get('error') for r in measured for p in r['trace'] if p.get('index')==0 and p.get('error')))}


def main():
    metadata=read(BASE/'metadata.json');completion=read(BASE/'completion.json')
    assert completion['pip_cache_retained'] is True
    for path,digest in {**metadata['source_sha256'],**metadata['harness_sha256'],**metadata['data_sha256']}.items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,'Evaluated input changed: '+path
    phases={};overall={};worker_counts={};resource_audit={}
    for device in ('cpu','cuda'):
        for state in ('unloaded','loaded'):
            name=device+'_B_'+state;directory=BASE/name
            records=rows(directory/'requests.jsonl');summary=read(directory/'summary.json');meta=read(directory/'metadata.json')
            assert meta['source_sha256']==metadata['source_sha256'] and meta['data_sha256']==metadata['data_sha256']
            events=read(directory/'C_processes.json');worker_counts[name]=len(events)
            for event in events:
                assert event['command'][1]=='-I' and event['command'][0].endswith('/indictrans2-C/venv/bin/python')
                assert event['ready']['isolated'] and not event['ready']['ros_paths']
            assert len({r['id'] for r in records})==len(records)
            phases[name]=(records,summary,meta,events)
            overall[name]=aggregates(records)
            ready_ns=int(datetime.fromisoformat(events[0]['time']).timestamp()*1e9)
            samples=rows(directory/'resources.jsonl');warm=[r for r in samples if r['time_ns']>=ready_ns]
            pids={e['ready']['pid'] for e in events}
            warm_pids={p['pid'] for r in warm for p in r.get('compute_processes',[])}
            resource_audit[name]={'window':'samples at/after first C-ready event (warm workload and shutdown); no timed request occurs before C ready',
                'C_ready_time_ns':ready_ns,'raw_samples':len(samples),'warm_samples':len(warm),
                'warm_peak_total_gpu_mib':max((r.get('gpu_used_mib',0) for r in warm),default=None),
                'startup_inclusive_peak_gpu_mib':summary['resources']['peak_total_gpu_mib'],
                'C_pids':sorted(pids),'warm_compute_pids':sorted(warm_pids),
                'B_unloaded_confirmed_in_warm_samples':warm_pids<=pids if state=='unloaded' else None,
                'C_peak_allocated_bytes':max((r.get('C_call',{}).get('cuda_peak_allocated_bytes',0) for r in records if r.get('C_call')),default=0),
                'C_peak_reserved_bytes':max((r.get('C_call',{}).get('cuda_peak_reserved_bytes',0) for r in records if r.get('C_call')),default=0)}
            if state=='unloaded':assert warm_pids<=pids,'Unexpected compute process in B-unloaded warm window'
    identifiers=[r['id'] for r in phases['cpu_B_loaded'][0]]
    assert all([r['id'] for r in records]==identifiers for records,_,_,_ in phases.values())
    (BASE/'resource_audit.json').write_text(json.dumps(resource_audit,indent=2)+'\n')
    (BASE/'report_summary.json').write_text(json.dumps({'aggregate':overall,'C_process_counts':worker_counts,'resources':resource_audit,
        'pip_cache_retained':True,'source_commit_task4b':metadata['task4b_commit']},indent=2)+'\n')
    reference_A={r['id']:r for r in rows(ROOT/'records/task4/A_glossary_off/requests.jsonl')}
    reference_B={r['id']:r for r in rows(ROOT/'records/task4b/B_t07_complete_2026-11-03/requests.jsonl')}
    review=ROOT/'docs/task4_C_rewrites';review.mkdir(exist_ok=True)
    index={name:{r['id']:r for r in records} for name,(records,_,_,_) in phases.items() if name.endswith('_loaded')}
    with (BASE/'rewrite_plan_review.csv').open('w',newline='') as stream:
        writer=csv.writer(stream,lineterminator='\n');writer.writerow(['id','language','device','source','raw_rewrite','planner','raw_plan','grounded_plan','error','owner_meaning_rating'])
        for language in ('en','ta','ml','te'):
            page=[f'# C versus B/A — typed {language} review','',
                '[checked] Same authored inputs, proposed labels unconfirmed; no owner/native meaning score. C CPU and GPU translations are deterministic beam5, one draw each. Each is compared to a live B greedy rewrite. English C side is original-text passthrough. A and reference B rewrites are saved earlier runs, clearly identified. All model content is escaped screen-review data, never spoken payload.','']
            for identifier in identifiers:
                row=index['cpu_B_loaded'][identifier]
                if row['language']!=language:continue
                page += [f'## {identifier}','',f"Input: {escape(row['text'])}",'',f"Intended English (unconfirmed): {escape(row['intended_english'])}",'',
                    '| Device / source | Raw rewrite | Planner / raw plan | Grounded / error |','| --- | --- | --- | --- |']
                for name in ('cpu_B_loaded','cuda_B_loaded'):
                    record=index[name][identifier]
                    for pair in record['trace']:
                        if 'index' not in pair:continue
                        source=pair.get('rewrite_source','unknown');raw=pair.get('rewrite_raw')
                        detail={'grounded':pair.get('plan'),'error':pair.get('error_detail'),'issue':pair.get('issue'),'unknown':pair.get('uncertain')}
                        page.append(f"| {name} / {source} | {escape(raw)} | {pair.get('planner')} / {escape(json.dumps(pair.get('raw_plan'),ensure_ascii=False))} | {escape(json.dumps(detail,ensure_ascii=False))} |")
                        writer.writerow([identifier,language,name,source,raw,pair.get('planner'),json.dumps(pair.get('raw_plan'),ensure_ascii=False),json.dumps(pair.get('plan'),ensure_ascii=False),pair.get('error_detail',pair.get('issue')),''])
                    result=record.get('result') or {}
                    page += ['',f"{name}: {result.get('decision')}; draws {result.get('draws')}; wall {record.get('request_wall_ms')} ms. Fixed readback: {escape(result.get('readback'))}",'']
                for name,reference in [('A task4 word-list-off',reference_A),('B task4b 0.7 greedy',reference_B)]:
                    if identifier in reference:
                        pair=next((p for p in reference[identifier]['trace'] if p.get('index')==0),{})
                        page.append(f"{name} rewrite: {escape(pair.get('rewrite_raw'))}")
                page.append('')
            (review/f'{language}.md').write_text('\n'.join(page).rstrip()+'\n')
    lines=['# Task 4 C — fixed planner and cross-system agreement, 3 November 2026','',
        '[checked] Task 4b was completed and committed first as **5921fcb**, after 209 tests exited 0. Its grammar, names, clarification, schema, source guards, map checks and fixed speech are byte-identical in this C campaign. The C adapter adds an explicit two-source schedule; the ordinary B adaptive/default-temperature path is unchanged. No task 4/4b raw evidence was overwritten. No new model/package install, training, console edit, robot motion or push occurred.','',
        '[checked] C installation and pre-load custom-source review were already recorded in [C_INSTALLATION.md](C_INSTALLATION.md). This campaign reverified every pinned file against the immutable model revision and reviewed SHA-256 inventory before loading. All C imports/inference occurred in separate `.runtime/indictrans2-C/venv/bin/python -I` processes. Child metadata confirms isolation and no ROS paths. The caller never imports torch/transformers/IndicTransToolkit. Local files only, HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1, token=False; no token or authenticated network access is needed. Pip cache file count/bytes before and after match exactly.','',
        '## What was measured','',
        '[checked] One frozen campaign has four planned conditions, each using the same 70 authored requests: C CPU/B-unloaded translation-only, C CPU/B-loaded full pairing, C GPU/B-unloaded translation-only, C GPU/B-loaded full pairing. These are memory/device controls, not additional independent C votes. B-unloaded conditions do not run B planning/agreement, so their latency is translation + IPC only. Full-path quality and true request wall time are measured in the two B-loaded conditions. C uses CPU float32 or GPU float16, four CPU threads, eager attention, deterministic beam5 decoding, no sampling and a 256-token source/output cap. English input bypasses the Indic-to-English translator and is copied verbatim on the C side; its zero translation cost is not a translation capability claim.','',
        '[checked] For each full request: one C translation → fixed hybrid grammar/model fallback, and one live B greedy rewrite (temperature zero, original greedy seed) → the same planner. Identical rewrites share planning; duplicates do not vote independently. Both supported grounded plans must be valid and agree before act, with fixed readback and requester yes still mandatory. Disagreement/invalid output asks or refuses; there is no majority or extra random sampling. This two-system agreement is uncalibrated and not a correctness guarantee. B at temperature 0.7 remains the ordinary default from task 4b.','',
        '[checked] Startup hashing/model loading and pre/post generation liveness are outside request timing. Each inference has the existing 60-second safety limit; no five-second measurement abort. Failed infrastructure gets one owned-process recovery/retry, is counted and excluded if still unmeasured. Capacity/truncation/empty-output failures are completed model failures, not infrastructure or silently accepted partial text. Full wall timing includes IPC, C generation, B greedy generation, fallback planning, validation, grounding, map checks and fixed readback. Five-second attainment includes fast asks/refusals, not successful-job completion. p95 is nearest rank and per-ten-row p95 equals maximum.','',
        '| Condition | Measured / total | Time p50/p95 s | C translation p50/p95 s | Missing | C / B restarts | Warm / startup-inclusive total GPU peak MiB | C process lifetime peak RSS MiB |',
        '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for name,(records,s,_,events) in phases.items():
        g=overall[name]
        lines.append(f"| {name} | {g['measured']}/{g['rows']} | {timing(g['p50_ms'],g['p95_ms'])} | {timing(g['translation_p50_ms'],g['translation_p95_ms'])} | {g['missing']} | {s.get('C_restarts',0)} / {s.get('restarts',0)} | {resource_audit[name]['warm_peak_total_gpu_mib']} / {s['resources']['peak_total_gpu_mib']} | {s['C_process_peak_rss_kib']/1024:.1f} |")
    lines += ['', '| Paired condition | B size / size_vram bytes with C | C allocator peak allocated / reserved MiB |', '| --- | --- | --- |']
    for name in ('cpu_B_loaded','cuda_B_loaded'):
        allocation=read(BASE/name/'B_allocation_with_C.json')['ps']['models'][0];audit=resource_audit[name]
        lines.append(f"| {name} | {allocation['size']} / {allocation['size_vram']} | {audit['C_peak_allocated_bytes']/1048576:.1f} / {audit['C_peak_reserved_bytes']/1048576:.1f} |")
    lines += ['', '[checked] GPU memory is sampled at a 100-ms target interval and may miss brief spikes. The primary warm-state peak uses samples at/after C-ready, before which no timed request occurs. Startup-inclusive peaks are also retained. In cuda_B_unloaded, the first startup samples captured the previous owned B process releasing VRAM (3795 MiB inclusive); all warm samples contain only the C compute PID (617 MiB peak). This transition contamination is exposed rather than called C-only memory or triggering a C rerun. Exact windows/PID sets are in resource_audit.json. GPU totals include all visible allocations. C RSS is the child process lifetime high-water mark, including loading, not total system RAM or a translation-only increment. Raw samples and per-call C allocator peaks are retained. CPU/GPU precision differs as stated; this is not an otherwise identical precision speed comparison. B is loaded before C in paired phases to preserve its allocation, and /api/ps allocation snapshots are saved before/with/after C.','',
        '| Condition | Source / lang | C translation p50/p95 s | Complete C output / measured |', '| --- | --- | --- | --- |']
    for name,(_,s,_,_) in phases.items():
        for key,g in s['groups'].items():
            lines.append(f"| {name} | {key} | {timing(g['translation_p50_ms'],g['translation_p95_ms'])} | {g['valid_translations']}/{g['measured']} |")
    lines += ['', '[checked] Complete C output means nonempty, not truncated and within capacity; it is not a human translation rating or necessarily a valid English plan. English rows are passthrough. All 70 rows are authored STARTER variants; real OWNER rows: '+str(metadata['real_owner_rows'])+'. No blank row is counted as collected, and proposed labels remain pending.','',
        '## Full-path comparison and quality','',
        '| Path | Measured | Full p50/p95 s | Within 5s (Wilson 95%) | Proposed targets (Wilson 95%) | Draws | act/ask/refuse |',
        '| --- | --- | --- | --- | --- | --- | --- |']
    b_records=rows(ROOT/'records/task4b/B_t07_complete_2026-11-03/requests.jsonl')
    a_records=rows(ROOT/'records/task4/A_glossary_off/requests.jsonl')
    for name,records in [('A task4 hybrid v1 reference',a_records),('B task4b fixed hybrid 0.7',b_records)]+[(n,phases[n][0]) for n in ('cpu_B_loaded','cuda_B_loaded')]:
        g=aggregates(records);d=g['decisions']
        lines.append(f"| {name} | {g['measured']}/{g['rows']} | {timing(g['p50_ms'],g['p95_ms'])} | {ratio(g['within_5s'])} | {ratio(g['target_match'])} | {g['draws']} | {d.get('act',0)}/{d.get('ask',0)}/{d.get('refuse',0)} |")
    lines += ['', '[checked] A is the saved task 4/v1 hybrid reference, not a new fixed-v2 A run. B is the completed fixed task 4b run. This is a pipeline comparison, including different draw schedules, not an isolated translation-only ablation. Proposed targets require the unconfirmed expected decision/reason and target sequence; they do not verify translated items/messages/questions/clock meaning. No owner/native rating or real-world accuracy is inferred.','',
        '| C full path | Source / lang | Full p50/p95 s | Within 5s (Wilson 95%) | Proposed targets (Wilson 95%) | Grammar/model pairs | Grammar-only requests | Schema-valid (Wilson 95%) | Empty null plans | Draws |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for name in ('cpu_B_loaded','cuda_B_loaded'):
        for key,g in phases[name][1]['groups'].items():
            lines.append(f"| {name} | {key} | {timing(g['p50_ms'],g['p95_ms'])} | {ratio(g['within_5s'])} | {ratio(g['target_match'])} | {g['grammar_pairs']}/{g['model_pairs']} | {g['grammar_only_requests']}/{g['measured']} | {ratio(g['schema_valid'])} | {g['empty_null_plans']} | {g['draws']} |")
    lines += ['', '[checked] Planner shares/validity count distinct attempted grammar/model pairs; identical rewrite reuse counts once. Invalid rewrites with no plan do not enter that denominator. Valid refusal schemas can be counted as valid, and validity does not establish meaning. V2 grammar holdout coverage remains the same frozen task 4b result (31/36 v1 holdout, 30/36 v2 holdout); no grammar tuning occurred for C.','',
        '| C path | First question event | Requests |','| --- | --- | --- |']
    for name in ('cpu_B_loaded','cuda_B_loaded'):
        for event,count in sorted(overall[name]['questions'].items()):lines.append(f'| {name} | {event} | {count} |')
    lines += ['', '| C path | Valid supported grounded C/B agreement | C generation errors | C validation errors |', '| --- | --- | --- | --- |']
    for name in ('cpu_B_loaded','cuda_B_loaded'):
        g=overall[name];lines.append(f"| {name} | {g['supported_grounded_agreement']}/{g['measured']} | {g['invalid_C_generation']} | {g['C_validation_errors']} |")
    lines += ['', '[checked] Supported agreement is tallied before collection/map guards; agreeing candidate plans may still require a collection clarification instead of act. Agreement failure often reflects different payload wording or names, not a known meaning error. Conversely, agreement can preserve a wrong interpretation. Against the authored unconfirmed reference STARTER-ta-09 (“Make tea”), C returned “Give me some tea” and B greedy “Please give me tea”; both grounded to delivery and the paired result was act pending requester yes. The ordinary 0.7 B run asked after its four draws. The item is outside the fixed spoken lexicon, so this readback uses the existing generic item phrase; exact plan content remains on screen. This example blocks any claim that cross-system agreement establishes correct intent. No robot action occurred. Native/owner confirmation is still needed.', '']
    lines += ['', 'Side-by-side C CPU/GPU translations, live B greedy rewrites, raw/grounded plans, errors and earlier A/B rewrites: [English](task4_C_rewrites/en.md), [Tamil](task4_C_rewrites/ta.md), [Malayalam](task4_C_rewrites/ml.md), [Telugu](task4_C_rewrites/te.md). Machine review sheet: records/task4_C_2026-11-03/rewrite_plan_review.csv. Raw per-case records include every attempted call, liveness result and recovery.','',
        '## Recommendation','',
        read(BASE/'recommendation.json')['text'],'',
        '[not checked] Native/owner meaning and fixed speech review, real microphone/typed client commands, confirmed labels/live map facts and shared GPU use while Gazebo navigates remain open. No live console adapter, speech model, navigation test or robot dispatch was added. Mandatory readback/yes and fixed speech with arbitrary content on screen remain the guard against two systems agreeing on a wrong meaning.','']
    (ROOT/'docs/TASK4_C_CHECK.md').write_text('\n'.join(lines).rstrip()+'\n')
    print('Generated C comparison, four rewrite-review pages and resource audit; frozen hashes and isolated processes verified.')


if __name__=='__main__':main()
