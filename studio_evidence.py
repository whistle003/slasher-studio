"""Recover names from input evidence, then verify against the typed capture."""
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import re

import studio_animpkg as animpkg
import studio_core as core
import studio_zone as zone


KINDS = ('xanim', 'sound_asset', 'sound_alias', 'animpkg')
SOUND_ID = re.compile(r'sound_([0-9a-f]{1,16})', re.I)
ALIAS_ID = re.compile(r'(?:alias|sound_alias|hash)_[0-9a-f]{1,16}', re.I)
SOUND_TAIL = re.compile(r'\.[a-z]{1,5}\.\d{1,3}\.\d{4,6}\.[a-z0-9_]+\Z')
NUMBERED = re.compile(r'_(\d{1,3})\Z')
SAFE = re.compile(r'[a-z0-9_./\\$@+\-]{3,240}\Z')


def normalize(value):
    name = value.strip().translate(core.ASCII_FOLD)
    return name if SAFE.fullmatch(name) else ''


def keys(ctx, kind):
    return ctx.raw if kind == 'sound_alias' else ctx.pools.get(kind, set())


def verified(ctx, kind, name, expected=None):
    name = normalize(name)
    if not name:
        return None
    key = core.name_key(name, kind)
    if expected is not None and expected != key:
        return None
    if key in keys(ctx, kind) and key & core.MASK in ctx.pools.get(kind, ()):
        return key, name
    return None


def within(root, pattern):
    return sorted(p for p in root.rglob(pattern) if p.is_file() and p.resolve().is_relative_to(root))


def baseline(ctx, kind, latest=None):
    path = ctx.table_dir / (core.TABLE[kind] + '.csv')
    data = dict(core.names_csv(path)) if path.is_file() else {}
    for directory in dict.fromkeys(p for p in (latest,ctx.saluki_dir) if p):
        path = directory / (core.TABLE[kind] + '.cdb')
        if path.is_file():
            data.update(core.read_cdb(path))
    return data


def animation_rows(ctx, root, rows, check, log, *, latest=None, clips=None, excluded=()):
    packages, slots, views, embedded = animpkg.inventory((root/'animpkgs').resolve(), check)
    clip_ids = {k for p in packages for k in p.references.values()} & keys(ctx, 'xanim')
    targets=clip_ids if clips is None else clip_ids & {key for key,_ in clips}
    ids = {p.key for p in packages if p.key is not None}
    pkg_table = baseline(ctx, 'animpkg', latest)
    pkg_names = animpkg.known_for('animpkg', ids, pkg_table)
    clip_table = baseline(ctx, 'xanim', latest) if clip_ids else {}
    clip_names = animpkg.known_for('xanim', clip_ids, clip_table)
    for kind, names in (('xanim', embedded), ('animpkg', {core.name_key(p.path.stem, 'animpkg'):p.path.stem
                                                        for p in packages if p.key is None})):
        for key, name in names.items():
            if result := verified(ctx, kind, name, key):
                if clips is None or (kind=='xanim' and key in targets and key not in excluded):
                    rows[kind][key] = result[1]
                elif kind=='xanim' and key in clip_ids:
                    clip_names[key]=result[1]
    hints = (animpkg.clip_hints((root/'animations').resolve(),clip_ids,check) if clips is None else
             animpkg.read_clip_hints(((key,path) for key,path in clips if key in clip_ids),check))
    matcher = animpkg.Matcher(packages, slots, views, hints, {'animpkg':pkg_table}, use_words=ctx.game=='MODWAR7')
    active_targets=targets-clip_names.keys()-rows['xanim'].keys()-set(excluded)
    for p in packages:
        check()
        if p.key is not None and p.key not in pkg_names and (clips is None or active_targets.intersection(p.references.values())):
            name = animpkg.find_key('animpkg', p.key, matcher.package_candidates(p), check)
            if name:
                pkg_names[p.key] = name
                if clips is None and verified(ctx, 'animpkg', name, p.key):
                    rows['animpkg'][p.key] = name
    pending = defaultdict(list)
    for p in packages:
        label = p.path.stem if p.key is None else pkg_names.get(p.key)
        for slot, key in p.references.items():
            if key in targets and key not in excluded and key not in clip_names and key not in rows['xanim']:
                pending[key].append((p, label, slot))
    for key, name in matcher.resolve_clips(pending,clip_names|rows['xanim'],check,log,scope=keys(ctx,'xanim')).items():
        if result := verified(ctx, 'xanim', name, key):
            rows['xanim'][key] = result[1]
    log(f'动画证据：{len(packages):,} 个包，{len(clip_ids):,} 个在快照内的引用 ID。')


def mw7_export_root(ctx):
    if ctx.source.evidence.strip():
        folder=Path(ctx.source.evidence).resolve()
        if (folder/'animpkgs').is_dir():
            return folder
    if ctx.saluki_dir:
        folder=ctx.saluki_dir.parent/'exported_files'/'mw7'
        if (folder/'animpkgs').is_dir():
            return folder.resolve()
    return None


def sound_parts(name):
    tail = SOUND_TAIL.search(name)
    if tail is None:
        return None
    head = name[:tail.start()]
    sep = '/' if '/' in head else '.'
    base, _, stem = head.rpartition(sep)
    return base, stem, tail[0]


def spellings(folder, stem, tail):
    yield (folder + '/' if folder else '') + stem + tail
    if folder:
        yield folder.replace('/', '.') + '.' + stem + tail
        yield folder.replace('.', '/') + '/' + stem + tail


def candidates(alias, families, profiles):
    family = NUMBERED.sub('', alias)
    aliases = tuple(dict.fromkeys((alias, family, alias.replace('wfoly_rex_','wfoly_',1))))
    for folder, stem, tail in families:
        match = NUMBERED.search(stem)
        if match:
            for n in range(1, 17):
                yield from spellings(folder, stem[:match.start(1)]+str(n).zfill(len(match[1])),tail)
        for name in aliases:
            yield from spellings(folder,name,tail)
    for folder, tail in profiles:
        for name in aliases:
            yield from spellings(folder,name,tail)
        # Capture the conventional zero-based and one-based variants without
        # guessing a directory or codec absent from the neighboring rows.
        for n in range(0, 5):
            yield from spellings(folder,f'{alias}_{n:02d}',tail)


def sound_rows(ctx, bank_dir, rows, check, log):
    seen_alias = set()
    seen_file = {}
    known = baseline(ctx,'sound_asset') if 'sound_asset' in ctx.pools else {}
    missing = set()
    files = within(bank_dir, '*.json')
    for idx, file in enumerate(files,1):
        check()
        data = json.loads(file.read_bytes())
        if not isinstance(data,list):
            raise ValueError(f'声音银行应为 JSON 列表：{file.name}')
        profiles = Counter()
        pending = set()
        family = defaultdict(set)
        for item in data:
            if not isinstance(item,dict):
                continue
            alias = normalize(item['alias']) if isinstance(item.get('alias'),str) else ''
            for name in (alias,item.get('alias2')):
                if (not ctx.raw or not isinstance(name,str) or not name or
                        ALIAS_ID.fullmatch(name) or name in seen_alias):
                    continue
                seen_alias.add(name)
                if answer := verified(ctx,'sound_alias',name):
                    rows['sound_alias'][answer[0]] = answer[1]
            if 'sound_asset' not in ctx.pools or not isinstance(item.get('snd'),str):
                continue
            value = normalize(item['snd'])
            if not value:
                continue
            if m := SOUND_ID.fullmatch(value):
                key = int(m[1],16) & core.MASK
                if key not in keys(ctx,'sound_asset') or key in rows['sound_asset']:
                    continue
                old = known.get(key,'')
                if old and not verified(ctx,'sound_asset',old,key):
                    old = old.replace('/','.')
                if old and verified(ctx,'sound_asset',old,key):
                    value = old
                else:
                    missing.add(key)
                    if alias and not ALIAS_ID.fullmatch(alias):
                        pending.add((key,alias))
                    continue
            bits = sound_parts(value)
            if not bits:
                continue
            if value not in seen_file:
                matched = None
                for option in dict.fromkeys((value, *spellings(*bits))):
                    if matched := verified(ctx,'sound_asset',option):
                        rows['sound_asset'][matched[0]] = matched[1]
                        break
                seen_file[value] = matched
            if seen_file[value]:
                profiles[bits[0],bits[2]] += 1
                if alias:
                    family[NUMBERED.sub('',alias)].add(bits)
        best = [p for p,_ in profiles.most_common(5)]
        for key, alias in sorted(pending):
            check()
            if key in rows['sound_asset']:
                continue
            nearby = sorted(family.get(NUMBERED.sub('',alias),()))[:8]
            for name in dict.fromkeys(candidates(alias,nearby,best)):
                if matched := verified(ctx,'sound_asset',name,key):
                    rows['sound_asset'][key] = matched[1]
                    break
        if idx % 250 == 0:
            log(f'声音证据：已读 {idx:,} / {len(files):,} 个银行。')
    log(f'声音证据：{len(files):,} 个银行，{len(missing):,} 个快照内的待解文件引用。')


def read(ctx, folder, check=lambda:None, log=lambda _:None):
    root = Path(folder).resolve()
    if not root.is_dir():
        raise ValueError('名称线索必须选择目录。')
    if not set(KINDS) & set(ctx.pools):
        raise ValueError('名称线索需要勾选动画、声音文件、声音别名或动画包。')
    rows = defaultdict(dict)
    inputs = 0
    for path in sorted(root.glob('*names.csv')):
        if not path.is_file() or not path.resolve().is_relative_to(root):
            continue
        inputs += 1
        with path.open(encoding='utf-8-sig',newline='') as stream:
            reader = csv.DictReader(stream)
            for line in reader:
                check()
                kind, key, name = (line.get(k) or '' for k in ('type','hash','name'))
                if kind in KINDS and kind in ctx.pools and core.HEX.fullmatch(key):
                    if match := verified(ctx,kind,name,int(key,16)):
                        rows[kind][match[0]] = match[1]
    if set(ctx.pools) & {'xanim','animpkg'} and (root/'animpkgs').is_dir():
        inputs += 1
        animation_rows(ctx,root,rows,check,log)
    if set(ctx.pools) & {'sound_asset','sound_alias'} and (root/'sndbanks/json').is_dir():
        inputs += 1
        sound_rows(ctx,(root/'sndbanks/json').resolve(),rows,check,log)
    if (root/'strings.cipher.txt').is_file():
        inputs += 1
        kinds = [kind for kind in KINDS if ctx.pools.get(kind)]
        before = sum(map(len,rows.values()))
        for name in zone.candidates(root,check):
            for kind in kinds:
                if match := verified(ctx,kind,name):
                    rows[kind].setdefault(*match)
        log(f'Zone 字符串：新增 {sum(map(len,rows.values()))-before:,} 个快照内的名称命中。')
    if not inputs:
        raise ValueError('目录里没有 animpkgs、sndbanks/json、名称 CSV 或 ZoneKit 解密表。')
    return {k:v for k,v in rows.items() if v}


def coverage(ctx, claimed, found, out, latest=None):
    """Inventory the entire selected animation/audio capture, including misses."""
    records = {}
    with (out/'unresolved_names.csv').open('x',encoding='utf-8',newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(('type','hash','hash_bits'))
        for kind in KINDS:
            if kind not in ctx.pools:
                continue
            targets = keys(ctx,kind)
            known = targets & baseline(ctx,kind).keys()
            if latest:
                p = latest/(core.TABLE[kind]+'.cdb')
                if p.is_file():
                    known.update(targets & core.read_cdb(p).keys())
            community = {key for k,key in claimed if k == kind and key in targets} - known
            new = (targets & found.get(kind,{}).keys()) - known - community
            left = targets - known - community - new
            alias = kind == 'sound_alias'
            no_raw = ctx.pools[kind] - {key & core.MASK for key in targets} if alias else set()
            records[kind] = dict(captured=len(targets)+len(no_raw), installed_or_published=len(known),
                                 community=len(community), recovered=len(new), unresolved=len(left)+len(no_raw))
            if alias:
                records[kind]['capture_ids63'] = len(ctx.pools[kind])
                records[kind]['without_raw64'] = len(no_raw)
            writer.writerows((kind,f'{key:016x}',64 if alias else 63) for key in sorted(left))
            writer.writerows((kind,f'{key:016x}',63) for key in sorted(no_raw))
    (out/'name_coverage.json').write_text(json.dumps(records,indent=2)+'\n',encoding='utf-8')
    return records
