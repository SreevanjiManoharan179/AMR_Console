"""Caller-supplied map facts and append-only owner suggestions; never edit maps."""
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import stat
from zoneinfo import ZoneInfo

from .calibration import ROOT
from .office_schema import walk_steps
from .grounding import normalize

LOG_ROOT=ROOT/'records/map_requests'
REASONS={'blocked','no_route','closed','not_verified','unknown'}

def _reason(value):
    return value.casefold().replace(' ','_') if isinstance(value,str) else value


class MapRequests:
    def __init__(self,map_id,source='UNREVIEWED_TYPED'):
        self.map_id=map_id;self.source=source
    def append(self,kind,requester,words,language,missing,nearest=None,**extra):
        now=datetime.now(ZoneInfo('Asia/Kolkata'))
        root=LOG_ROOT.resolve()
        if not root.is_relative_to((ROOT/'records').resolve()):raise ValueError('Map log escaped module records')
        root.mkdir(parents=True,exist_ok=True)
        path=root/(now.strftime('%Y-%m-%d')+'.jsonl')
        row={'time':now.isoformat(),'who_asked':requester,'exact_words':words,'language':language,
             'kind':kind,'missing_or_wrong':missing,'nearest_mapped_place':nearest,
             'map_id':self.map_id,'source':self.source,'applied':False,**extra}
        encoded=(json.dumps(row,ensure_ascii=False)+'\n').encode()
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('Map log must be a regular unlinked file')
            fcntl.flock(fd,fcntl.LOCK_EX);os.write(fd,encoded)
        finally:os.close(fd)
        return row
    def seen_elsewhere(self,person,place,usual,requester,words,language):
        observation=self.append('person_seen_elsewhere_observation',requester,words,language,
            'Confirmed interaction away from usual place',person=person,place=place,usual_place=usual)
        count=0;requested=False
        root=LOG_ROOT.resolve()
        for path in sorted(root.glob('*.jsonl')):
            if path.is_symlink():continue
            for line in path.read_text().splitlines():
                try:row=json.loads(line)
                except ValueError:continue
                if (row.get('map_id'),row.get('person'),row.get('place'),row.get('usual_place'),row.get('source'))!=(self.map_id,person,place,usual,self.source):continue
                count+=row.get('kind')=='person_seen_elsewhere_observation'
                requested=requested or row.get('kind')=='person_location_update_request'
        if count>=3 and not requested:
            return self.append('person_location_update_request',requester,words,language,
                'Person confirmed away from usual place three times; owner review required',place,
                person=person,place=place,usual_place=usual,observations=count)
        return observation


class MapFacts:
    def __init__(self,roster):
        self.roster=roster;self.raw=roster.names.raw
        self.places={k:v for group in ('rooms','seats','objects') for k,v in roster.names.groups[group].items()}
        for key,row in self.places.items():
            for field in ('on_map','reachable','requires_arm','requires_door_operation','requires_lift','requires_stairs'):
                if field in row and type(row[field]) is not bool:raise ValueError('Invalid '+field+' map fact')
            nearest=row.get('nearest_mapped_place')
            if nearest is not None and nearest not in self.places:raise ValueError('Unknown nearest place fact')
            reason=row.get('unreachable_reason')
            if reason is not None and _reason(reason) not in REASONS:raise ValueError('Use a fixed reachability reason code')
        for person,row in roster.names.groups['people'].items():
            for field in ('usual_place','last_seen_place'):
                if row.get(field) is not None and row[field] not in self.places:raise ValueError('Unknown person place fact')
            if row.get('last_seen_place') and not isinstance(row.get('last_seen_at'),str):raise ValueError('Last seen place needs its recorded time')
        if self.raw.get('requester_place') is not None and self.raw['requester_place'] not in self.places:raise ValueError('Unknown requester origin place')
    def usual(self,person):
        row=getattr(self.roster,'temporary',{}).get(person) or self.roster.names.groups['people'].get(person,{})
        return row.get('usual_place')
    def facts(self,key):
        row=self.places[key];parent=self.places.get(row.get('room'),{})
        on_map=row.get('on_map',parent.get('on_map',('x' in row and 'y' in row) or bool(row.get('target_xy'))))
        reachable=row.get('reachable',parent.get('reachable'))
        return on_map,reachable,_reason(row.get('unreachable_reason',parent.get('unreachable_reason','not_verified' if reachable is None else 'unknown')))
    def mapped(self,reachable=False):
        return sorted(k for k in self.places if self.facts(k)[0] and (not reachable or self.facts(k)[1] is True))
    def _xy(self,key):
        row=self.places[key];value=row.get('target_xy')
        if isinstance(value,list) and len(value)==2 and all(type(v) in (int,float) and math.isfinite(v) for v in value):return tuple(value)
        if all(type(row.get(k)) in (int,float) and math.isfinite(row[k]) for k in ('x','y')):return row['x'],row['y']
        return None
    def nearest(self,key):
        row=self.places.get(key,{})
        explicit=row.get('nearest_mapped_place')
        eligible=self.mapped(reachable=True)
        if explicit in eligible:return explicit
        point=self._xy(key) if key in self.places else None
        if point is None:return None
        candidates=[(math.dist(point,self._xy(k)),k) for k in eligible if k!=key and self._xy(k) is not None]
        return min(candidates)[1] if candidates else None
    def check(self,key,person_locations=None):
        person_locations=person_locations or {}
        if key==self.roster.requester_id:
            # The console captures the requester origin. Check an explicit place when supplied.
            origin=self.raw.get('requester_place')
            if origin is None:return None
            key=origin
        elif self.roster.person(key):
            place=person_locations.get(key,self.usual(key))
            if place is None:return {'event':'ask_where_person','target':key,'options':self.mapped(reachable=True)[:3]}
            key=place
        if key not in self.places:return {'event':'unknown_name','target':key,'options':[]}
        on_map,reachable,reason=self.facts(key)
        if not on_map:
            nearest=self.nearest(key)
            return {'event':'place_not_on_map','target':key,'nearest':nearest,'options':[nearest] if nearest else []}
        if reachable is not True:
            options=[p for p in self.mapped(reachable=True) if p!=key][:3]
            return {'event':'place_unreachable','target':key,'reason':reason,'options':options}
        return None
    def check_steps(self,steps,person_locations=None):
        for step in walk_steps(steps):
            for slot in ('to','from','at'):
                if slot in step:
                    problem=self.check(step[slot],person_locations)
                    if problem:return {**problem,'slot':slot}
            if step['if_not_there'].startswith('leave_with:'):
                problem=self.check(step['if_not_there'].split(':',1)[1],person_locations)
                if problem:return {**problem,'slot':'if_not_there'}
        return None
    def location_hint(self,text,person=None):
        # Literal aliases only; no fuzzy relocation and no invented place.
        value=text.strip(' .!?')
        import re
        subject=person or self.roster.requester_id
        value=re.sub(r'^(?:(?:he|she|they|it)|'+re.escape(subject)+r')\s+(?:is|are)\s+(?:in|at)\s+','',value,flags=re.I)
        matches=self.roster.match(value,action='go')
        return tuple(k for k in matches if k in self.places)
