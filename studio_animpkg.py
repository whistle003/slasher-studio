"""Confirm names for IDs witnessed in MW7 exports and merge them into Saluki CDBs."""

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from itertools import chain
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from uuid import uuid4

import studio_core as core


PACKAGE = re.compile(r'animpkg_([0-9a-f]{1,16})', re.I)
ANIMATION = re.compile(r'anim_([0-9a-f]{1,16})', re.I)
WEAPON = re.compile(r'^(?P<lead>.+?)_(?P<group>ar|br|dm|eq|la|lm|me|pi|sh|sl|sm|sn|ww)_(?P<weapon>[a-z0-9]+)(?:_(?P<tail>.*))?$')
VARIANT = re.compile(r'(?:^|_)(v[0-9]{1,3}|apex|bp_[a-z0-9]+)(?=_|$)')
FOLEY = re.compile(rb'wfoly_([a-z0-9]+)_(?:plr|npc)_([a-z0-9]+_[a-z0-9]+)_([a-z0-9]+(?:_[a-z0-9]+)*)(?![a-z0-9_])')
EVENT_ACTION = re.compile(r'(?:^|_)((?:branched_|transition_)?reload|inspect)(?=_|$)')
EVENT_NUMBER = re.compile(r'_\d{1,3}$')
PROFILE_TOKENS = re.compile(
    r'^(?:(?:apex|v\d{1,3}|drum|xmag|xmaglrg|smag|ext0[12]|akimbo(?:_[lr])?|ubsh|ubgl|irons|stocksaw|breacher)_)+')
KINDS = ('animpkg', 'xanim')
CONFIRMED = 'confirmed_names.csv'
CDB_DIR = Path('saluki-ready/hash_pkg')
FLAT_TAILS = ('', 'animpackage', 'anim_package', 'package', 'base', 'default', 'apex',
              'altmode', 'carry', 'carry_hostage', 'inspect', 'mp', 'sp')
INSPECT_SLOTS = {'2352': ('inspect',), '2356': ('inspect', 'inspect_empty')}
SLOT_WORDS = '动作槽位词源.csv'
PAIRED_WORDS = '同包动作迁移.csv'
WORD = re.compile(r'[a-z0-9]+(?:_[a-z0-9]+)*')


@dataclass
class Package:
    path: Path
    key: int | None
    families: tuple
    variants: tuple
    references: dict
    named_clips: dict


@dataclass(frozen=True)
class Report:
    packages: int
    packages_confirmed: int
    packages_unresolved: int
    clips_confirmed: int
    clips_unresolved: int
    packages_added: int
    clips_added: int


def target_id(pattern, value):
    m=pattern.fullmatch(value)
    return int(m[1],16)&core.MASK if m else None


def extract_family(value):
    m=WEAPON.fullmatch(value.lower())
    if not m:
        return None
    lead,group,weapon,tail=m.groups()
    return (group,weapon),lead,tail or ''


@lru_cache(maxsize=1)
def load_word_sources(root):
    slots=defaultdict(dict)
    pairs=defaultdict(lambda:defaultdict(dict))
    with (root/SLOT_WORDS).open(encoding='utf-8-sig',newline='') as stream:
        for row in csv.DictReader(stream):
            action=row['action']
            if WORD.fullmatch(action):
                slots[row['slot']][action]=int(row['verified_weapon_families'])
    with (root/PAIRED_WORDS).open(encoding='utf-8-sig',newline='') as stream:
        for row in csv.DictReader(stream):
            source,target=row['source_action'],row['target_action']
            if WORD.fullmatch(source) and WORD.fullmatch(target):
                pairs[row['target_slot']][row['source_slot'],source][target]=int(row['verified_weapon_families'])
    return slots,pairs


def clip_templates(name):
    """Find the action while retaining the position of the clip's variant."""
    parsed=extract_family(name)
    if not parsed:
        return
    family,lead,tail=parsed
    stem='_'.join((lead,*family))+'_'
    if tail:
        yield family,stem,tail,''
    for variant in VARIANT.finditer(tail):
        if variant.start()==0 and tail[variant.end():].startswith('_'):
            yield family,stem+tail[:variant.end()+1],tail[variant.end()+1:],''
        elif variant.start()>0 and variant.end()==len(tail):
            yield family,stem,tail[:variant.start()],tail[variant.start():]


def event_prefix(tail):
    match=EVENT_ACTION.search(tail)
    return (tail[:match.start(1)],tail[match.start(1):]) if match else None


def inventory(root, check):
    packages=[]
    slots=defaultdict(Counter)
    views=defaultdict(Counter)
    embedded={}
    for file in sorted(root.rglob('*.json')):
        check()
        if not file.is_file() or not file.resolve().is_relative_to(root):
            continue
        key=target_id(PACKAGE,file.stem)
        try:
            data=json.loads(file.read_bytes())
        except (ValueError,UnicodeError):
            if key is not None:
                raise ValueError(f'无法读取动画包 JSON：{file.name}') from None
            continue
        if not isinstance(data,dict) or not isinstance(data.get('anims'),dict):
            if key is not None:
                raise ValueError(f'动画包缺少 anims 字段：{file.name}')
            continue
        families=Counter()
        variants=set()
        references={}
        named_clips={}
        for slot,name in data['anims'].items():
            if not isinstance(name,str) or not name:
                continue
            k=target_id(ANIMATION,name)
            if k is not None:
                references[slot]=k
                continue
            embedded.setdefault(core.name_key(name,'xanim'),name)
            named_clips[slot]=name
            f=extract_family(name)
            if not f:
                continue
            family,lead,tail=f
            if not tail:
                continue
            # Idle and fire slots identify the owning weapon more reliably than
            # the reused traversal/gesture clips found in many packages.
            weight=4 if slot in ('69','71','73','74','100','349','2352','2356') else 1
            families[family]+=weight
            variants.update(m[1] for m in VARIANT.finditer(tail))
            slots[slot][tail]+=1
            if 'vm' in lead.split('_') or 'wm' in lead.split('_'):
                views[family][lead]+=weight
        packages.append(Package(file,key,tuple(k for k,_ in families.most_common(6)),
                                tuple(sorted(variants)),references,named_clips))
    if not packages:
        raise ValueError('所选目录里没有含 anims 的 Saluki 动画包 JSON。')
    return packages,slots,views,embedded


def clip_files(root, wanted, check):
    files=[]
    if root is None or not wanted:
        return files
    for path in sorted(root.rglob('*.cast')):
        check()
        key=target_id(ANIMATION,path.stem)
        if key in wanted and path.is_file() and path.resolve().is_relative_to(root):
            files.append((key,path))
    return files


def read_clip_hints(files, check):
    hints=defaultdict(set)
    for key,path in files:
        check()
        with path.open('rb') as f:
            sample=f.read(8*1024*1024)
        for match in FOLEY.finditer(sample):
            source,weapon,tail=(part.decode('ascii') for part in match.groups())
            prefix,action=event_prefix(tail) or ('',tail)
            hints[key].add((source,tuple(weapon.split('_',1)),prefix.rstrip('_'),action))
    return hints


def clip_hints(root, wanted, check):
    return read_clip_hints(clip_files(root,wanted,check),check)


def load_tables(original, csv_dirs, cdb_dirs, log):
    maps={}
    baselines={}
    for kind in KINDS:
        filename=core.TABLE[kind]
        master=original/(filename+'.cdb')
        if not master.is_file():
            raise ValueError(f'Saluki 原库缺少 {master.name}：{original}')
        baseline=core.read_cdb(master)
        if not baseline:
            raise ValueError(f'Saluki 原库没有名称：{master}')
        baselines[kind]=baseline
        known={}
        for folder in csv_dirs:
            p=folder/(filename+'.csv')
            if p.is_file():
                known.update(core.names_csv(p))
        for folder in cdb_dirs:
            p=folder/(filename+'.cdb')
            if p.is_file() and p.resolve()!=master.resolve():
                known.update(core.read_cdb(p))
        # Keep the local library separate: synchronized results can supply a
        # match that Saluki still lacks, and unrelated remote rows stay out.
        maps[kind]=known
    log(f'原库：{len(baselines["animpkg"]):,} 个动画包名、{len(baselines["xanim"]):,} 个动画名（{original}）。')
    return baselines,maps


def known_for(kind, ids, values):
    return {k:values[k] for k in ids if k in values and core.name_key(values[k],kind)==k}


def find_key(kind, key, candidates, check, seen=None):
    if seen is None:
        seen=set()
    for i,name in enumerate(candidates):
        if i%1024==0:
            check()
        if name in seen:
            continue
        seen.add(name)
        if core.name_key(name,kind)==key:
            return name
    return None


class Matcher:
    def __init__(self, packages, slots, views, hints, known, *, use_words=True):
        self.packages=packages
        self.slots=slots
        self.views=views
        self.hints=hints
        self.slot_words,self.paired_words=(load_word_sources(core.ASSETS/'animpkg'/'word-sources')
                                           if use_words else ({},{}))
        self.stems=defaultdict(Counter)
        self.native=defaultdict(Counter)
        self.common=Counter()
        packages_named=Counter(known['animpkg'].values())
        packages_named.update(p.path.stem for p in packages if p.key is None)
        for name,weight in packages_named.items():
            src=extract_family(name)
            if src:
                family,lead,tail=src
                self.stems[family][lead]+=weight
                if tail:
                    self.native[family][tail]+=weight
                    self.common[tail]+=weight

    def foreshadow(self, package):
        local=set()
        for key in set(package.references.values()):
            local.update(self.hints.get(key,()))
        return sorted(local)

    def package_candidates(self, package):
        hints=self.foreshadow(package)
        # A clip notify can expose exactly the variant obscured in the JSON.
        for lead,family,variant,_ in hints:
            yield '_'.join(filter(None,(lead,*family,variant)))
        families=list(dict.fromkeys([f for _,f,_,_ in hints]+list(package.families)))
        for family in families:
            seeds=list(self.stems[family])
            for lead,_ in self.views[family].most_common(4):
                cleaned='_'.join(x for x in lead.split('_') if x not in ('vm','wm'))
                if cleaned:
                    seeds.append(cleaned)
            leads=list(dict.fromkeys([*seeds,'rex','sat','t10','jup','iw9','']))
            tails=list(dict.fromkeys([*package.variants,*FLAT_TAILS,*self.native[family],
                                      *(f'v{i:02d}' for i in range(100)),
                                      *(t for t,_ in self.common.most_common(180))]))
            for lead in leads:
                stem='_'.join(filter(None,(lead,*family)))
                for tail in tails:
                    yield f'{stem}_{tail}' if tail else stem
                    if tail and not tail.startswith('animpackage'):
                        yield f'{stem}_animpackage_{tail}'

    def event_candidates(self, key):
        for lead,family,profile,action in sorted(self.hints.get(key,())):
            stem='_'.join((lead,'vm',*family))
            prefix=stem+'_'+(profile+'_' if profile else '')
            shortened=EVENT_NUMBER.sub('',action)
            if action.startswith('inspect'):
                # Inspect notifies usually end in an event number.
                yield prefix+shortened
                yield prefix+'xmag_'+shortened
                for tail in dict.fromkeys((action,shortened)):
                    while tail:
                        yield prefix+tail
                        if profile:
                            yield f'{stem}_{tail}_{profile}'
                        tail=tail.rpartition('_')[0]
            elif 'reload' in action.split('_'):
                # Reload can use the same event in hip-fire and ADS versions.
                for tail in dict.fromkeys((action,shortened)):
                    yield prefix+tail
                    yield prefix+tail+'_ads'
            else:
                # Raise, selectsemi and altmode events can carry a complete clip stem.
                for view in ('vm','wm'):
                    stem='_'.join((lead,view,*family))+'_'
                    yield stem+action
                    yield stem+shortened

    def event_slot_candidates(self, slot, prefixes):
        words=self.slot_words.get(slot,{})
        for word in sorted(words,key=lambda item:(-words[item],item)):
            for prefix in prefixes:
                yield prefix+word

    def clip_candidates(self, package, name, slot):
        tails=list(dict.fromkeys([*INSPECT_SLOTS.get(slot,()),*[t for t,_ in self.slots[slot].most_common(32)]]))
        yield from self.family_candidates(package,name,tails)

    def family_candidates(self, package, name, tails):
        if not tails:
            return
        identified=extract_family(name) if name else None
        families=list(package.families[:3])
        variants=[]
        if identified:
            family,lead,tail=identified
            families=[family,*[f for f in families if f!=family]]
            # Drop the generic package suffix; the remaining text is the variant.
            tail=tail.removeprefix('animpackage_')
            if tail not in FLAT_TAILS or tail=='apex':
                variants.append(tail)
        variants.extend(package.variants)
        variants.extend(('apex','v01','v02','v03','v04','v05','v06','v07','v08','v09','v10'))
        for family in families:
            views=[v for v,_ in self.views[family].most_common(4)]
            if identified and family==identified[0]:
                lead=identified[1]
                views=[lead if 'vm' in lead.split('_') else lead+'_vm',*views]
            for lead in list(dict.fromkeys([*views,'rex_vm','rex_wm'])):
                stem='_'.join((lead,*family))
                for tail in tails:
                    yield f'{stem}_{tail}'
                    for variant in dict.fromkeys(v for v in variants if v):
                        yield f'{stem}_{variant}_{tail}'
                        yield f'{stem}_{tail}_{variant}'

    def word_candidates(self, package, name, slot, anchors):
        replacements=self.paired_words.get(slot,{})
        words=self.slot_words.get(slot,{})
        if not replacements and not words:
            return
        paired={}
        templates=set()
        families=set(package.families[:3])
        if name and (identified:=extract_family(name)):
            families.add(identified[0])
        for source_slot,family,prefix,action,suffix in anchors:
            for target,support in replacements.get((source_slot,action),{}).items():
                candidate=prefix+target+suffix
                paired[candidate]=max(support,paired.get(candidate,0))
            if family in families:
                templates.add((prefix,suffix))
        yield from sorted(paired,key=lambda candidate:(-paired[candidate],candidate))
        tails=sorted(words,key=lambda word:(-words[word],word))
        for prefix,suffix in sorted(templates):
            for tail in tails:
                yield prefix+tail+suffix
        yield from self.family_candidates(package,name,tails)

    def extend_slots(self, pending, known, found, check, scope):
        slot_words=defaultdict(Counter)
        prefixes=defaultdict(set)

        def observe(package, slot, name, referenced):
            parsed=extract_family(name)
            if not parsed or not parsed[2]:
                return
            family,lead,tail=parsed
            modifier=PROFILE_TOKENS.match(tail)
            slot_words[slot][tail]+=1
            if modifier:
                slot_words[slot][tail[modifier.end():]]+=1
            if referenced:
                stem='_'.join((lead,*family))+'_'
                prefixes[package.path].add(stem)
                if modifier:
                    prefixes[package.path].add(stem+modifier[0])

        def candidates(stems, words):
            for stem in sorted(stems):
                world='wm_'+stem[3:] if stem.startswith('vm_') else stem.replace('_vm_','_wm_',1)
                for word in words:
                    yield stem+word
                    if world!=stem and word.startswith('fingers_'):
                        yield world+word
                        if world.endswith(('_akimbo_l_','_akimbo_r_')):
                            yield world[:-2]+word

        trusted=known|found
        for package in self.packages:
            check()
            for slot,name in package.named_clips.items():
                if scope is None or core.name_key(name,'xanim') in scope:
                    observe(package,slot,name,False)
            for slot,key in package.references.items():
                if key in trusted:
                    observe(package,slot,trusted[key],True)

        added={}
        while True:
            hits={}
            ranked={}
            for key in sorted(pending.keys()-found.keys()-added.keys()):
                check()
                seen=set()
                for package,_,slot in pending[key]:
                    if not prefixes[package.path] or not slot_words[slot]:
                        continue
                    if slot not in ranked:
                        ranked[slot]=[word for word,_ in slot_words[slot].most_common()]
                    names=candidates(prefixes[package.path],ranked[slot])
                    if solved:=find_key('xanim',key,names,check,seen):
                        hits[key]=solved
                        break
            if not hits:
                return added
            added.update(hits)
            for key,name in hits.items():
                for package,_,slot in pending[key]:
                    observe(package,slot,name,True)

    def resolve_clips(self, pending, known, check, log, *, scope=None):
        event_names={}
        for key in self.hints.keys() & pending.keys():
            check()
            if solved:=find_key('xanim',key,self.event_candidates(key),check):
                event_names[key]=solved
        found=dict(event_names)
        event_hits=len(found)
        # An exact event match can establish a different weapon code from
        # the borrowed idle and movement clips in the same animation package.
        event_names.update((key,known[key]) for key in self.hints.keys() & known.keys())
        event_slots=0
        word_hits=0
        anchors={}
        prefixes={}
        for key,refs in pending.items():
            check()
            if key in found:
                continue
            seen=set()
            ordered=sorted(refs,key=lambda ref:not ref[1])
            for package,_,slot in ordered:
                if slot not in self.slot_words:
                    continue
                if package.path not in prefixes:
                    stems=set()
                    for reference in set(package.references.values()) & event_names.keys():
                        if parsed:=extract_family(event_names[reference]):
                            family,lead,tail=parsed
                            if event:=event_prefix(tail):
                                stem='_'.join((lead,*family))+'_'
                                stems.update((stem+event[0],stem))
                    prefixes[package.path]=sorted(stems,key=lambda stem:(-len(stem),stem))
                if candidates:=prefixes[package.path]:
                    if solved:=find_key('xanim',key,self.event_slot_candidates(slot,candidates),check,seen):
                        found[key]=solved
                        event_slots+=1
                        break
            if key in found:
                continue
            for package,name,slot in ordered:
                if slot not in self.slot_words and slot not in self.paired_words:
                    continue
                if package.path not in anchors:
                    siblings=chain(package.named_clips.items(),
                                   ((s,known[k]) for s,k in package.references.items() if k in known))
                    anchors[package.path]=[(s,*template) for s,clip in siblings for template in clip_templates(clip)]
                candidates=self.word_candidates(package,name,slot,anchors[package.path])
                if solved:=find_key('xanim',key,candidates,check,seen):
                    found[key]=solved
                    word_hits+=1
                    break
            if key in found:
                continue
            for package,name,slot in ordered:
                if solved:=find_key('xanim',key,self.clip_candidates(package,name,slot),check,seen):
                    found[key]=solved
                    break
        slot_count=sum(map(len,self.slot_words.values()))
        pair_count=sum(len(words) for targets in self.paired_words.values() for words in targets.values())
        if slot_count or pair_count:
            log(f'MW7 动画包词源：{slot_count:,} 条槽位词、{pair_count:,} 条同包规则；'
                f'{len(pending):,} 个待查动画引用中用词源确认 {word_hits:,} 个。')
        if self.hints:
            log(f'CAST 动作音效：{len(self.hints.keys() & pending.keys()):,} 个待查引用有事件线索，'
                f'直接确认 {event_hits:,} 个；同包槽位再确认 {event_slots:,} 个。')
        if self.slot_words and pending.keys()-found.keys():
            extended=self.extend_slots(pending,known,found,check,scope)
            found.update(extended)
            log(f'已验证同包扩展：确认 {len(extended):,} 个。')
        return found


def write_confirmed(path, confirmed, added):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f)
        w.writerow(('type','hash','name','status'))
        w.writerows((kind,f'{k:016x}',n,'merged' if k in added[kind] else 'known')
                    for kind in KINDS for k,n in sorted(confirmed[kind].items()))


def read_confirmed(path):
    confirmed={k:{} for k in KINDS}
    added={k:{} for k in KINDS}
    with path.open(encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            kind,key,name,status=row['type'],int(row['hash'],16),row['name'],row['status']
            if kind not in KINDS or key!=core.name_key(name,kind) or status not in ('known','merged'):
                raise ValueError('确认清单有无法回算的名称或未知状态。')
            confirmed[kind][key]=name
            if status=='merged':
                added[kind][key]=name
    return confirmed,added


def run(source, clips, output, saluki=None, csv_dirs=(), cdb_dirs=(), check=lambda:None, log=lambda _:None):
    source=Path(source).resolve()
    base=(source/'animpkgs').resolve() if (source/'animpkgs').is_dir() else source
    if not base.is_dir():
        raise ValueError('找不到所选的 Saluki 动画包目录。')
    cliproot=Path(clips).resolve() if clips else base.parent/'animations'
    if clips and not cliproot.is_dir():
        raise ValueError('找不到所选的动画文件目录。')
    if not cliproot.is_dir():
        cliproot=None
    else:
        cliproot=cliproot.resolve()
    original=Path(saluki).resolve() if saluki else base.parent.parent.parent/'hash_pkg'
    if (original/'hash_pkg').is_dir():
        original=(original/'hash_pkg').resolve()
    if not original.is_dir():
        raise ValueError('找不到 Saluki 原库，请指定 Saluki 安装目录或 hash_pkg。')
    output=Path(output).resolve()
    if output.is_relative_to(base) or (cliproot and output.is_relative_to(cliproot)) or output.is_relative_to(original):
        raise ValueError('结果目录不能放在动画输入目录或 Saluki 原库内。')
    bundled=core.ASSETS/'animpkg'
    baselines,maps=load_tables(original,tuple(dict.fromkeys((bundled,*csv_dirs))),cdb_dirs,log)
    log('读取动画包 JSON，只收集文件名和 anims 里的占位 ID。')
    packages,slots,views,embedded=inventory(base,check)
    clip_ids={key for p in packages for key in p.references.values()}
    file_ids={p.key for p in packages if p.key is not None}
    log(f'{len(packages):,} 个动画包；{len(file_ids):,} 个包名 ID、{len(clip_ids):,} 个动画引用 ID 待查。')
    hints=clip_hints(cliproot,clip_ids,check)
    package_names=known_for('animpkg',file_ids,maps['animpkg'])|known_for('animpkg',file_ids,baselines['animpkg'])
    clip_names=(known_for('xanim',clip_ids,embedded)|known_for('xanim',clip_ids,maps['xanim'])|
                known_for('xanim',clip_ids,baselines['xanim']))
    matcher=Matcher(packages,slots,views,hints,{k:maps[k]|baselines[k] for k in KINDS})
    for p in packages:
        check()
        if p.key is not None and p.key not in package_names:
            solved=find_key('animpkg',p.key,matcher.package_candidates(p),check)
            if solved:
                package_names[p.key]=solved
    log(f'包名已确认 {len(package_names):,} / {len(file_ids):,}；用包名和槽位验证动画。')
    unresolved_slot=defaultdict(list)
    for p in packages:
        name=p.path.stem if p.key is None else package_names.get(p.key)
        for slot,k in p.references.items():
            if k not in clip_names:
                unresolved_slot[k].append((p,name,slot))
    clip_names.update(matcher.resolve_clips(unresolved_slot,clip_names,check,log))
    log(f'动画原名已确认 {len(clip_names):,} / {len(clip_ids):,}；将命中记录合进原库副本。')
    confirmed={'animpkg':package_names,'xanim':clip_names}
    added={kind:{k:n for k,n in found.items() if baselines[kind].get(k)!=n}
           for kind,found in confirmed.items()}
    output.mkdir(parents=True,exist_ok=True)
    destination=output/('cdb-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:5])
    with TemporaryDirectory(dir=output,prefix='.cdb-') as temporary:
        stage=Path(temporary)/'output'
        stage.mkdir()
        write_confirmed(stage/CONFIRMED,confirmed,added)
        for kind,rows in added.items():
            check()
            if not rows:
                continue
            full=baselines[kind]|rows
            path=stage/CDB_DIR/(core.TABLE[kind]+'.cdb')
            from studio_pipeline import export_names, convert
            txt=export_names(stage,kind,rows)
            convert(txt,stage/'merged-csv'/(core.TABLE[kind]+'.csv'),path,full,log)
            log(f'{core.LABEL[kind]}：已合入 {len(rows):,} 条，CDB 回读通过。')
        result=Report(len(packages),len(package_names),len(file_ids)-len(package_names),len(clip_names),
                      len(clip_ids)-len(clip_names),len(added['animpkg']),len(added['xanim']))
        check()
        stage.rename(destination)
    return destination,result
