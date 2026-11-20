"""Approved destinations and explicit requester identity for office v3."""
from difflib import SequenceMatcher
import re
import unicodedata

from .grounding import Names,NONE,normalize
from .schema import valid_string
from .core import source_span

LANGUAGES=('en','ta','ml','te')


class OfficeNames:
    def __init__(self,raw,requester):
        self.names=Names(raw)
        for row in self.names.entries.values():
            if row.get('weight_class') not in (None,'light','medium','heavy'):raise ValueError('Invalid item weight class')
            for flag in ('fits_tray','fragile','personal','restricted'):
                if flag in row and type(row[flag]) is not bool:raise ValueError('Invalid item '+flag)
        for row in self.names.groups['people'].values():
            if row.get('language') is not None and row['language'] not in LANGUAGES:
                raise ValueError('Invalid person language')
        if isinstance(requester,str):
            requester={'id':requester,'name':requester}
        if not isinstance(requester,dict) or not valid_string(requester.get('id'),100):
            raise ValueError('Requester identity required')
        if set(requester)-{'id','name','language'}:
            raise ValueError('Invalid requester fields')
        self.temporary={}
        for row in raw.get('temporary_people',[]):
            if not isinstance(row,dict) or set(row)!={'id','name','usual_place','requester','job_id'} or row['requester']!=requester['id'] or not all(valid_string(row[k],100) for k in row):raise ValueError('Invalid one-job person context')
            if row['id'] in self.names.entries or row['usual_place'] not in self.names.entries:raise ValueError('Invalid temporary person location')
            self.temporary[row['id']]=dict(row)
        self.requester=dict(requester)
        if self.requester['id'] in self.names.entries and self.requester['id'] not in self.names.groups['people']:
            raise ValueError('Requester must be a person or explicit caller identity, not a destination')
        self.requester.setdefault('name',self.requester['id'])
        if not valid_string(self.requester['name'],100):
            raise ValueError('Invalid requester name')
        if self.requester.get('language') is not None and self.requester['language'] not in LANGUAGES:
            raise ValueError('Invalid requester language')

    @property
    def requester_id(self):
        return self.requester['id']

    def match(self,query,slot='to',action='go'):
        value=normalize(query)
        if value in {normalize(self.requester_id),normalize(self.requester['name']),'me','myself','requester','the requester'}:
            return (self.requester_id,)
        if value.startswith('the '):
            value=value[4:]
        groups=('rooms','people','seats','objects') if action in ('go','check') else ('rooms','people','seats')
        matched=set()
        for group in groups:
            matched.update(self.names.aliases[group].get(value,()))
        if not matched and 'rooms' in groups:
            generic=self.names.match('place',value)
            if generic!=(NONE,):
                matched.update(generic)
        from .office_learning import load_aliases
        aliases=load_aliases()
        aliases.update(self.names.raw.get('review_aliases',{}))
        matched.update(target for alias,target in aliases.items() if normalize(alias)==value and self.known(target))
        matched.update(k for k,r in self.temporary.items() if value in (normalize(k),normalize(r['name'])))
        return tuple(sorted(matched))

    @staticmethod
    def _sound(value):
        # Coarse letter-name transliteration is a suggestion aid, never grounding.
        pieces=[]
        for char in unicodedata.normalize('NFKD',value.casefold()):
            if char.isascii():pieces.append(char)
            elif char.isalpha():
                name=unicodedata.name(char,'')
                pieces.append(name.split('LETTER ')[-1].lower() if 'LETTER ' in name else '')
        text=''.join(pieces)
        for old,new in (('ph','f'),('ck','k'),('qu','k'),('q','k'),('th','t'),('sh','s'),('ch','c'),('c','k'),('w','v')):text=text.replace(old,new)
        text=re.sub('[^a-z]','',text)
        return re.sub(r'(.)\1+',r'\1',re.sub('[aeiouyh]','',text))

    def suggestions(self,query,limit=3,action='go',person_only=False):
        scores={};value=re.sub(r'^(?:the|a|an)\s+','',normalize(query));sound=self._sound(value)
        groups=('people',) if person_only else ('rooms','people','seats','objects') if action in ('go','check') else ('rooms','people','seats')
        for group in groups:
            for alias,ids in self.names.aliases[group].items():
                other=self._sound(alias)
                score=SequenceMatcher(None,value,alias).ratio()
                if sound and other:
                    phonetic=.86*SequenceMatcher(None,sound,other).ratio()
                    # A short approved alias can be the start of a noisy longer word.
                    if len(other)>=2 and sound.startswith(other):phonetic=max(phonetic,.82)
                    score=max(score,phonetic)
                for key in ids:scores[key]=max(scores.get(key,0.),score)
        return [key for key,score in sorted(scores.items(),key=lambda pair:(-pair[1],pair[0]))[:limit] if score>=.35]

    def known(self,key):
        return key==self.requester_id or key in self.names.entries or key in self.temporary

    def person(self,key):
        return key==self.requester_id or key in self.names.groups['people'] or key in self.temporary

    def label(self,key,language):
        if key in self.temporary:return {'en':'the person shown on screen','ta':'திரையில் காட்டிய நபர்','ml':'സ്ക്രീനിലെ വ്യക്തി','te':'తెరపై చూపిన వ్యక్తి'}[language]
        if key==self.requester_id and key not in self.names.entries:
            return self.requester['name']
        if key not in self.names.entries:
            raise ValueError('Unapproved spoken identity')
        entry=self.names.entries[key]
        aliases=entry.get('aliases_by_language',{}).get(language,[])
        return aliases[0] if aliases else key.replace('_',' ')

    def language_for(self,key,fallback='en'):
        if key==self.requester_id and self.requester.get('language'):
            return self.requester['language']
        return self.names.entries.get(key,{}).get('language') or fallback

    def prompt_roster(self,language):
        rows=[]
        for group,entries in self.names.groups.items():
            for key,row in entries.items():
                aliases=row.get('aliases',[])+row.get('aliases_by_language',{}).get(language,[])
                rows.append({'id':key,'kind':group,'aliases':aliases})
        return {'requester':self.requester_id,'approved':rows}

    def check_preserved_names(self,original,english):
        owners={}
        for aliases in self.names.aliases.values():
            for alias,ids in aliases.items(): owners.setdefault(alias,set()).update(ids)
        for group,entries in self.names.groups.items():
            for key,row in entries.items():
                aliases=[key,*row.get('aliases',[])]
                for values in row.get('aliases_by_language',{}).values(): aliases.extend(values)
                # This is a conservative dropped-name check, never a way to
                # authorize a destination. Native inflected aliases can contain
                # the approved stem; final grounding still uses the English rewrite.
                mentioned=any(source_span(alias,original) if alias.isascii() else normalize(alias) in normalize(original)
                              for alias in aliases if len(alias)>=3 and owners.get(normalize(alias))=={key})
                if mentioned and not any(source_span(alias,english) for alias in aliases if alias.isascii()):
                    raise ValueError('Rewrite dropped an explicitly named approved entity')
