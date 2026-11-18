"""Recover verified names for Saluki export placeholders without changing Saluki."""

import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
import csv
from itertools import islice
import json
from pathlib import Path
import re
import shutil

import studio_core as core
from studio_gpu import OpenCL, GpuError, WORK_SIZE, index_keys, pack_words


ANIM = re.compile(r'anim_([a-f0-9]{1,16})', re.I)
SOUND = re.compile(r'sound_([a-f0-9]{1,16})', re.I)
WEAPON = re.compile(r'^(.*?_(?:ar|br|dm|la|lm|me|pi|sh|sl|sm|sn|ww)_[a-z0-9]+)_(.+)$', re.I)
QUALITY = re.compile(r'(\.[a-z]{1,5}\.\d{1,3}\.\d{4,6}\.[a-z0-9_]+)$', re.I)
ENDING_NUMBER = re.compile(r'_(\d{1,3})$')
NUMBER = re.compile(r'\d{1,3}(?=_|$)')
XAR1 = {'xanim': (0x4536d0ac29cd9c5b, 'rex_vm_ar_xar1_v05_inspect'),
        'animpkg': (0x51a6030f083f49c2, 'rex_ar_xar1_v05')}
INSPECT = re.compile(r'wfoly_rex_plr_ar_xar1_v05_inspect_\d\d$', re.I)
CHUNK = 65_536


def write_rows(path, header, rows):
    with path.open('w',encoding='utf-8',newline='') as f:
        w=csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


class Targets:
    def __init__(self, regex, baseline, pool):
        self.regex=regex
        self.before=baseline
        self.pool=pool
        self.refs=Counter()
        self.first={}
        self.new={}

    def note(self, field, context):
        m=self.regex.fullmatch(field)
        if not m:
            return None
        key=int(m[1],16)&core.MASK
        self.refs[key]+=1
        self.first.setdefault(key,context)
        return key

    def left(self):
        return self.refs.keys()-self.before.keys()-self.new.keys()

    def add(self, key, name, source):
        if key in self.refs and key not in self.before:
            self.new.setdefault(key,(name,source))

    def stats(self):
        return {'placeholder_references':sum(self.refs.values()),'unique_keys':len(self.refs),
                'within_snapshot':len(self.refs.keys()&self.pool),
                'known_before':len(self.refs.keys()&self.before.keys()),
                'recovered':len(self.new),'remaining':len(self.left())}


def scan(target, kind, candidates, label):
    pending=target.left()
    if not pending:
        return
    attempts=0
    emitted=candidates
    with ExitStack() as context:
        try:
            cl=OpenCL(context)
        except GpuError:
            cl=None
        if cl is not None:
            table,mask=index_keys(pending)
            table=cl.buffer(context,table)
        for part in iter(lambda:list(islice(emitted,CHUNK)),[]):
            words=list(dict.fromkeys(n for n in part if n and len(n)<320))
            if not words:
                continue
            attempts+=len(words)
            if cl is None:
                indices=range(len(words))
            else:
                with ExitStack() as buffers:
                    raw,offsets=pack_words(['',*words,''])
                    text=cl.buffer(buffers,raw)
                    offsets=cl.buffer(buffers,offsets)
                    indices=cl.launch(text,offsets,1,len(words)+1,1,1,core.IW_BASIS,table,mask,0,len(words))
            for i in indices:
                name=words[i].translate(core.ASCII_FOLD)
                k=core.name_key(name,kind)
                if k in pending:
                    target.add(k,name,label)
    print(f'{label}: {attempts:,} candidates; {len(target.new):,} verified so far',flush=True)


def scan_suffixes(target, stems, suffixes):
    """Try real sound encoding/locale suffixes without materializing the product."""
    pending=target.left()
    if not pending:
        return
    suffixes=sorted(suffixes)
    stems=sorted(stems)
    tail_count=len(suffixes)
    with ExitStack() as resources:
        try:
            cl=OpenCL(resources)
        except GpuError as exc:
            print(f'Profile sweep skipped without GPU: {exc}',flush=True)
            return
        data,mask=index_keys(pending)
        table=cl.buffer(resources,data)
        for i in range(0,len(stems),16_384):
            chunk=stems[i:i+16_384]
            with ExitStack() as buffers:
                blob,offsets=pack_words(['',*chunk,*suffixes])
                text=cl.buffer(buffers,blob)
                offsets=cl.buffer(buffers,offsets)
                count=len(chunk)*tail_count
                for start in range(0,count,WORK_SIZE):
                    end=min(count,start+WORK_SIZE)
                    hits=cl.launch(text,offsets,1,len(chunk)+1,1,tail_count,core.IW_BASIS,table,mask,start,end)
                    for hit in hits:
                        name=(chunk[hit//tail_count]+suffixes[hit%tail_count]).translate(core.ASCII_FOLD)
                        key=core.name_key(name,'sound_asset')
                        if key not in pending:
                            raise ValueError('Sound profile sweep result did not reproduce on CPU.')
                        target.add(key,name,'sound quality and locale profile sweep')
    print(f'sound profile sweep: {len(stems)*tail_count:,} candidates; {len(target.new):,} verified so far',flush=True)


def sound_profiles(names):
    stems=set()
    tails=Counter()
    for name in names:
        m=QUALITY.search(name)
        if not m:
            continue
        stem=name[:m.start()]
        stems.update((stem,stem.replace('/','.')))
        tails[m[1]]+=1
    return stems,{t for t,_ in tails.most_common(256)}


def nearby_sound_stems(names, pool):
    pattern=re.compile(r'(?<![0-9])\d{1,5}(?=_|$)')
    stems=set()
    for key,name in names.items():
        if key not in pool:
            continue
        tail=QUALITY.search(name)
        if not tail:
            continue
        body=name[:tail.start()]
        leaf=body.rsplit('/' if '/' in body else '.',1)[-1]
        origin=len(body)-len(leaf)
        for m in list(pattern.finditer(leaf))[-1:]:
            value=int(m[0])
            if not 0<=value<10000:
                continue
            for number in range(max(0,value-2),value+3):
                for width in {len(m[0]),1}:
                    raw=str(number).zfill(width)
                    if raw==m[0]:
                        continue
                    variant=body[:origin+m.start()]+raw+body[origin+m.end():]
                    stems.update((variant,variant.replace('/','.')))
    return stems


def sound_parts(name):
    head,_,base=name.rpartition('/')
    end=QUALITY.search(base)
    return (head,base[:end.start()],end[1]) if end else None


def sound_spellings(path, stem, tail):
    yield (path+'/' if path else '')+stem+tail
    if path:
        yield path.replace('/','.')+'.'+stem+tail


def numeric_neighbors(stem):
    spans=list(NUMBER.finditer(stem))
    for m in spans[-2:]:
        given=int(m[0])
        for n in range(max(0,given-3),given+4):
            if n!=given:
                yield stem[:m.start()]+str(n).zfill(len(m[0]))+stem[m.end():]
    if not spans or spans[-1].end()!=len(stem):
        for n in range(1,7):
            yield f'{stem}_{n:02d}'


def anim_candidates(blocks, suffixes, target, package_names):
    for file, prefix, slots in blocks:
        if not prefix:
            continue
        pkg=package_names.get(file,file)
        variant=''
        m=WEAPON.match(pkg)
        if m and m[2]!='animpackage' and not ANIM.fullmatch(m[2]):
            variant=m[2]
        for slot,key in slots:
            if key in target.before or key in target.new:
                continue
            for end,_ in suffixes[slot].most_common(100):
                yield f'{prefix}_{end}'
                if variant and not end.startswith(variant+'_'):
                    yield f'{prefix}_{variant}_{end}'
                    yield f'{prefix}_{end}_{variant}'
    # Validate package variants from the name of their owning package.
    for file,prefix,slots in blocks:
        if prefix:
            yield from (f'{prefix}_{v}_{action}' for v in ('v05','apex','v0','v01','v02','v03','v04','v06','v07')
                        for action in ('inspect','inspect_empty','idle','idle_non_loop','melee_hit_01',
                                       'melee_hit_02','melee_hit_03','melee_miss_01','melee_miss_02','melee_miss_03',
                                       'melee_fatal_01','melee_fatal_02','melee_fatal_03'))


def anim_nearby(names, pool):
    action=re.compile(r'_(?:inspect|fire|reload|melee|idle|pose|raise|altmode|sprint|loco|rechamber)(?:_|$)')
    num=re.compile(r'(?<=_)(?:v|p)?(\d{1,3})(?=_|$)')
    for key,name in names.items():
        if key not in pool:
            continue
        for m in list(num.finditer(name))[-2:]:
            n=int(m[1])
            for other in range(max(0,n-3),n+4):
                if other!=n:
                    yield name[:m.start(1)]+str(other).zfill(len(m[1]))+name[m.end(1):]
        m=action.search(name)
        if m and WEAPON.match(name):
            for v in ('v01','v02','v03','v04','v05','v06','v07','v08','v09','v10','v11','v12','apex',
                      'hyperburst','bp','rare','special'):
                yield f'{name[:m.start()]}_{v}{name[m.start():]}'
                yield f'{name}_{v}'
            if name.startswith('rex_vm_'):
                yield 'rex_wm_'+name[len('rex_vm_'):]


def bank_candidates(exports, target, phase):
    for f in exports:
        vals=json.loads(f.read_bytes())
        if not isinstance(vals,list):
            raise ValueError(f'Unexpected sound bank structure: {f}')
        seen=set()
        missing=set()
        profiles=Counter()
        by_alias=defaultdict(set)
        families=defaultdict(set)
        for row in vals:
            snd=row.get('snd')
            alias=row.get('alias')
            if not isinstance(snd,str) or not isinstance(alias,str):
                continue
            m=SOUND.fullmatch(snd)
            if m:
                key=int(m[1],16)&core.MASK
                if key not in target.before and key not in target.new and alias:
                    missing.add(alias)
                continue
            bits=sound_parts(snd)
            if bits:
                path,stem,tail=bits
                profiles[path,tail]+=1
                by_alias[alias].add(bits)
                families[ENDING_NUMBER.sub('',alias)].add(bits)
                seen.add(bits)
        if not missing:
            continue
        if phase=='neighbor':
            for path,stem,tail in seen:
                for candidate in numeric_neighbors(stem):
                    yield from sound_spellings(path,candidate,tail)
                for alt in ('ln','lnn','tnn','vnn'):
                    alt_tail=re.sub(r'^\.[a-z]{1,5}\.',f'.{alt}.',tail)
                    if alt_tail!=tail:
                        yield from sound_spellings(path,stem,alt_tail)
        else:
            for alias in missing:
                variants={alias,alias.replace('wfoly_rex_','wfoly_',1)}
                for name in tuple(variants):
                    stem=ENDING_NUMBER.sub('',name)
                    variants.add(stem)
                    variants.update(f'{name}_{n:02d}' for n in range(1,5))
                for path,tail in (p for p,_ in profiles.most_common(4)):
                    for stem in variants:
                        yield from sound_spellings(path,stem,tail)
                common=families.get(ENDING_NUMBER.sub('',alias),set())
                for path,stem,tail in sorted(by_alias[alias]|common)[:20]:
                    for other in numeric_neighbors(stem):
                        yield from sound_spellings(path,other,tail)


def export_bank_targets(files,target):
    inspect=defaultdict(set)
    for f in files:
        vals=json.loads(f.read_bytes())
        if not isinstance(vals,list):
            raise ValueError(f'Unexpected sound bank structure: {f}')
        for i,item in enumerate(vals):
            snd=item.get('snd')
            alias=item.get('alias')
            if isinstance(snd,str):
                key=target.note(snd,f'{f.name}:{i+1}')
                if isinstance(alias,str) and INSPECT.fullmatch(alias):
                    inspect[alias].add((key,snd))
    return inspect


def run(export,snapshot,tables,saluki,out,prior_animpkg=None,extended=False):
    out.mkdir(parents=True,exist_ok=False)
    pooled=('xanim','animpkg','sound_asset')
    _,pools,_=core.snapshot_data(snapshot.read_bytes(),snapshot.with_suffix('.pools.txt').read_text(encoding='utf-8-sig'),pooled)
    bases={k:core.read_cdb(saluki/(core.TABLE[k]+'.cdb')) for k in pooled}
    if prior_animpkg:
        bases['animpkg']=core.read_cdb(prior_animpkg)|bases['animpkg']
    package_names={key&core.MASK:name for key,name in bases['animpkg'].items()}
    for key,name in core.names_csv(tables/'fnv1a_animpkgs_v2.csv'):
        package_names.setdefault(key&core.MASK,name)
    package_names[XAR1['animpkg'][0]]=XAR1['animpkg'][1]
    resolved_packages={f'animpkg_{key:x}':name for key,name in package_names.items()}
    animation=Targets(ANIM,bases['xanim'],pools['xanim'])
    sound=Targets(SOUND,bases['sound_asset'],pools['sound_asset'])
    blocks=[]
    hints=defaultdict(Counter)
    anim_files=sorted((export/'animpkgs').rglob('*.json'))
    sound_files=sorted((export/'sndbanks/json').rglob('*.json'))
    if not anim_files or not sound_files:
        raise ValueError('No animation package or sound bank JSON exports were found.')
    for f in anim_files:
        values=json.loads(f.read_bytes()).get('anims',{})
        pref=Counter()
        slots=[]
        for slot, name in values.items():
            if not isinstance(name,str):
                continue
            key=animation.note(name,f'{f.name}:{slot}')
            if key is not None:
                slots.append((slot,key))
                continue
            m=WEAPON.match(name)
            if m:
                pref[m[1]]+=1
                hints[slot][m[2]]+=1
        blocks.append((f.stem,pref.most_common(1)[0][0] if pref else None,slots))
    inspect=export_bank_targets(sound_files,sound)
    print('Initial animation audit',animation.stats(),flush=True)
    print('Initial sound audit',sound.stats(),flush=True)
    for kind,target in [('xanim',animation),('sound_asset',sound)]:
        for key,name in core.names_csv(tables/(core.TABLE[kind]+'.csv')):
            if key in target.refs and key not in target.before:
                for exact in (name,name.replace('/','.')):
                    if core.name_key(exact,kind)==key:
                        target.add(key,exact,'cod-name-db')
                        break
    ak,an=XAR1['xanim']
    pk,pn=XAR1['animpkg']
    assert ak in animation.refs and ak in pools['xanim'] and pk in pools['animpkg']
    assert core.name_key(an,'xanim')==ak and core.name_key(pn,'animpkg')==pk
    animation.add(ak,an,'xar1 v05 inspect slots + CAST events')
    scan(animation,'xanim',anim_candidates(blocks,hints,animation,resolved_packages),'animation package slot templates')
    scan(sound,'sound_asset',bank_candidates(sound_files,sound,'neighbor'),'sound sibling names and encoding variants')
    scan(sound,'sound_asset',bank_candidates(sound_files,sound,'alias'),'sound bank alias and directory templates')
    if extended:
        scan(animation,'xanim',anim_nearby(bases['xanim'],pools['xanim']),'animation variants and nearby indices')
        stems,suffixes=sound_profiles(bases['sound_asset'].values())
        scan_suffixes(sound,stems,suffixes)
        updated=bases['sound_asset']|{k:v[0] for k,v in sound.new.items()}
        _,suffixes=sound_profiles(updated.values())
        scan_suffixes(sound,nearby_sound_stems(updated,pools['sound_asset']),suffixes)
    return animation,sound,inspect,anim_files,sound_files,bases


def deliver(export,out,animation,sound,inspect,anim_files,sound_files,bases):
    animpkg=XAR1['animpkg']
    all_new={k:{h:n for h,(n,_) in target.new.items()}
             for k,target in [('xanim',animation),('sound_asset',sound)]}
    if animpkg[0] not in bases['animpkg']:
        all_new['animpkg']={animpkg[0]:animpkg[1]}
    for k,rows in all_new.items():
        if not rows:
            continue
        merged=bases[k]|rows
        path=out/'saluki-ready/hash_pkg'/(core.TABLE[k]+'.cdb')
        core.write_cdb(path,merged)
        assert core.read_cdb(path)==merged
    write_rows(out/'new_names.csv',('type','hash','name'),
               ((k,f'{h:016x}',n) for k,rows in all_new.items() for h,n in sorted(rows.items())))
    for label,t in [('animation',animation),('sound',sound)]:
        write_rows(out/(label+'-unresolved.csv'),('hash','first_export_reference','references','in_snapshot'),
                   ((f'{k:016x}',t.first[k],t.refs[k],int(k in t.pool)) for k in sorted(t.left())))
    soundmap=sound.before|all_new['sound_asset']
    write_rows(out/'xar1-v05-inspect-sounds.csv',('alias','sound_hash','filename','state'),
               ((alias,f'{key:016x}' if key is not None else '',soundmap.get(key,raw),
                 'named' if key is None or key in soundmap else 'unresolved')
                for alias,rows in sorted(inspect.items()) for key,raw in sorted(rows,key=lambda x:(x[0] is None,x[1]))))
    file=export/'animations/xar1/anim_4536d0ac29cd9c5b.cast'
    if file.is_file():
        shutil.copy2(file,out/(XAR1['xanim'][1]+'.cast'))
    package=export/'animpkgs/animpkg_51a6030f083f49c2.json'
    if package.is_file():
        data=json.loads(package.read_bytes())
        all_names=animation.before|all_new['xanim']
        for slot,value in data['anims'].items():
            m=ANIM.fullmatch(value)
            if m:
                data['anims'][slot]=all_names.get(int(m[1],16)&core.MASK,value)
        (out/(animpkg[1]+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    summary={'animpkg_files':len(anim_files),'sound_bank_files':len(sound_files),
             'animation':animation.stats(),'sound_file':sound.stats(),
             'inspect_events':len(inspect),
             'inspect_events_with_named_sound':sum(any(key is None or key in soundmap for key,_ in rows)
                                                    for rows in inspect.values())}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False),flush=True)


def recover_aliases(export, snapshot, saluki, out, prior_alias=None):
    """Use plaintext event names from banks and check the captured full keys."""
    fixed=saluki/'fnv1a_soundbanks_aliases_v2.cdb'
    installed=core.read_cdb(fixed)
    baseline=(core.read_cdb(prior_alias) if prior_alias else {})|installed
    capture=core.alias_raw(str(snapshot.parent/'sound_alias.raw64.txt.gz'))
    _,pools,_=core.snapshot_data(snapshot.read_bytes(),
                                 snapshot.with_suffix('.pools.txt').read_text(encoding='utf-8-sig'),('sound_alias',))
    pool=pools['sound_alias']
    assert capture and all(h&core.MASK in pool for h in capture)
    exported=set()
    event_summary={}
    for file in sorted((export/'sndbanks/json').rglob('*.json')):
        for item in json.loads(file.read_bytes()):
            for k in ('alias','alias2'):
                n=item.get(k)
                if isinstance(n,str) and n.strip():
                    exported.add(n)
    new={}
    for name in sorted(exported):
        hash64=core.name_key(name,'sound_alias')
        if hash64 in capture and hash64 not in baseline:
            new.setdefault(hash64,name.translate(core.ASCII_FOLD))
        if INSPECT.fullmatch(name):
            event_summary[name]={'hash64':f'{hash64:016x}','verified':hash64 in capture,
                                 'present_before':hash64 in baseline}
    assert len(event_summary)==23 and all(v['verified'] for v in event_summary.values())
    if new:
        merged=baseline|new
        dest=out/'saluki-ready/hash_pkg'/fixed.name
        core.write_cdb(dest,merged)
        assert core.read_cdb(dest)==merged
    write_rows(out/'sound-alias-new-names.csv',('type','hash','name'),
               (('sound_alias',f'{k:016x}',name) for k,name in sorted(new.items())))
    info={'new_aliases':len(new),'inspect_aliases':event_summary,
          'inspect_aliases_added':sum(int(v['hash64'],16) in new for v in event_summary.values())}
    (out/'sound-alias-summary.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'Added {len(new):,} full-width aliases, including {info["inspect_aliases_added"]} xar1 inspect events.',flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('export','snapshot','tables','saluki','out'):
        p.add_argument(name,type=Path)
    p.add_argument('--prior-animpkg',type=Path)
    p.add_argument('--prior-alias',type=Path)
    p.add_argument('--alias-only',action='store_true')
    p.add_argument('--extended',action='store_true')
    a=p.parse_args()
    if a.alias_only:
        a.out.mkdir(parents=True,exist_ok=True)
        recover_aliases(a.export,a.snapshot,a.saluki,a.out,a.prior_alias)
    else:
        deliver(a.export,a.out,*run(a.export,a.snapshot,a.tables,a.saluki,a.out,a.prior_animpkg,a.extended))


if __name__=='__main__':
    main()
