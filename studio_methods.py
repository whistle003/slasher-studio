"""Evidence-bound candidate methods adapted from hash-slinging-slasher (GPL-3.0).

See METHOD_SOURCES.md for original scripts, thresholds and deliberate limits.
Methods generate names; only the task engine may confirm them.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
import itertools
import re

import studio_core as core
import studio_specialized as specialized


VERSION = '2'
KINDS = ('xanim', 'sound_asset', 'sound_alias', 'material', 'image')
TAIL = re.compile(r'(.*)(\.[a-z]{1,4}\d*\.\d{1,3}\.\d{4,6}\.[a-z_]+)$')
NUMBER = re.compile(r'\d{1,3}')


def sound(name):
    m = TAIL.fullmatch(name)
    if not m:
        return None
    head, tail = m.groups()
    at = max(head.rfind('/'), head.rfind('.'))
    return head[:at+1], head[at+1:], tail


def spell(game, folder, stem, tail):
    folder = folder.replace('.', '/').replace('\\','/').rstrip('/')
    return ((folder+'.' if folder else '')+stem+tail if game in ('MODWAR22','YAMYAMOK')
            else (folder+'/' if folder else '')+stem+tail)


def split(kind,name):
    if kind == 'sound_asset':
        p = sound(name)
        if not p:
            return None
        folder,base,tail = p
    else:
        folder,sep,base = name.rpartition('/')
        folder,tail = folder+sep,''
    words = tuple(base.split('_'))
    if not 2 <= len(words) <= 18 or not all(w.isascii() and w.isalnum() for w in words):
        return None
    return folder,words,tail


@dataclass
class Method:
    key: str
    kind: str
    label: str
    deps: tuple
    priority: float
    seeds: list
    emit: object


def prepare(corpus, training, held, game, wanted, check):
    """Build measured rules once; apply them to small durable seed shards."""
    methods=[]
    for kind in KINDS:
        if not wanted.get(kind):
            continue
        present = held.get(kind,set())
        parsed = {n:p for n in present if (p:=split(kind,n))}
        byslot = defaultdict(Counter)
        numbers = defaultdict(set)
        for i,(name,(folder,tokens,tail)) in enumerate(parsed.items()):
            if i%4096 == 0:check()
            for at,word in enumerate(tokens):
                byslot[tokens[0],at][word]+=1
            base = '_'.join(tokens)
            for m in NUMBER.finditer(base):
                numbers[folder,base[:m.start()],base[m.end():],len(m[0]),tail].add(int(m[0]))
        slots = {key:tuple(w for w,n in c.most_common(12) if n>=3) for key,c in byslot.items()}

        def edits(name,slots=slots,kind=kind):
            parts=split(kind,name)
            if not parts:return
            folder,tokens,tail = parts
            for at in range(1,len(tokens)):
                yield folder+'_'.join(tokens[:at]+tokens[at+1:])+tail
                for word in slots.get((tokens[0],at),()):
                    if word!=tokens[at]:
                        yield folder+'_'.join(tokens[:at]+(word,)+tokens[at+1:])+tail
                    yield folder+'_'.join(tokens[:at]+(word,)+tokens[at:])+tail
        methods.append(Method('slot-edits',kind,'有依据的词位替换、插入和删除',(kind,),1.0,sorted(parsed),edits))
        families=sorted(key for key,nums in numbers.items() if len(nums)>=2 and max(nums)-min(nums)<=256)
        def numbered(key,numbers=numbers):
            folder,left,right,width,tail = key
            values=numbers[key]
            for number in range(max(0,min(values)-2),min(999,max(values)+2)+1):
                if number not in values:
                    yield folder+left+str(number).zfill(width)+right+tail
        methods.append(Method('numbered',kind,'录音和动作编号族补缺',(kind,),3.0,families,numbered))

        # Recover the final byte by inverse FNV state; handle both high-bit
        # possibilities for ordinary 63-bit keys and exact alias 64-bit keys.
        inverse=pow(0x100000001b3,-1,1<<64)
        buckets=defaultdict(list)
        for i,key in enumerate(sorted(wanted[kind])):
            if i%8192==0:check()
            for full in (key,) if kind=='sound_alias' else (key,key|(1<<63)):
                before=full*inverse&core.UINT64
                buckets[before>>8].append(before)
        prefixes=sorted({n[:-1] for n in present if n and n[-1].isascii() and kind!='sound_asset'})
        def final(prefix,buckets=buckets,kind=kind):
            basis=core.ALIAS_BASIS if kind=='sound_alias' else core.IW_BASIS
            for byte in prefix.encode():
                basis=(basis^byte)*0x100000001b3&core.UINT64
            for before in buckets.get(basis>>8,()):
                byte=before^basis
                if 32<=byte<127:
                    name=prefix+chr(byte)
                    if name==name.translate(core.ASCII_FOLD):
                        yield name
        methods.append(Method('final-byte',kind,'末字节逆向求解',(kind,),5.0,prefixes,final))

    # One-to-two-token block rules train only on independent table rows.
    if wanted.get('xanim'):
        frames=defaultdict(set)
        def cuts(row):
            for width in (1,2):
                for pos in range(1,len(row)-width):
                    yield row[:pos],row[pos:pos+width],row[pos+width:]
        for i,name in enumerate(sorted(training.get('xanim',()))):
            if i%4096==0:check()
            p=split('xanim',name)
            if p:
                for left,block,right in cuts(p[1]):
                    frames[left,right].add(block)
        support=defaultdict(set)
        for (left,right),blocks in frames.items():
            if not 2<=len(blocks)<=24:continue
            for a,b in itertools.combinations(sorted(blocks),2):
                if len(a)!=len(b) and a[0]!=b[0] and a[-1]!=b[-1]:
                    support[(left[-1],right[0]),a,b].add((left,right))
        rules=defaultdict(set)
        for (anchor,a,b),witness in support.items():
            if len(witness)>=3:
                rules[anchor,a].add(b);rules[anchor,b].add(a)
        def blocks(name,rules=rules):
            p=split('xanim',name)
            if p:
                for left,block,right in cuts(p[1]):
                    for replacement in sorted(rules.get(((left[-1],right[0]),block),())):
                        yield '_'.join(left+replacement+right)
        methods.append(Method('animation-blocks','xanim','动画内部块长度变换',('xanim',),4.0,sorted(held.get('xanim',())),blocks))
        del frames,support

        # Coupled substitutions require three distinct complete sibling frames.
        for gap in (2,3,4,5,6):
            groups=defaultdict(set)
            for i,name in enumerate(sorted(training.get('xanim',set()) & held.get('xanim',set()))):
                if i%4096==0:check()
                p=split('xanim',name)
                if not p:continue
                row=p[1]
                for at in range(len(row)-gap):
                    j=at+gap
                    groups[row[:at],row[at+1:j],row[j+1:]].add((row[at],row[j]))
            support=Counter()
            for choices in groups.values():
                if 2<=len(choices)<=24:
                    for a,b in itertools.combinations(sorted(choices),2):
                        if a[0]!=b[0] and a[1]!=b[1]:support[a,b]+=1
            pair_rules=defaultdict(set)
            for (a,b),count in support.items():
                if count>=3:pair_rules[a].add(b);pair_rules[b].add(a)
            def pairs(name,gap=gap,rules=pair_rules):
                p=split('xanim',name)
                if not p:return
                row=p[1]
                for at in range(len(row)-gap):
                    j=at+gap
                    for a,b in sorted(rules.get((row[at],row[j]),())):
                        new=list(row);new[at]=a;new[j]=b
                        yield '_'.join(new)
            methods.append(Method(f'animation-pairs-{gap}','xanim',f'动画联动词位（间隔 {gap}）',('xanim',),3.5,
                                  sorted(held.get('xanim',())),pairs))
            del groups,support

    sounds = {n:p for n in corpus.get('sound_asset',()) if (p:=sound(n))}
    target = {n:p for n,p in sounds.items() if n in held.get('sound_asset',())}
    tails=Counter(p[2] for p in target.values())
    observed=sorted(t for t,n in tails.items() if n>=3)
    heads=sorted({(f,b) for f,b,t in sounds.values()})
    if wanted.get('sound_asset'):
        def tail_swap(head):
            folder,base=head
            for tail in observed:yield spell(game,folder,base,tail)
        methods.append(Method('sound-tails','sound_asset','跨作品声音词干与本作编码尾部',('sound_asset',),2.5,heads,tail_swap))
        components=[Counter() for _ in range(4)]
        for tail,count in tails.items():
            for c,value in zip(components,tail[1:].split('.')):c[value]+=count
        axes=[sorted(v for v,n in c.items() if n>=5) for c in components]
        def grammar(head):
            folder,base=head
            for parts in itertools.product(*axes):
                tail='.'+'.'.join(parts)
                if tail not in tails:yield spell(game,folder,base,tail)
        methods.append(Method('sound-grammar','sound_asset','声音格式、质量、采样率和语言组合',('sound_asset',),1.5,heads,grammar))

        dirs=defaultdict(set)
        endings=defaultdict(set)
        for folder,base,tail in target.values():
            endings[folder].add(tail)
            tokens=base.split('_')
            for n in range(2,min(8,len(tokens))+1):dirs['_'.join(tokens[:n])].add(folder)
        aliases=sorted(n for n in held.get('sound_alias',()) if '/' not in n and '.' not in n)
        def alias_files(alias):
            words=alias.split('_')
            for n in range(min(8,len(words)),1,-1):
                folders=dirs.get('_'.join(words[:n]),())
                if folders and len(folders)<=20:
                    for folder in sorted(folders):
                        for tail in sorted(endings[folder]):
                            yield spell(game,folder,alias,tail)
                    break
        methods.append(Method('alias-files','sound_asset','别名按同族声音目录定位',('sound_asset','sound_alias'),3.0,aliases,alias_files))
    if wanted.get('sound_alias'):
        bases=sorted({base for folder,base,tail in sounds.values()})
        def file_alias(base):
            yield base
            yield re.sub(r'_\d{1,3}$','',base)
            for title in ('iw9_','jup_','t10_','rex_','sat_'):
                if base.startswith(title):
                    yield base[len(title):]
                    yield re.sub(r'_\d{1,3}$','',base[len(title):])
        methods.append(Method('file-aliases','sound_alias','声音文件名反推播放别名',('sound_asset','sound_alias'),2.0,bases,file_alias))
    for kind in ('xanim','material','image'):
        if wanted.get(kind):
            seeds=sorted(n for n in held.get(kind,()) if specialized.NUMERIC_SLOT.search(n)
                         or any(before in n for before,_ in specialized.TRANSFORMS))
            priority=2.0 if kind=='xanim' else 4.7
            methods.append(Method('visual-variants',kind,'单例编号、通道与模式兄弟名称',(kind,),priority,
                                  seeds,specialized.visual_variants))
    if wanted.get('sound_asset'):
        # Preserve the actual directory and same verified encoding tail. A narrow
        # alphabet and same-tail pairs bound the upstream basename-byte search.
        seeds=sorted({(n[:-len(tail)-1],tail) for n,(_,base,tail) in target.items() if len(base)>1})
        methods.append(Method('sound-basename-byte','sound_asset','声音编码尾部前的文件名变体',
                              ('sound_asset',),2.8,seeds,specialized.sound_basename_byte))
    if wanted.get('image'):
        verified=training.get('image',set()) & held.get('image',set())
        rules=specialized.image_wrapper_rules(verified,check)
        seeds=sorted(n for n in held.get('image',()) if any(w in rules for _,w in specialized.image_cuts(n)))
        def image_wrappers(name):
            yield from specialized.image_replacements(name,rules)
        methods.append(Method('image-joint-wrappers','image','贴图前后缀成对替换',('image',),5.1,seeds,image_wrappers))
    return methods
