from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import threading

import lz4.block
import studio_cache as cache


ASSETS = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'assets'
VERSION = '1.14.5'
MASK = (1 << 63) - 1
UINT64 = (1 << 64) - 1
IW_BASIS = 0x47F5817A5EF961BA
ALIAS_BASIS = 0xCBF29CE484222325
GAMES = {'MODWAR22', 'YAMYAMOK', 'BLACKOP6', 'BLACKOP7', 'MODWAR7'}
TABLE = dict(xanim='fnv1a_xanims_v2', image='fnv1a_ximages_v2', material='fnv1a_xmaterials_v2',
             sound_asset='fnv1a_xsounds_v2', sound_alias='fnv1a_soundbanks_aliases_v2',
             animpkg='fnv1a_animpkgs_v2')
DEFAULT_KINDS = ('xanim', 'image', 'material', 'sound_asset', 'sound_alias')
SEARCH_PHASES = (('xmodel',), ('animpkg', 'xanim'), ('sound_asset', 'sound_alias'), ('material',), ('image',))
PRIORITY = {kind: index for index, phase in enumerate(SEARCH_PHASES) for kind in phase}
SUPPORTED_KINDS = (*TABLE, 'xmodel')
LABEL = dict(xanim='动画', image='贴图', material='材质', sound_asset='声音文件', sound_alias='声音别名',
             animpkg='动画包', xmodel='模型')
EXTRA_TABLES = {'fnv1a_soundbanks_v2'}
HEX = re.compile(r'[0-9a-fA-F]{1,16}')
ASCII_FOLD = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ\\', 'abcdefghijklmnopqrstuvwxyz/')
ROW = struct.Struct('<QH')


def name_key(name, kind):
    h = ALIAS_BASIS if kind == 'sound_alias' else IW_BASIS
    for b in name.translate(ASCII_FOLD).encode('utf-8'):
        h = ((h ^ b) * 0x100000001B3) & UINT64
    return h if kind == 'sound_alias' else h & MASK


def canonical(kind):
    return 'sound_asset' if kind == 'sndasset' else kind


def snapshot_data(data, census, kinds=TABLE):
    if len(data) < 18 or data[:6] != b'CODIDS':
        raise ValueError('快照不是 CODIDS 格式。请选择 .ids 文件。')
    version, n = struct.unpack_from('<HH', data, 6)
    if version != 1 or not 6 <= n <= 10 or len(data) < 18 + n:
        raise ValueError('快照头不完整或版本不支持。')
    tag = data[10:10+n].decode('ascii')
    if tag not in GAMES:
        raise ValueError(f'本版支持现代快照：MWII、MWIII、BO6、BO7、MW7。目前文件为 {tag}。')
    length, = struct.unpack_from('<Q', data, 10+n)
    if len(data) != 18+n+length*ROW.size:
        raise ValueError('快照长度与头部记录数不符。')
    indexes = {}
    for line in census.splitlines():
        m = re.fullmatch(r'\s*(\d+)[,\s]+(\S+?)[,\s]+(\d+)\s*', line)
        if m:
            pool, kind, count = int(m[1]), canonical(m[2]), int(m[3])
            if pool in indexes or kind in {k for k, _ in indexes.values()}:
                raise ValueError('池清单含重复类型或索引。')
            indexes[pool] = kind, count
    pools = {k: set() for k in kinds}
    counts = Counter()
    previous = (-1, -1)
    for key, pool in ROW.iter_unpack(memoryview(data)[18+n:]):
        if pool not in indexes or (key, pool) <= previous or key > MASK:
            raise ValueError('快照有未知池、未排序的记录或超过 63 位的索引。')
        previous = key, pool
        counts[pool] += 1
        kind = indexes[pool][0]
        if kind in pools:
            pools[kind].add(key)
    if counts != {i: n for i, (_, n) in indexes.items() if n}:
        raise ValueError('快照与 .pools.txt 的条目数不一致。')
    return tag, pools, length


def find_snapshot(value):
    p = Path(value).resolve()
    if p.is_dir():
        choices = sorted(x for x in p.glob('*.ids') if x.with_suffix('.pools.txt').is_file())
        if len(choices) != 1:
            raise ValueError('快照目录需恰有一组同名 .ids / .pools.txt；有多组时直接选 .ids 文件。')
        p = choices[0]
    if not p.is_file() or p.suffix.lower() != '.ids' or not p.with_suffix('.pools.txt').is_file():
        raise ValueError('需要同目录、同名的 .ids 和 .pools.txt。')
    return p


def alias_raw(path):
    if not path:
        return set()
    p = Path(path)
    op = gzip.open if p.suffix.lower() == '.gz' else open
    out = set()
    with op(p, 'rt', encoding='utf-8-sig') as f:
        for line in f:
            key = line.strip()
            if not key:
                continue
            if not HEX.fullmatch(key):
                raise ValueError('原始别名证据应为每行一个完整十六进制键。')
            out.add(int(key, 16))
    return out


def names_csv(path):
    yield from cache.file_value(path,'csv',lambda:tuple(_names_csv(path)))


def _names_csv(path):
    with path.open(encoding='utf-8-sig', newline='', errors='strict') as f:
        for row in csv.reader(f):
            if len(row) >= 2 and HEX.fullmatch(row[0].strip()) and row[1]:
                yield int(row[0].strip(), 16), row[1]


def parse_finding_rows(text):
    for line in text.splitlines():
        key, sep, name = line.partition(',')
        if sep and HEX.fullmatch(key.strip()) and name.strip():
            yield int(key.strip(), 16), name.strip()


def read_cdb(path):
    return cache.file_value(path,'cdb',lambda:_read_cdb(path)).copy()


def _read_cdb(path):
    data = path.read_bytes()
    if len(data) < 16:
        raise ValueError(f'基表无效：{path.name}')
    magic, count, packed, size = struct.unpack_from('<4sIII', data)
    if magic != b'PNDB' or count > 10_000_000 or size > 768*1024*1024:
        raise ValueError(f'无法读取 Saluki 基表：{path.name}')
    if not count:
        return {}
    if packed != len(data) - 16 or size < 9*count:
        raise ValueError(f'基表长度异常：{path.name}')
    values = lz4.block.decompress(data[16:], uncompressed_size=size)
    if len(values) != size:
        raise ValueError(f'基表数据长度异常：{path.name}')
    boundary = size - 8*count
    names = values[:boundary].split(b'\0')
    if len(names) != count + 1 or names[-1] != b'':
        raise ValueError(f'基表名称表不完整：{path.name}')
    return {key: name.decode('utf-8') for (key,), name in zip(struct.iter_unpack('<Q', memoryview(values)[boundary:]), names)}


def write_cdb(path, entries):
    items = sorted(entries.items(), key=lambda kv: (kv[1], kv[0]))
    names = bytearray()
    keys = bytearray()
    for key, name in items:
        if not name or '\0' in name:
            raise ValueError('CDB 名称为空，或含空字节。')
        names.extend(name.encode('utf-8') + b'\0')
        keys.extend(struct.pack('<Q', key))
    names.extend(keys)
    packed = lz4.block.compress(names, store_size=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as f:
        f.write(struct.pack('<4sIII', b'PNDB', len(entries), len(packed), len(names)))
        f.write(packed)


@dataclass(frozen=True)
class Inputs:
    snapshot: str
    tables: str
    saluki: str
    output: str
    candidates: str = ''
    alias_keys: str = ''
    kinds: tuple = DEFAULT_KINDS
    millions: int = 10
    keyword: str = ''
    continuous: bool = True
    engine: str = 'auto'
    evidence: str = ''
    adaptive: bool = False


@dataclass
class Context:
    source: Inputs
    path: Path
    table_dir: Path
    saluki_dir: Path | None
    out: Path
    game: str
    pools: dict
    raw: set
    count: int


def inspect(source):
    if not source.snapshot.strip() or not source.tables.strip() or not source.output.strip():
        raise ValueError('请选择快照、名称表和结果目录。')
    path = find_snapshot(source.snapshot)
    table_dir = Path(source.tables).resolve()
    if (table_dir/'csv').is_dir():
        table_dir = table_dir/'csv'
    if not all((table_dir/(TABLE[k]+'.csv')).is_file() for k in DEFAULT_KINDS):
        raise ValueError('名称表目录应为 cod-name-db 或里面的 csv 文件夹，必须包含五张现代名称表。')
    if 'animpkg' in source.kinds and not (table_dir/(TABLE['animpkg']+'.csv')).is_file():
        raise ValueError('搜索动画包需要名称表 fnv1a_animpkgs_v2.csv。')
    saluki_dir = Path(source.saluki).resolve() if source.saluki.strip() else None
    if saluki_dir and (saluki_dir/'hash_pkg').is_dir():
        saluki_dir = saluki_dir/'hash_pkg'
    if saluki_dir and not any((saluki_dir/(stem+'.cdb')).is_file() for stem in TABLE.values()):
        raise ValueError('Saluki 目录没有相应的现代 CDB。请选择 Saluki 安装目录或 hash_pkg。')
    out = Path(source.output).resolve()
    if out in (table_dir, path.parent, saluki_dir) or (saluki_dir and out.is_relative_to(saluki_dir)):
        raise ValueError('请选择独立的结果目录。')
    if not source.kinds or any(k not in SUPPORTED_KINDS for k in source.kinds):
        raise ValueError('请至少选择一类资产。')
    game, pools, count = snapshot_data(path.read_bytes(), path.with_suffix('.pools.txt').read_text(encoding='utf-8-sig'), source.kinds)
    if 'xmodel' in pools and game != 'MODWAR7':
        raise ValueError('模型本地验证目前仅支持 MW7 快照。')
    for optional in ('animpkg','xmodel'):
        if optional in pools and not pools[optional]:
            raise ValueError(f'快照没有 {optional} 池的有效 ID。需要包含该池的 .ids 和 .pools.txt。')
    raw_path = source.alias_keys.strip()
    if not raw_path:
        raw_path = next((str(p) for p in (path.parent/'sound_alias.raw64.txt.gz', path.parent/'sound_alias.raw64.txt') if p.is_file()), '')
    raw = alias_raw(raw_path) if 'sound_alias' in pools and pools['sound_alias'] else set()
    if 'sound_alias' in pools and (not raw or not {k & MASK for k in raw} <= pools['sound_alias']):
        raise ValueError('搜索声音别名需要与快照配套的原始 64 位键清单。没有清单时可取消勾选声音别名。')
    if not any(pools.values()):
        raise ValueError('所选类别在快照中没有可搜索的 ID。')
    return Context(source, path, table_dir, saluki_dir, out, game, pools, raw, count)


def matches(ctx, kind, key, name):
    return (key == name_key(name, kind) and key & MASK in ctx.pools.get(kind, ())
            and (kind != 'sound_alias' or key in ctx.raw))


class Job:
    def __init__(self):
        self.cancelled = threading.Event()
        self.child = None
        self.lock = threading.Lock()

    def stop(self):
        self.cancelled.set()
        with self.lock:
            if self.child is not None and self.child.poll() is None:
                self.child.terminate()

    def check(self):
        if self.cancelled.is_set():
            raise InterruptedError('已停止。')

    def call(self, args, cwd, log):
        self.check()
        with self.lock:
            self.check()
            proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.child = proc
        try:
            for line in proc.stdout:
                log(line.rstrip())
            status = proc.wait()
            self.check()
            if status:
                raise RuntimeError(f'Slasher 返回 {status}。请查看运行日志。')
        finally:
            proc.stdout.close()
            with self.lock:
                self.child = None


def seed_rows(ctx, kind, job):
    if kind != 'xmodel':
        yield from names_csv(ctx.table_dir/(TABLE[kind]+'.csv'))
        return
    # No dedicated MW7 model table. Shared names must also match the actual
    # model pool before they can supply vocabulary. Legacy keys are never reused.
    yield from names_csv(ctx.table_dir/(TABLE['material']+'.csv'))
    legacy=ctx.table_dir/'fnv1a_xmodels.csv'
    if legacy.is_file():
        for index, (_, name) in enumerate(names_csv(legacy)):
            if index%1024==0:job.check()
            key=name_key(name,kind)
            if key in ctx.pools[kind]:
                yield key,name


def plan_words(ctx, kinds, job, log):
    base = Counter()
    ends = Counter()
    stems = set()
    keyword = ctx.source.keyword.strip().lower()
    for kind in kinds:
        n = 0
        for i, (key, name) in enumerate(seed_rows(ctx,kind,job)):
            if i % 1024 == 0:
                job.check()
            if key & MASK not in ctx.pools[kind] or len(name) > 220 or '\n' in name:
                continue
            raw = name.translate(ASCII_FOLD)
            if key != name_key(raw, kind) or keyword and keyword not in raw:
                continue
            words = raw.rstrip('/').split('_')
            if len(words) < 2:
                continue
            n += 1
            base[words[0]+'_'] += 1
            for k in range(1, min(3, len(words))):
                ends['_'+ '_'.join(words[-k:])] += 1
                part = '_'.join(words[1:-k])
                if part and len(part) >= 4 and part[0] not in '#@':
                    stems.add(part)
            if n >= 32_000:
                break
        log(f'{LABEL[kind]}：从本作快照验证了 {n:,} 条种子。')
    if not stems or not base or not ends:
        if keyword:
            log('关键词没有匹配到这个搜索组的已知名称，跳过该组。')
            return None
        raise ValueError('没有可用于组合的已验证种子。请核对快照与名称表，或指定候选名单。')
    openings = [s for s, _ in base.most_common(48)]
    endings = [s for s, _ in ends.most_common(128)]
    return openings, endings, sorted(stems, key=lambda s: (-len(s.split('_')), s))


def write_plan(path, openings, endings, stems):
    path.write_text('label: Studio bounded family search\nbare: yes\nfold: yes\n' + '\n'.join(
        f'{section}: {item}' for section, xs in [('begin', openings), ('stem', stems), ('end', endings)] for item in xs) + '\n', encoding='utf-8')


def results_export(ctx, rows, out, log, latest_cdb=None, previous=None):
    from studio_pipeline import export_names, convert
    export = out/'saluki-ready'/'hash_pkg'
    accepted = defaultdict(dict)
    counter = Counter()
    cache={}
    old_cache={}
    def file_hash(path):
        with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
    if previous and (previous/'export-cache.json').is_file():
        try:old_cache=json.loads((previous/'export-cache.json').read_text(encoding='utf-8'))
        except (ValueError,OSError):pass
    for kind, values in sorted(rows.items()):
        if not values:
            continue
        if kind=='xmodel':
            for key,name in values.items():
                if matches(ctx,kind,key,name):
                    accepted[kind][key]=name
            if accepted[kind]:
                log(f'模型：确认 {len(accepted[kind]):,} 条本地匹配，已写入 CSV。')
            continue
        stem = TABLE[kind]
        base_csv = ctx.table_dir/(stem+'.csv')
        current = latest_cdb/(stem+'.cdb') if latest_cdb else None
        local_base = ctx.saluki_dir/(stem+'.cdb') if ctx.saluki_dir else None
        inputs=[]
        for p in (base_csv,current,local_base,ctx.path):
            if p and p.is_file():
                stat=p.stat();inputs.append((str(p.resolve()),stat.st_size,stat.st_mtime_ns))
        signature=hashlib.sha256(json.dumps([inputs,sorted(values.items())],ensure_ascii=False).encode()).hexdigest()
        cache[kind]={'signature':signature,'files':{}}
        paths=[Path('txt')/(kind+'.txt'),Path('merged-csv')/(stem+'.csv'),Path('saluki-ready/hash_pkg')/(stem+'.cdb')]
        entry=old_cache.get(kind,{})
        reusable=(isinstance(entry,dict) and entry.get('signature')==signature and
                  all((previous/p).is_file() and file_hash(previous/p)==entry.get('files',{}).get(p.as_posix()) for p in paths))
        if reusable:
            for p in paths:
                (out/p).parent.mkdir(parents=True,exist_ok=True);shutil.copy2(previous/p,out/p)
            accepted[kind].update(parse_finding_rows((out/paths[0]).read_text(encoding='utf-8')))
            manifest=json.loads((previous/'pipeline.json').read_text(encoding='utf-8'))
            file=out/'pipeline.json'
            merged=json.loads(file.read_text(encoding='utf-8')) if file.exists() else {}
            merged[kind]=manifest[kind]
            file.write_text(json.dumps(merged,ensure_ascii=False,indent=2),encoding='utf-8')
            log(f'{LABEL[kind]}：名称和基库未改变，沿用已验证的 TXT / CDB。')
            cache[kind]=entry
            continue
        baseline = dict(names_csv(base_csv))
        if current and current.is_file():
            baseline.update(read_cdb(current))
        if local_base and local_base.is_file():
            baseline.update(read_cdb(local_base))
        for key, name in values.items():
            if not matches(ctx, kind, key, name):
                counter['unverified'] += 1
            elif key in baseline:
                counter['known'] += 1
            else:
                accepted[kind][key] = name
        if accepted[kind]:
            baseline.update(accepted[kind])
            cdb = export/(stem+'.cdb')
            txt = export_names(out, kind, accepted[kind])
            convert(txt, out/'merged-csv'/(stem+'.csv'), cdb, baseline, log)
            cache[kind]['files']={p.as_posix():file_hash(out/p) for p in paths}
            log(f'{LABEL[kind]}：导出 {len(accepted[kind]):,} 条新名称；合并库共 {len(baseline):,} 条。')
    with (out/'new_names.csv').open('x', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(('type','hash','name'))
        for kind, values in sorted(accepted.items()):
            w.writerows((kind, f'{key:016x}', name) for key, name in sorted(values.items()))
    (out/'export-cache.json').write_text(json.dumps(cache,sort_keys=True),encoding='utf-8')
    return {k:v for k,v in accepted.items() if v}

