import json
import time

from amr_language.core import understand
from amr_language.backends import MockBackend, OllamaBackend
from amr_language.backends import BackendError, BackendTimeout
from amr_language.backends import source_schema
from amr_language.core import _settings
from amr_language.grounding import Names


def test_literal_loopback_schema_and_nonstreaming(monkeypatch,names,form):
    from amr_language import backends
    seen={}
    class Response:
        status=200
        def __init__(self):
            self.raw=json.dumps({'done':True,'message':{'content':json.dumps(form(place='reception'))}}).encode()
        def read1(self,size):
            value,self.raw=self.raw[:size],self.raw[size:]
            return value
    class Connection:
        sock=None
        def __init__(self,host,port,timeout):
            seen.update(host=host,port=port)
        def request(self,method,path,body,headers):
            seen.update(method=method,path=path,payload=json.loads(body))
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(backends.http.client,'HTTPConnection',Connection)
    backend=OllamaBackend('local-model')
    backend.sample('go to reception',Names(names),None,timeout=1,index=0,settings=_settings(None))
    assert seen['host']=='127.0.0.1'
    assert seen['path']=='/api/chat'
    assert seen['payload']['format']['additionalProperties'] is False
    assert seen['payload']['think'] is False and seen['payload']['stream'] is False
    assert 'map_yaml' not in json.dumps(seen['payload'])


def test_decoder_cannot_generate_unspoken_targets_or_split_indic_marks():
    schema=source_schema('manager cabin-ku கோப்பு kudunga')
    values=schema['$defs']['spoken_text']['enum']
    assert None in values and 'manager cabin' in values and 'கோப்பு' in values
    assert 'Priya' not in values and 'home' not in values and 'க' not in values
    assert len(source_schema('a '*150)['$defs']['spoken_text']['enum'])<=513
    for slot in ('place','person','object','item'):
        assert schema['properties']['steps']['items']['properties'][slot]=={'$ref':'#/$defs/spoken_text'}


def test_concurrent_call_queue_obeys_deadline(names,form,monkeypatch):
    from amr_language import core
    core._MODEL_LOCK.acquire()
    try:
        result=understand('go to reception',names,settings={'backend':MockBackend([form(place='reception')]),'timeout_s':.01})
        assert result['refusal']['template']=='model_timeout'
        assert result['samples']==0
    finally:
        core._MODEL_LOCK.release()


def test_pinned_model_manifest_and_server_identity(tmp_path,monkeypatch):
    import hashlib
    import pytest
    raw=json.dumps({'layers':[{'mediaType':'application/vnd.ollama.image.model','digest':'sha256:'+'a'*64}]}).encode()
    path=tmp_path/'manifest'
    path.write_bytes(raw)
    backend=OllamaBackend('chosen:tag','a'*64)
    backend.load_manifest(path)
    assert backend.manifest_sha256==hashlib.sha256(raw).hexdigest()
    def correct(method,path,payload,port,timeout):
        return {'version':'pinned'} if path=='/api/version' else {'models':[{'name':'chosen:tag','digest':backend.manifest_sha256}]}
    monkeypatch.setattr(backend,'_request',correct)
    backend.verify_identity({'port':11434,'runner_version':'pinned'},1.)
    with pytest.raises(BackendError):
        backend.verify_identity({'port':11434,'runner_version':'changed'},1.)
    with pytest.raises(ValueError):
        OllamaBackend('chosen:tag','b'*64).load_manifest(path)


def test_wrapped_registry_manifest_requires_verified_matching_layers(tmp_path,monkeypatch):
    import hashlib
    import pytest
    blobs=tmp_path/'models/blobs'
    manifests=tmp_path/'models/manifests/library'
    blobs.mkdir(parents=True)
    manifests.mkdir(parents=True)
    layer={'mediaType':'application/vnd.ollama.image.model','digest':'sha256:'+'a'*64,'size':10}
    config={'mediaType':'application/vnd.docker.container.image.v1+json','digest':'sha256:'+'b'*64,'size':1}
    original={'schemaVersion':2,'config':config,'layers':[layer]}
    raw=json.dumps(original).encode()
    digest=hashlib.sha256(raw).hexdigest()
    blob=blobs/('sha256-'+digest)
    blob.write_bytes(raw)
    wrapper={**original,'runner':'llamacpp','layers':[layer,{
        'mediaType':'application/vnd.docker.distribution.manifest.v2+json',
        'digest':'sha256:'+digest,'size':len(raw)}]}
    path=manifests/'model'
    path.write_text(json.dumps(wrapper))
    backend=OllamaBackend('local:tag','a'*64)
    backend.load_manifest(path)
    assert backend.registry_manifest_sha256==digest
    assert backend.manifest_sha256!=digest
    monkeypatch.setattr(backend,'_request',lambda method,path,*args:
                        {'version':'v'} if path=='/api/version' else {'models':[{'name':'local:tag','digest':digest}]})
    backend.verify_identity({'port':11434,'runner_version':'v'},1.)
    blob.write_text('{}')
    with pytest.raises(ValueError,match='checksum'):
        backend.load_manifest(path)
    blob.write_bytes(raw)
    wrapper['layers'][0]={**layer,'size':11}
    path.write_text(json.dumps(wrapper))
    with pytest.raises(ValueError,match='differs'):
        backend.load_manifest(path)


def test_truncated_and_redirected_outputs_fail(monkeypatch,names,form):
    import pytest
    backend=OllamaBackend('local')
    monkeypatch.setattr(backend,'_request',lambda *args:{'done':True,'done_reason':'length','message':{'content':'{}'}})
    with pytest.raises(BackendError):
        backend.sample('go to reception',Names(names),None,timeout=1,index=0,settings=_settings(None))
    from amr_language import backends
    class Response:
        status=302
    class Connection:
        sock=None
        def __init__(self,*args,**kwargs): pass
        def request(self,*args,**kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(backends.http.client,'HTTPConnection',Connection)
    with pytest.raises(BackendError):
        OllamaBackend('local')._request('GET','/api/tags',None,11434,1.)


def test_absolute_deadline_interrupts_stalled_headers(monkeypatch):
    from threading import Event
    import pytest
    from amr_language import backends
    released=Event()
    class Socket:
        def shutdown(self,how): released.set()
    class Connection:
        sock=Socket()
        def __init__(self,*args,**kwargs): pass
        def request(self,*args,**kwargs): pass
        def getresponse(self):
            released.wait(1.)
            raise OSError('closed socket')
        def close(self): pass
    monkeypatch.setattr(backends.http.client,'HTTPConnection',Connection)
    started=time.monotonic()
    with pytest.raises(BackendTimeout):
        OllamaBackend('local')._request('GET','/api/tags',None,11434,.02)
    assert time.monotonic()-started<.5
