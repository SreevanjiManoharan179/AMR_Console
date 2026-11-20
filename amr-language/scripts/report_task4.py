#!/usr/bin/env python3
"""Render task 4 evidence without inventing human meaning ratings or C results."""
from collections import Counter
import csv
import hashlib
import html
import json
from pathlib import Path
import statistics
from bench_utils import percentile, wilson

ROOT=Path(__file__).resolve().parent.parent
BASE=ROOT/'records/task4'


def read(path): return json.loads(path.read_text())
def rows(path): return [json.loads(line) for line in path.read_text().splitlines()]
def esc(value): return html.escape(str(value)).replace('|','&#124;').replace('\n','<br>')
def ratio(value):
    if not value['total']: return 'not measured'
    lo,hi=value['wilson95']
    return f"{value['successes']}/{value['total']} ({100*lo:.1f}–{100*hi:.1f}%)"
def timing(group): return f"{group['p50_ms']/1000:.2f} / {group['p95_ms']/1000:.2f}" if group['p50_ms'] is not None else 'not measured'
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    runs={}
    for c in ('A','B'):
        for glossary in ('off','on'):
            name=f'{c}_glossary_{glossary}';d=BASE/name
            runs[name]=(rows(d/'requests.jsonl'),read(d/'summary.json'),read(d/'metadata.json'))
    first=next(iter(runs.values()))[2]
    for _,_,metadata in runs.values():
        assert metadata['source_sha256']==first['source_sha256'],'Package differed across runs'
        assert metadata['data_sha256']==first['data_sha256'],'Data differed across runs'
        assert metadata['real_owner_rows']==first['real_owner_rows']
    for path,digest in first['source_sha256'].items(): assert sha(ROOT/path)==digest,'Measured package changed: '+path
    for path,digest in first['data_sha256'].items(): assert sha(ROOT/path)==digest,'Measured data changed: '+path
    (BASE/'evaluated_package_hashes.json').write_text(json.dumps(first['source_sha256'],indent=2)+'\n')
    coverage=read(BASE/'grammar_coverage_v1/summary.json')
    assert sha(ROOT/'data/grammar_holdout.csv')==coverage['candidate']['holdout_sha256']
    assert sha(ROOT/'amr_language/office_grammar.py')==coverage['candidate']['grammar_sha256']
    aggregate={}
    for name,(records,summary,_) in runs.items():
        measured=[r for r in records if r['status']=='measured']; times=[r['request_wall_ms'] for r in measured]
        aggregate[name]={'measured':len(measured),'rows':len(records),'missing':summary['missing'],'restarts':summary['restarts'],
            'p50_ms':statistics.median(times),'p95_ms':percentile(times,.95),
            'within_5s':wilson(sum(r['within_5s'] for r in measured),len(measured)),
            'draws':dict(Counter(r['result']['draws'] for r in measured)),
            'proposed_target_match':wilson(sum(r.get('proposed_comparison',{}).get('proposed_targets_match',False) for r in measured),len(measured)),
            'decisions':dict(Counter(r['result']['decision'] for r in measured)),
            'resources':summary['resources']}
    (BASE/'comparison_summary.json').write_text(json.dumps(aggregate,indent=2)+'\n')
    review=ROOT/'docs/task4_rewrites';review.mkdir(exist_ok=True)
    for language in ('en','ta','ml','te'):
        sample=next(iter(runs.values()))[0]
        ids=[r['id'] for r in sample if r['language']==language]
        indexed={name:{r['id']:r for r in records} for name,(records,_,_) in runs.items()}
        page=[f'# Task 4 typed {language} review','',
              '[checked] Authored STARTER inputs, proposed labels unconfirmed. No OWNER rows. Every draw is retained, including invalid rewrites and reused plans. These are escaped model data for screen review only. No native meaning rating is implied. C office comparison: not measured; see C_INSTALLATION.md for installation status.','']
        for identifier in ids:
            source=indexed['A_glossary_off'][identifier]
            page += [f'## {identifier}','',f"Input: {esc(source['text'])}",'',f"Intended English (unconfirmed): {esc(source['intended_english'])}",'',
                     '| Path | Draw / planner | Raw English rewrite | Raw v3 plan | Grounded plan / validation |',
                     '| --- | --- | --- | --- | --- |']
            for name in runs:
                row=indexed[name][identifier]
                for pair in row['trace']:
                    if 'index' not in pair:continue
                    raw=pair.get('rewrite_raw',pair.get('english','(no returned rewrite)'))
                    detail={'grounded':pair.get('plan'),'issue':pair.get('issue'),'uncertain':pair.get('uncertain'),
                            'error':pair.get('error'),'detail':pair.get('error_detail'),'reused_from':pair.get('plan_reused_from')}
                    page.append(f"| {name} | {pair['index']} / {pair.get('planner')} | {esc(raw)} | {esc(json.dumps(pair.get('raw_plan'),ensure_ascii=False))} | {esc(json.dumps(detail,ensure_ascii=False))} |")
                result=row['result'] or {}
                page+=['',f"{name}: {result.get('decision')}; {result.get('draws')} draws; {row['request_wall_ms']/1000:.3f}s; proposed target match {row.get('proposed_comparison',{}).get('proposed_targets_match')}. Readback: {esc(result.get('readback'))}",'']
        (review/f'{language}.md').write_text('\n'.join(page).rstrip()+'\n')
    with (BASE/'rewrite_plan_review.csv').open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['id','source','language','path','draw','raw_rewrite','planner','raw_plan','grounded_plan','error','owner_meaning_rating'])
        for name,(records,_,_) in runs.items():
            for row in records:
                for pair in row['trace']:
                    if 'index' in pair:writer.writerow([row['id'],row['source'],row['language'],name,pair['index'],pair.get('rewrite_raw',''),pair.get('planner'),json.dumps(pair.get('raw_plan'),ensure_ascii=False),json.dumps(pair.get('plan'),ensure_ascii=False),pair.get('error_detail',pair.get('issue')),''])
    text=['# Task 4 — hybrid planner and map-aware rehearsal, 2 November 2026','',
        '[checked] Version 0.4.0 is a separate module for typed input with a small local language model and text rehearsal in simulation. It does not dispatch robot tasks. The authoritative owner task is 11,522 bytes, SHA-256 bb2b3e8a2b8483f291680e861ac1aee7ea16f2f5e482415dd3cd0125d9a1fbbd, including section 1b.','',
        '[checked] Section 0 is consistent with the raw task 3 planning failures. The supplied Claude rewrite ratings are a non-native review, not new owner/native measurements by this check. Translation errors can survive agreement. The cup/file example is why fixed readback and explicit requester yes stay mandatory; cup now has a fixed readback lexicon entry so it is not hidden behind a generic item label. Arbitrary messages/questions/answers still appear on screen only, following the owner’s speech policy.','',
        '## Why the seven check requests were refused','',
        '[checked] All 28 draws had schema-valid plans with on_answer:null and branch actions flattened into unconditional steps. None reached grounding. In en-06, ml-06 and te-06 every draw failed the on_answer/source-condition guard. In ta-06, draws 0/1/3 first failed invented message text "null"; draw 2 failed the condition guard. In all three MIXED-06 cases each draw first failed a question reordered from its literal source span. This was a model planning error, correctly rejected; no schema/grounding rule was relaxed. The grammar now builds nested branches and copies source question text. Two genuine guard omissions (if so/if not branches and contracted absence wording) were fixed with bounded phrases, not general condition support. See [per-case audit](TASK4_BRANCH_AUDIT.md) and records/task4/branch_refusal_audit.json.','',
        '## Measurement and denominators','',
        '[checked] Same 70 authored rows: 40 original STARTER and 30 STARTER-MIXED. Four new standalone runs use identical package/data hashes, hybrid grammar-first planning, adaptive draws, context 4096, rewrite/plan caps 384/768, seed 1729, sequential calls and duplicate-plan reuse. A/B are the existing Qwen3.5 2B/4B Q4_K_M artifacts. No new download/install was performed. Real OWNER rows: '+str(first['real_owner_rows'])+'. Blank owner rows are not collected commands. Owner labels and native text remain pending.','',
        '[checked] Startup hashes/model loading and pre/post generation liveness probes are untimed. Measurement uses a 60-second per-call safety limit, no five-second aborts, one infrastructure restart/retry if necessary, and excludes/counts unmeasured cases. Whole request timing includes the actual rewrite/planning/validation/grounding/map checks/readback. Every run records liveness, allocation, resources and per-draw stages under records/task4. Five-second attainment includes quick asks/refusals; it is not successful task completion. p95 uses nearest rank; at ten rows it is the maximum. Wilson 95% intervals describe these authored samples only.','',
        '**[checked] Two agreeing grammar-led draws are a weaker check than four. Agreement is still uncalibrated; readback with yes remains mandatory.** A failed/disagreeing first pair expands to four, with all used draws valid and all distinct grounded plans agreeing; no majority vote. The model planner runs only if the grammar returns no parse. A parsed but invalid/ungrounded plan never triggers a model rescue or authorizes act.','',
        '| Path | Measured / total | p50 / p95 s | Within 5s (Wilson 95%) | Draws 2 / 4 | Proposed target match (Wilson 95%) | Missing / restarts | Peak total GPU MiB |',
        '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for name,g in aggregate.items():
        text.append(f"| {name} | {g['measured']}/{g['rows']} | {timing(g)} | {ratio(g['within_5s'])} | {g['draws'].get(2,g['draws'].get('2',0))} / {g['draws'].get(4,g['draws'].get('4',0))} | {ratio(g['proposed_target_match'])} | {g['missing']} / {g['restarts']} | {g['resources']['peak_total_gpu_mib']} |")
    text+=['','[checked] The list raised the aggregate proposed target match by two requests for each model, while reducing within-five-second counts by one for A and two for B. B mixed Tamil regressed from 7/10 to 4/10 proposed targets. This is not consistent evidence for enabling it by default.','',
        '| Conditional check case | B no list (draws) | B list (draws) |', '| --- | --- | --- |']
    off={r['id']:r for r in runs['B_glossary_off'][0]};on={r['id']:r for r in runs['B_glossary_on'][0]}
    for identifier in off:
        if not identifier.endswith('-06'):continue
        a=off[identifier]['result'];b=on[identifier]['result']
        text.append(f"| {identifier} | {a['decision']} ({a['draws']}) | {b['decision']} ({b['draws']}) |")
    text+=['','[checked] The English conditional request now reaches act with nested branches in both settings. Four of seven B/no-list cases and five of seven B/list cases reach act; the others ask because rewrite/payload/planning differences still prevent agreement. Fixing branch construction does not establish correct translation in every language.','',
        '## Old and new, by language','',
        '| Model | Source / lang | Task 3 p50 / p95 s | Hybrid no list p50 / p95 s | Hybrid list p50 / p95 s | Within 5s: old → off → on | Proposed targets: old → off → on |',
        '| --- | --- | --- | --- | --- | --- | --- |']
    for c in ('A','B'):
        old=read(ROOT/f'records/task3/final_{c}/report_summary.json')['groups']
        for key,o in old.items():
            off=runs[f'{c}_glossary_off'][1]['groups'][key];on=runs[f'{c}_glossary_on'][1]['groups'][key]
            small=lambda v:f"{v['successes']}/{v['total']}"
            text.append(f"| {c} | {key} | {timing(o)} | {timing(off)} | {timing(on)} | {small(o['within_5s'])} → {small(off['within_5s'])} → {small(on['within_5s'])} | {small(o['proposed_targets_match'])} → {small(off['target_match'])} → {small(on['target_match'])} |")
    text+=['','[checked] Old is task 3’s final healthy liveness-gated four-draw model-planner run, not the superseded stalled October run. Comparison includes the combined hybrid/adaptive/source-guard changes; it does not isolate grammar from draw reduction. The new map facts are explicitly scripted, with the same approved IDs/aliases; they are not verified Gazebo routes. Ten cases per group give wide intervals; there is one run per setting, not repeated randomized timing trials.','',
        '## Planner use, validity and draws','',
        '| Path | Source / lang | Draws 2 / 4 | Distinct grammar / model plans (grammar %) | Grammar-only requests | Schema-valid (Wilson 95%) | Empty steps + null issue | Within 5s (Wilson 95%) | Proposed targets (Wilson 95%) |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for name,(_,s,_) in runs.items():
        for key,g in s['groups'].items():
            n=g['grammar_pairs']+g['model_pairs'];share=100*g['grammar_pairs']/n if n else 0
            text.append(f"| {name} | {key} | {g['draws'].get('2',0)} / {g['draws'].get('4',0)} | {g['grammar_pairs']} / {g['model_pairs']} ({share:.1f}%) | {g['grammar_only_requests']}/{g['measured']} | {ratio(g['schema_valid'])} | {g['empty_null_plans']} | {ratio(g['within_5s'])} | {ratio(g['target_match'])} |")
    text+=['','[checked] Planner shares and schema validity count distinct attempted grammar/model plan pairs; reused plans count once. Invalid rewrites without a plan are excluded from that plan denominator, and malformed model output counts invalid. Explicit supported refusal plans can be schema-valid. Validity is not correct meaning. Proposed target matching requires the proposed decision/reason and target sequence; it does not establish translated payload meaning. Labels remain unconfirmed. Raw rewrites, raw/grounded plans, errors and reuse are side by side in [English](task4_rewrites/en.md), [Tamil](task4_rewrites/ta.md), [Malayalam](task4_rewrites/ml.md) and [Telugu](task4_rewrites/te.md), and records/task4/rewrite_plan_review.csv. No human meaning rating was invented.','',
        '## Frozen grammar check and word-list comparison','',
        '| Corpus | Parsed / rows | Unique parsed / texts |', '| --- | --- | --- |']
    for name,g in coverage['groups'].items():text.append(f"| {name} | {g['parsed']}/{g['rows']} | {g['unique_parsed']}/{g['unique_texts']} |")
    text+=['','[checked] The 36-row authored holdout was written before grammar implementation; SHA-256 b1f07d843278057b5cd87f32cb81b28d53ec59ad207ceda2ec7a2f8e0d89a698. Grammar candidate SHA-256 '+coverage['candidate']['grammar_sha256']+' was frozen before its first held-out evaluation and has not been tuned afterwards. See records/task4/grammar_holdout_freeze.json, grammar_candidate_freeze.json and grammar_coverage_v1. Held-out parser coverage is 91.7%, versus 100% on intended starters (only ten unique English templates). This is an authored, same-developer check, not a blind independent test. Coverage includes explicit refusal issues and only means a schema-valid parse; it can still contain an incorrect target or swallowed extra text. Grounding may then ask. Saved task 3 rewrites have substantially lower parse coverage, especially A. Every saved phrasing is tested for safe parse-or-fallback, not claimed supported by grammar.','',
        '[checked] data/office_meanings.json contains fixed general office words in en/ta/ml/te, no whole starter sentences. SHA-256 '+sha(ROOT/'data/office_meanings.json')+'. The prompt includes the relevant language plus English. This list was derived from these same starter errors: any measured gain is optimistic and cannot establish generalization. Tables report both conditions, including regressions. The true test is real owner commands and native review; there are none yet.','',
        '## Map rehearsal and write boundary','',
        '[checked] Twenty scripted scenario groups (ten scenarios each in English and typed Tamil/codemixed Tamil) exercise all requested map rows and destination steps. These use recorded mock rewrites, not measured language-model quality. All names dictionaries and the fixture file remained byte-identical; map_or_names_changes:false and robot_motion:false. Actual authored log records are marked SCRIPTED. See records/task4/map_rehearsals/summary.json and per-session transcripts.','',
        '| Scenario (both languages) | Observed outcome |', '| --- | --- |',
        '| Unknown name | Asked with approved options; no action |',
        '| Known off-map, decline | Logged request; cancelled after revised proposal; no action |',
        '| Known off-map, confirm | Proposed cafeteria; explicit yes before illustrative go |',
        '| Unreachable | Fixed blocked reason; later deferred the job |',
        '| Person missing usual_place | Asked mapped location; one-job choice + new yes; delivered without roster edit |',
        '| Mapped person hint, no | Offered new destination; no reroute, original bring-back policy |',
        '| Mapped person hint, yes | One confirmed new step to conference hall; delivered there |',
        '| follow_person_hint never | Asked nearby people, no reroute; original absence policy |',
        '| Unmapped person hint | Logged suggestion; original absence policy |',
        '| Found away three times | Three confirmed deliveries; one person-location suggestion, applied:false |','',
        '[checked] Map checks cover main steps, sources, nested branches and leave_with alternatives. Fields and exact boundaries are proposed in [INTEGRATION.md](INTEGRATION.md). Only append-only records/map_requests is writable by the feature; symlink/hardlink destinations are rejected. Tests protect sentinel names/map files and compare caller dictionaries across success, asks and hint handling. Missing usual_place/reachability asks instead of inventing facts. Nearest suggestions are caller-provided or Euclidean supplied-coordinate comparisons, not a route search. The six required map speech events exist in four languages and all non-English templates remain needs native-speaker check. The module does not implement the console owner-approval UI.','',
        '## C and console recommendation','',
        ('[checked] C was installed after these A/B runs. All pinned files and packages were verified, custom sources reviewed before loading, and offline CPU/GPU greeting smoke tests passed in Tamil, Malayalam and Telugu. The temporary token was revoked. See [C installation record](C_INSTALLATION.md). [not checked] The full 70-row C translation→hybrid comparison, CPU/GPU timing and memory with B loaded/unloaded, C-versus-B-greedy agreement and C/A/B rewrite review remain pending. No C office-quality result is inferred from installation smoke tests.' if (BASE/'C_install/completion.json').exists() else '[not checked] C is awaiting installation. No translation score or memory number is inferred.'),'']
    # Give a provisional path based on evidence; don’t silently change the library default.
    g=aggregate['B_glossary_off']
    text += [f"[likely] Give the console **B + hybrid** as a supervised typed-preview candidate, with office_meanings:false until native review. It retains the stronger translation evidence supplied by the owner’s task 3 review, while A’s formal Tamil remains weak. Use planner_mode:hybrid, draw_policy:adaptive, follow_person_hint:ask, sequential calls, context 4096, rewrite_tokens:384 and plan_tokens:768. Keep product timeout_s:5, prepare_office at worker startup and separate UI/STOP/navigation work. Across these 70 authored B/no-list requests, measured p50/p95 was {g['p50_ms']/1000:.2f}/{g['p95_ms']/1000:.2f}s, {g['within_5s']['successes']}/70 finished within 5s, and sampled total GPU peak was {g['resources']['peak_total_gpu_mib']} MiB. These standalone diagnostic times include calls beyond the product deadline; product timeout refusals are not timed successful plans. Expect this range only for this authored workload, not a service guarantee. The per-language tails above still miss the p95≤5s target. Keep the existing 4-GiB allocation reservation; peak sampling can miss spikes.", '',
        '[not checked] Seamless console operation is not established: native/owner meaning review, real microphone commands, owner-approved map facts, shared GPU use while Gazebo navigates and C comparison remain open. B’s better rewrite evidence does not imply accurate final jobs. Keep explicit readback + yes, arbitrary content on screen, and no robot dispatch from this module. The glossary is not recommended by default merely because it was designed from these tests.','']
    (ROOT/'docs/TASK4_HYBRID_CHECK.md').write_text('\n'.join(text).rstrip()+'\n')
    print('Generated task 4 report, four review pages and comparison evidence; all measured hashes match.')

if __name__=='__main__': main()
