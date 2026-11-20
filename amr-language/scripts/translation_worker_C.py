#!/usr/bin/env python3
"""Pinned offline C translator. Must run in its own C-venv Python -I process."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import time

ROOT=Path(__file__).resolve().parent.parent
BASE=ROOT/'.runtime/indictrans2-C'
TAGS={'ta':'tam_Taml','ml':'mal_Mlym','te':'tel_Telu'}


def emit(value):print(json.dumps(value,ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--device',choices=['cpu','cuda'],required=True);args=parser.parse_args()
    if not sys.flags.isolated or Path(sys.prefix).resolve()!=(BASE/'venv').resolve():
        raise SystemExit('C requires its own .runtime/indictrans2-C/venv/bin/python -I process')
    if any('/opt/ros/' in p for p in sys.path):raise SystemExit('ROS path leaked into isolated C interpreter')
    started=time.monotonic()
    manifest=json.loads((ROOT/'data/indictrans2_C_model.json').read_text())
    reviewed=json.loads((ROOT/'records/task4/C_install/inventory_and_source_review.json').read_text())
    if not reviewed.get('reviewed_before_trust_remote_code') or reviewed['revision']!=manifest['revision']:
        raise SystemExit('Pinned custom sources require review first')
    hashes={}
    for entry in manifest['files']:
        p=BASE/'model'/entry['rfilename'];sha=hashlib.sha256();git=hashlib.sha1(b'blob '+str(p.stat().st_size).encode()+b'\0')
        if p.stat().st_size!=entry['size']:raise SystemExit('Pinned size mismatch')
        with p.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):sha.update(chunk);git.update(chunk)
        if entry.get('lfs'):
            if sha.hexdigest()!=entry['lfs']['sha256']:raise SystemExit('Pinned model hash mismatch')
        elif git.hexdigest()!=entry['blobId']:raise SystemExit('Pinned source/config hash mismatch')
        hashes[p.name]=sha.hexdigest()
    if {r['file']:r['sha256'] for r in reviewed['verified_files']}!=hashes:raise SystemExit('Reviewed inventory differs')
    if {p.name for p in (BASE/'model').glob('*.py')}!={n for n in hashes if n.endswith('.py')}:raise SystemExit('Unexpected custom source file')
    os.environ.update(HF_HOME=str(BASE/'hf-cache'),HF_MODULES_CACHE=str(BASE/'custom-modules'),
        HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',TOKENIZERS_PARALLELISM='false')
    for key in ('HF_TOKEN','HUGGING_FACE_HUB_TOKEN'):os.environ.pop(key,None)
    import torch
    from transformers import AutoTokenizer,AutoModelForSeq2SeqLM
    from IndicTransToolkit.processor import IndicProcessor
    torch.set_num_threads(4);torch.manual_seed(1729)
    if args.device=='cuda' and not torch.cuda.is_available():raise SystemExit('CUDA is not available; no substitution')
    tokenizer=AutoTokenizer.from_pretrained(str(BASE/'model'),trust_remote_code=True,local_files_only=True,token=False,use_fast=False)
    model=AutoModelForSeq2SeqLM.from_pretrained(str(BASE/'model'),trust_remote_code=True,local_files_only=True,
        token=False,use_safetensors=True,attn_implementation='eager',torch_dtype=torch.float32).eval()
    model=model.to(device=args.device,dtype=torch.float16 if args.device=='cuda' else torch.float32)
    processor=IndicProcessor(inference=True)
    emit({'kind':'ready','pid':os.getpid(),'isolated':bool(sys.flags.isolated),'python':sys.executable,
        'prefix':sys.prefix,'ros_paths':[],'device':args.device,'dtype':str(next(model.parameters()).dtype),
        'revision':manifest['revision'],'file_sha256':hashes,'startup_ms':(time.monotonic()-started)*1000,
        'startup_max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'decoding':{'do_sample':False,'num_beams':5,'max_new_tokens':256,'attention':'eager','threads':4}})
    for line in sys.stdin:
        request_id=None
        def respond(value):emit({**value,'request_id':request_id})
        try:
            request=json.loads(line);request_id=request.get('request_id')
            if not isinstance(request_id,str) or not request_id or len(request_id)>80:
                respond({'ok':False,'error':'invalid_request_id'});continue
            if request.get('op')=='close':
                respond({'ok':True,'kind':'closed'});break
            if request.get('op') not in ('probe','translate'):
                respond({'ok':False,'error':'invalid_operation'});continue
            probe=request.get('op')=='probe';text='வணக்கம்.' if probe else request['text'];language='ta' if probe else request['language']
            if language not in ('en',*TAGS) or not isinstance(text,str) or not 0<len(text)<=2000:
                respond({'ok':False,'error':'invalid_input'});continue
            if args.device=='cuda':torch.cuda.synchronize();torch.cuda.reset_peak_memory_stats()
            tick=time.monotonic();input_tokens=0;output_tokens=0;error=None
            if language=='en':english=text;kind='english_passthrough'
            else:
                kind='translation'
                processed=processor.preprocess_batch([text],src_lang=TAGS[language],tgt_lang='eng_Latn')
                encoded=tokenizer(processed,return_tensors='pt',padding=True,truncation=False)
                input_tokens=int(encoded['input_ids'].shape[-1])
                if input_tokens>256:
                    respond({'ok':False,'error':'input_too_long','input_tokens':input_tokens,'translation_ms':(time.monotonic()-tick)*1000});continue
                encoded={k:v.to(args.device) for k,v in encoded.items()}
                with torch.inference_mode():
                    output=model.generate(**encoded,do_sample=False,num_beams=5,num_return_sequences=1,
                        min_length=0,max_new_tokens=8 if probe else 256,use_cache=True)
                if args.device=='cuda':torch.cuda.synchronize()
                output_tokens=int(output.shape[-1]-1)
                if int(output[0,-1])!=tokenizer.eos_token_id:error='truncated_translation'
                decoded=tokenizer.batch_decode(output,skip_special_tokens=True,clean_up_tokenization_spaces=True)
                english=processor.postprocess_batch(decoded,lang='eng_Latn')[0]
                if not english.strip():error='empty_translation'
            value={'ok':error is None,'kind':'probe' if probe else kind,'english':english if error is None else None,
                'error':error,'translation_ms':(time.monotonic()-tick)*1000,'input_tokens':input_tokens,
                'output_tokens':output_tokens,'max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'cuda_peak_allocated_bytes':torch.cuda.max_memory_allocated() if args.device=='cuda' else 0,
                'cuda_peak_reserved_bytes':torch.cuda.max_memory_reserved() if args.device=='cuda' else 0}
            respond(value)
        except Exception as error:
            respond({'ok':False,'error':'runtime_error','error_type':type(error).__name__})


if __name__=='__main__':main()
