#!/usr/bin/env python3
"""Offline install smoke check; not the 70-row task 4 translation benchmark.

Run with the pinned environment's Python -I. The reviewed inventory must exist.
No credentials or authentication files are read by this script.
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent
BASE=ROOT/'.runtime/indictrans2-C'
OUT=ROOT/'records/task4/C_install'


def main():
    review=json.loads((OUT/'inventory_and_source_review.json').read_text())
    if review.get('reviewed_before_trust_remote_code') is not True:
        raise ValueError('Inspect pinned custom sources before loading')
    manifest=json.loads((ROOT/'data/indictrans2_C_model.json').read_text())
    if review['revision']!=manifest['revision']:raise ValueError('Reviewed revision differs')
    for row in review['verified_files']:
        path=BASE/'model'/row['file']
        if path.stat().st_size!=row['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
            raise ValueError('Reviewed file changed: '+row['file'])
    os.environ.update(HF_HOME=str(BASE/'hf-cache'),HF_MODULES_CACHE=str(BASE/'custom-modules'),
                      HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',
                      TOKENIZERS_PARALLELISM='false')
    import torch
    from transformers import AutoModelForSeq2SeqLM,AutoTokenizer
    from IndicTransToolkit.processor import IndicProcessor
    torch.set_num_threads(4)
    torch.manual_seed(1729)
    start=time.monotonic()
    tokenizer=AutoTokenizer.from_pretrained(str(BASE/'model'),trust_remote_code=True,local_files_only=True,
                                          token=False,use_fast=False)
    model=AutoModelForSeq2SeqLM.from_pretrained(str(BASE/'model'),trust_remote_code=True,
            local_files_only=True,token=False,use_safetensors=True,attn_implementation='eager',torch_dtype=torch.float32).eval()
    load_ms=(time.monotonic()-start)*1000
    processor=IndicProcessor(inference=True)
    inputs=[('ta','tam_Taml','வணக்கம்.'),('ml','mal_Mlym','നമസ്കാരം.'),('te','tel_Telu','నమస్కారం.')]
    records=[]
    devices=['cpu']+(['cuda'] if torch.cuda.is_available() else [])
    for device in devices:
        model=model.to(dtype=torch.float16 if device=='cuda' else torch.float32,device=device)
        if device=='cuda':torch.cuda.reset_peak_memory_stats()
        for language,tag,text in inputs:
            tick=time.monotonic()
            batch=processor.preprocess_batch([text],src_lang=tag,tgt_lang='eng_Latn')
            encoded=tokenizer(batch,return_tensors='pt',padding=True,truncation=False)
            if encoded['input_ids'].shape[-1]>256:raise ValueError('Input exceeds pinned source context')
            encoded={k:v.to(device) for k,v in encoded.items()}
            with torch.inference_mode():
                generated=model.generate(**encoded,do_sample=False,num_beams=5,max_new_tokens=64,
                                         min_length=0,use_cache=True,num_return_sequences=1)
            if device=='cuda':torch.cuda.synchronize()
            decoded=tokenizer.batch_decode(generated,skip_special_tokens=True,clean_up_tokenization_spaces=True)
            translated=processor.postprocess_batch(decoded,lang='eng_Latn')[0]
            if not isinstance(translated,str) or not translated.strip():raise ValueError('No translation returned')
            row={'language':language,'source_tag':tag,'text':text,'device':device,
                 'dtype':'float16' if device=='cuda' else 'float32','translation':translated,
                 'wall_ms':round((time.monotonic()-tick)*1000,3),'meaning_owner_rating':None}
            records.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
    result={'time':datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(),'status':'passed',
            'model_revision':manifest['revision'],'offline':True,'local_files_only':True,
            'trust_remote_code':'only reviewed pinned local sources','attention':'eager',
            'parameters':sum(p.numel() for p in model.parameters()),'cpu_initial_load_ms':load_ms,
            'devices':devices,'translations':records,'task4_quality_benchmark':'not run',
            'notice':'Six greeting smoke cases check local loading/generation only; not office-command quality, latency or shared-GPU readiness.'}
    (OUT/'offline_smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print('Offline C install smoke check passed.',flush=True)


if __name__=='__main__':main()
