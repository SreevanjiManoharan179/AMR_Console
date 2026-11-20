#!/usr/bin/env python3
"""Render a factual Markdown report from completed synthetic benchmark logs."""
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import statistics
from zoneinfo import ZoneInfo

from bench_utils import percentile,wilson

ROOT=Path(__file__).resolve().parent.parent


def rate(value):
    if value['rate'] is None:
        return 'undefined (n=0)'
    low,high=value['wilson95']
    return f"{value['successes']}/{value['total']} ({100*value['rate']:.1f}%; {100*low:.1f}–{100*high:.1f}%)"


def main():
    directory=ROOT/'records/benchmarks/synthetic_standalone'
    summary=json.loads((directory/'summary.json').read_text())
    metadata=json.loads((directory/'metadata.json').read_text())
    records=[json.loads(line) for line in (directory/'requests.jsonl').read_text().splitlines()]
    if len(records)!=300 or summary['requests']!=300:
        raise ValueError('Refusing to report a partial run as complete')
    lines=['# Measured results — approved A, standalone synthetic development run','',
      f"[checked] Generated {datetime.now(ZoneInfo('Asia/Kolkata')).isoformat()} from saved logs. "
      '**This is SYNTHETIC development data, not real-world accuracy.** No model-generated task was dispatched. '
      'There is no real-command calibration or active-console-navigation benchmark.','',
      '[checked] Ollama v0.40.12; Qwen3.5 2B Q4_K_M; prompt `'+metadata['prompt_version']+'`; '
      'seven requested samples at temperature 0.7, context 4096 and generation cap 512. '
      'Production settings keep a two-second deadline. The diagnostic run used 120 seconds to let extraction finish; '
      'that change does not make the two-second target pass. Runtime and all model blob hashes are in '
      '[INSTALL_STATUS.md](INSTALL_STATUS.md) and the [inventory](../records/checks/model_inventory.json).','',
      '## Per-language extraction results','',
      '[checked] Each rate below shows numerator/denominator, percentage and a Wilson 95% interval. '
      'Task-form validity includes semantic and source-span checks and requires every sampled form in the request to pass. '
      'Requests refused before sampling are excluded only from that model-validity denominator. '
      'Action accuracy below uses positive/in-scope requests, including unknown and ambiguous destinations. '
      'Grounding accuracy uses requests with annotated destination slots, counts extra/missing targets as errors, '
      'and uses the modal joint target sequence across usable samples. Any failed sampled form makes the strict request score fail.','',
      '| Language | Requests | Strict form/source validity on sampled requests | Correct action (in-scope) | Correct grounding (target-bearing) |',
      '| --- | ---: | --- | --- | --- |']
    calculated={}
    for language in ('en','ta','ml','te'):
        rows=[r for r in records if r['language']==language]
        positive=[r for r in rows if not r['expected_task_form']['out_of_scope']]
        target=[r for r in positive if any(r['expected_ids'])]
        action=wilson(sum(r['assessment']['action_or_scope_correct'] for r in positive),len(positive))
        grounding=wilson(sum(r['assessment']['all_sampled_forms_valid'] and
                            r['assessment'].get('modal_grounding')==r['expected_ids'] for r in target),len(target))
        calculated[language]={'correct_action_in_scope':action,'correct_grounding_target_bearing':grounding}
        valid=summary['by_language'][language]['sampled_request_validity']
        lines.append(f'| [checked] {language} | {len(rows)} | {rate(valid)} | {rate(action)} | {rate(grounding)} |')
    lines+=['','## Actual decisions and latency','',
      '[checked] Decision rates are the actual public API results with no calibration. '
      'No synthetic threshold was fitted. All accepted-request wrong-destination rates are undefined because '
      'there were no accepted requests; this must not be presented as 0% wrong destinations or as demonstrated safety. '
      'Coverage at 90% is **not measured**.','',
      '| Language | Act | Ask | Refuse | Median API ms | p95 API ms |',
      '| --- | --- | --- | --- | ---: | ---: |']
    for language in ('en','ta','ml','te'):
        group=summary['by_language'][language]
        latency=group['latency_ms']
        lines.append(f"| [checked] {language} | {rate(group['decisions']['act'])} | {rate(group['decisions']['ask'])} | "
                     f"{rate(group['decisions']['refuse'])} | {latency['median']:.1f} | {latency['p95']:.1f} |")
    completed=[r for r in records if r['api']['samples']==7]
    times=[r['api']['latency_ms'] for r in completed]
    lines+=['',f'[checked] All-seven-sample requests: **{len(completed)}**, median **{statistics.median(times):.1f} ms**, '
            f'p95 **{percentile(times,.95):.1f} ms**. Early refusals and prefilters are included in the per-language '
            'all-request latencies above, so those figures can be shorter than completing the confidence procedure. '
            'These are observed standalone full-API timings with a warmed model and hash preflight; '
            'identity and model-file hash preflight happens before the benchmark loop and is not included in these warm latencies. '
            'No shared-load latency target is claimed.','',
            '[checked] Refusal reasons across the run: '+', '.join(f'`{key}` {count}' for key,count in
            sorted(Counter((r['api']['refusal'] or {}).get('template') for r in records).items()))+'.','',
            '## Rule-based comparison on the same requests','',
            '[checked] The baseline runs the unchanged `reference/current_grammar.py`, followed by approved-alias grounding. '
            'It is a parser/grounder comparison, not a rerun of the full console executor or its calibrated confirmation policy. '
            'The old grammar emits only go/deliver; explicit return/check/context actions and multi-step requests are compared '
            'against the new task-form labels exactly. This representational difference contributes errors and must not be '
            'misrepresented as inability to drive back to a dock. No new language aliases or parsing rules were added to the old grammar.','',
            '| Language | Baseline correct action (in-scope) | Baseline correct action + grounding (target-bearing) | Parse refusals, all requests |',
            '| --- | --- | --- | --- |']
    for language in ('en','ta','ml','te'):
        rows=[r for r in records if r['language']==language]
        positive=[r for r in rows if not r['expected_task_form']['out_of_scope']]
        target=[r for r in positive if any(r['expected_ids'])]
        action=wilson(sum(r['baseline']['action_or_scope_correct'] for r in positive),len(positive))
        grounding=wilson(sum(r['baseline']['grounding_correct'] for r in target),len(target))
        rejected=wilson(sum(r['baseline']['parse_rejected'] for r in rows),len(rows))
        calculated[language]['baseline_correct_action_in_scope']=action
        calculated[language]['baseline_correct_action_and_grounding_target_bearing']=grounding
        lines.append(f'| [checked] {language} | {rate(action)} | {rate(grounding)} | {rate(rejected)} |')
    probe=ROOT/'records/benchmarks/production_deadline_probe/requests.jsonl'
    if probe.exists():
        probe_rows=[json.loads(line) for line in probe.read_text().splitlines()]
        probe_reasons=Counter((r['api']['refusal'] or {}).get('template') for r in probe_rows)
        lines+=['','## Production two-second deadline probe','',
                f"[checked] A separate stratified **{len(probe_rows)}-request** probe (three simple go requests per language) "
                f"used the actual two-second deadline and seven requested samples. **{sum(r['api']['samples']==7 for r in probe_rows)}/{len(probe_rows)}** "
                'completed all seven samples. Refusals: '+', '.join(f'`{key}` {count}' for key,count in sorted(probe_reasons.items()))+
                f". Maximum observed API latency was **{max(r['api']['latency_ms'] for r in probe_rows):.3f} ms**. "
                'The cutoff works, but these calls do not deliver a complete confidence estimate within budget. '
                'This is an additional synthetic diagnostic, not a calibrated acceptance result. Evidence: '
                '[probe requests](../records/benchmarks/production_deadline_probe/requests.jsonl), '
                '[probe summary](../records/benchmarks/production_deadline_probe/summary.json).']
    resources=summary['resources']
    lines+=['','## Resources and cold start','',
      f"[checked] During this run, sampled peak total GPU memory was **{resources['peak_total_gpu_mib']} MiB**; "
      f"peak visible compute-process allocation was **{resources['peak_all_compute_processes_gpu_mib']} MiB**. "
      f"Minimum available RAM was **{resources['minimum_available_ram_kib']} KiB**. "
      f"{resources['resource_samples']} resource samples targeted 100 ms intervals. "
      'These sampled peaks may miss brief spikes and include all visible compute processes. '
      'No Gazebo/RViz/Nav2 session was established, so the memory result is standalone only.','',
      '[checked] Initial prompt smoke: **8/15** single-sample forms passed semantic validation; '
      'the initial first request took **58,165.7 ms**, including first model load. '
      'The later source-span-constrained prompt on the same development cases produced **12/15** valid forms. '
      'That is reused development data, not independent validation. The revised actual two-second API probe '
      'timed out at **2,001.006 ms** after two completed samples. Initial failures are retained rather than overwritten.','',
      '## Readiness and remaining work','',
      '- [checked] Approved runtime/model installation and actual CUDA execution succeeded; every installed model artifact is checksummed.',
      '- [checked] The current seven-sample configuration does not satisfy the two-second request target in isolation. '
        'A longer diagnostic timeout has not been substituted into deployment settings.',
      '- [checked] Native-language action/grounding failures remain; source-constrained slots prevent unspoken names '
        'but do not prove correct intent selection. No training/fine-tuning was performed.',
      '- [not checked] Real-client accuracy, speaker-disjoint calibration/test coverage, shared-load peak VRAM, navigation '
        'regressions and STOP/UI responsiveness. No owner-started navigation session was available for this run.',
      '- [not checked] Microphone/ASR setup and Tamil/English recorded-clip pilot; speech downloads still require their separate approval.',
      '- [checked] Synthetic language text and aliases are draft fixtures needing native-speaker review. '
        'Prompts were developed on this corpus. Language blocks ran in en/ta/ml/te order with shared caching/thermal state; '
        'these results are not an independent randomized benchmark. Wilson intervals are descriptive and do not remove '
        'dependence among template variants or supply a conformal guarantee.','',
      '[checked] Evidence: [benchmark metadata](../records/benchmarks/synthetic_standalone/metadata.json), '
      '[per-request records](../records/benchmarks/synthetic_standalone/requests.jsonl), '
      '[summary](../records/benchmarks/synthetic_standalone/summary.json), '
      '[derived report metrics](../records/benchmarks/synthetic_standalone/report_metrics.json), '
      '[resources](../records/benchmarks/synthetic_standalone/resources.jsonl), '
      '[initial smoke](../records/checks/model_smoke_initial.jsonl), '
      '[revised smoke](../records/checks/model_smoke.jsonl), '
      '[verification](../records/checks/verification.json).']
    (directory/'report_metrics.json').write_text(json.dumps(calculated,indent=2)+'\n')
    (ROOT/'docs/RESULTS.md').write_text('\n'.join(lines)+'\n')
    print('Wrote docs/RESULTS.md from all 300 saved request records')


if __name__=='__main__':
    main()
