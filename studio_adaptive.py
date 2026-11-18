"""Durable, outcome-driven selection of typed name-recovery methods."""
from collections import Counter, defaultdict, OrderedDict
from contextlib import nullcontext
from dataclasses import replace
import studio_queue as queue
import csv
import gzip
import hashlib
import json
from pathlib import Path
import time

import studio_cache as file_cache
import studio_core as core
import studio_evidence as evidence
import studio_methods as methods
from studio_sources import atomic_bytes, capture_identity


SEEDS_PER_SHARD = 256
CANDIDATES_PER_BATCH = 100_000
SCHEMA = '''
CREATE TABLE IF NOT EXISTS adaptive_run(
 id INTEGER PRIMARY KEY, method TEXT, kind TEXT, label TEXT, fingerprint TEXT UNIQUE,
 snapshot TEXT, seed_pos INTEGER DEFAULT 0, seeds INTEGER,
 attempted INTEGER DEFAULT 0, discovered INTEGER DEFAULT 0, seconds REAL DEFAULT 0,
 state TEXT DEFAULT 'ready');
CREATE TABLE IF NOT EXISTS adaptive_queue(
 id INTEGER PRIMARY KEY, run INTEGER, name TEXT);
CREATE INDEX IF NOT EXISTS adaptive_queue_run ON adaptive_queue(run,id);
CREATE TABLE IF NOT EXISTS adaptive_seen(kind TEXT,digest BLOB,PRIMARY KEY(kind,digest)) WITHOUT ROWID;
'''


def encode(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),sort_keys=True).encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def setup(db):
    # A larger on-demand page cache and read mapping help random probes on
    # large history indexes. These settings are local to this connection.
    db.execute('PRAGMA cache_size=-262144')
    db.execute('PRAGMA mmap_size=1073741824')
    if db.execute('PRAGMA temp_store').fetchone()[0]!=2:db.execute('PRAGMA temp_store=MEMORY')
    db.executescript(SCHEMA)
    if "seed_data" not in {row[1] for row in db.execute("pragma table_info(adaptive_run)")}:
        db.execute("ALTER TABLE adaptive_run ADD COLUMN seed_data TEXT DEFAULT ''")
        db.commit()


def _base_corpus(ctx, latest, check, log):
    corpus={k:set() for k in methods.KINDS}
    training={k:set() for k in methods.KINDS}
    held={k:set() for k in methods.KINDS}
    def add(kind,key,name,independent=False):
        name=name.strip().translate(core.ASCII_FOLD)
        if not evidence.SAFE.fullmatch(name):
            return
        actual=core.name_key(name,kind)
        if key!=actual:
            if kind!='sound_asset':
                return
            parts=evidence.sound_parts(name)
            if not parts:
                return
            for option in evidence.spellings(*parts):
                if core.name_key(option,kind)==key:
                    name=option
                    break
            else:return
        corpus[kind].add(name)
        if independent:training[kind].add(name)
        if key&core.MASK in ctx.pools.get(kind,()) and (kind!='sound_alias' or key in ctx.raw):
            held[kind].add(name)
    for kind in methods.KINDS:
        # Sound-file and alias methods share vocabulary even if only one target
        # type is selected. Each source spelling must reproduce its own key.
        file=ctx.table_dir/(core.TABLE[kind]+'.csv')
        if file.is_file():
            for i,(key,name) in enumerate(core.names_csv(file)):
                if i%8192==0:check()
                add(kind,key,name,True)
        for folder in dict.fromkeys(p for p in (latest,ctx.saluki_dir) if p):
            file=folder/(core.TABLE[kind]+'.cdb')
            if file.is_file():
                for i,(key,name) in enumerate(core.read_cdb(file).items()):
                    if i%8192==0:check()
                    add(kind,key,name)
        log(f'自动方法种子：{core.LABEL[kind]} {len(corpus[kind]):,} 个可回算原名。')
    return corpus,training,held


_corpus_memory=None
_prepared=OrderedDict()


def seed_corpus(ctx, db, latest, check, log, cache_dir=None):
    global _corpus_memory
    sources=[]
    for kind in methods.KINDS:
        files=[ctx.table_dir/(core.TABLE[kind]+'.csv')]
        files.extend(p/(core.TABLE[kind]+'.cdb') for p in (latest,ctx.saluki_dir) if p)
        sources.extend(file_cache.stamp(p) for p in files if p.is_file())
    key=digest(['verified-corpus-1',capture_identity(ctx),sorted(ctx.pools),sources])
    if _corpus_memory is not None and _corpus_memory[0]==key:
        base=_corpus_memory[1]
        log('复用已验证名称语料（内存缓存）。')
    else:
        path=cache_dir/'corpus.json.gz' if cache_dir else None
        packed=file_cache.read_json(path,key) if path else None
        if packed is not None:
            base=tuple({k:set(v) for k,v in values.items()} for values in packed)
            log('复用已验证名称语料（磁盘缓存）。')
        else:
            base=_base_corpus(ctx,latest,check,log)
            if path:file_cache.write_json(path,key,[{k:sorted(v) for k,v in values.items()} for values in base])
        _corpus_memory=(key,base)
    corpus,training,held=({k:set(v) for k,v in values.items()} for values in base)
    for i,(kind,key,name) in enumerate(db.execute('select kind,key,name from hit')):
        if i%8192==0:check()
        if kind in corpus and core.matches(ctx,kind,int(key,16),name):
            name=name.strip().translate(core.ASCII_FOLD)
            if evidence.SAFE.fullmatch(name):corpus[kind].add(name);held[kind].add(name)
    return corpus,training,held


def counts(db):
    try:
        done=db.execute('select coalesce(sum(attempted),0) from adaptive_run').fetchone()[0]
        pending=db.execute('select count(*) from adaptive_queue').fetchone()[0]
        return done,done+pending
    except Exception as exc:
        import sqlite3
        if isinstance(exc,sqlite3.OperationalError):return 0,0
        raise


def report(db, folder):
    (folder/'method_results.json').write_text(json.dumps(report_rows(db),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def report_rows(db):
    columns=('id','method','kind','label','fingerprint','seed_pos','seeds','attempted','discovered','seconds','state')
    data=[dict(zip(columns,row)) for row in db.execute('select '+','.join(columns)+' from adaptive_run order by id')]
    return data


def run(root, db, ctx, latest, claims, pause, log, execute, budget, progress=lambda:None, *, allowed_kinds=None, metrics=None):
    measure=metrics.measure if metrics is not None else lambda _:nullcontext()
    setup(db)
    directory=root/'methods'
    directory.mkdir(exist_ok=True)
    cache={}
    seen_inputs=set()
    used=0
    seedset=None
    refresh=True
    producer=queue.Producer(log,measure)
    sizing=queue.BatchSize()
    plan_index={}
    allowed=set(ctx.pools) if allowed_kinds is None else set(allowed_kinds)
    # Register new methods alongside pending legacy work, without changing old
    # fingerprints, updating their seed positions, or deleting queued rows.
    registered=set(db.execute('select distinct kind,method from adaptive_run'))
    new_methods={(kind,key) for kind in allowed & ctx.pools.keys()
                 for key in methods.specialized.METHODS.get(kind,())} - registered
    def pending_runs():
        return [row for row in db.execute("select id,method,kind,label,snapshot,seed_pos,seeds,seed_data from adaptive_run where state!='done'") if row[2] in allowed]
    def prepared(snapshot):
        if snapshot not in cache:
            path=directory/(snapshot+'.json.gz')
            key=(file_cache.stamp(path),ctx.game,methods.VERSION,id(methods.prepare))
            if key in _prepared:
                cache[snapshot]=_prepared.pop(key)
                _prepared[key]=cache[snapshot]
                log('复用同一方法快照的已测量规则。')
            else:
                with measure('method_preparation'):
                    with gzip.open(path,'rt',encoding='utf-8') as f:pack=json.load(f)
                    maps=[{k:set(v) for k,v in pack[name].items()} for name in ('corpus','training','held','wanted')]
                    log('正在测量动画词位与声音族规则…')
                    cache[snapshot]=(maps[0],methods.prepare(*maps[:3],ctx.game,maps[3],pause.check))
                _prepared[key]=cache[snapshot]
                while len(_prepared)>2:_prepared.popitem(last=False)
        return cache[snapshot]
    def plan_for(row):
        rid,key,kind,label,snapshot,pos,length,seed_data=row
        if rid not in plan_index:
            known,plans=prepared(snapshot)
            plan=next(p for p in plans if p.key==key and p.kind==kind)
            if seed_data:plan=replace(plan,seeds=json.loads(seed_data))
            plan_index[rid]=(known,plan)
        return plan_index[rid]

    def feedback(snapshot,hits):
        # Apply existing measured rules immediately to newly verified names.
        # Full rule retraining still follows when the pending queue is exhausted.
        _,plans=prepared(snapshot)
        with db:
            for plan in plans:
                seeds=set()
                for kind,_,name in hits:
                    if kind==plan.kind:
                        if plan.key=='slot-edits' and methods.split(kind,name):seeds.add(name)
                        elif plan.key.startswith('animation-'):seeds.add(name)
                        elif plan.key=='final-byte' and kind!='sound_asset':seeds.add(name[:-1])
                        elif plan.key in ('visual-variants','image-joint-wrappers'):seeds.add(name)
                        elif plan.key=='sound-basename-byte':
                            parts=methods.sound(name)
                            if parts and len(parts[1])>1:seeds.add((name[:-len(parts[2])-1],parts[2]))
                        elif plan.key in ('sound-tails','sound-grammar'):
                            parts=methods.sound(name)
                            if parts:seeds.add(parts[:2])
                    if kind=='sound_alias' and plan.key=='alias-files':seeds.add(name)
                    if kind=='sound_asset' and plan.key=='file-aliases':
                        parts=methods.sound(name)
                        if parts:seeds.add(parts[1])
                if not seeds:continue
                payload=encode(sorted(seeds)).decode()
                fingerprint=digest(['feedback',methods.VERSION,snapshot,plan.key,plan.kind,payload])
                db.execute('INSERT OR IGNORE INTO adaptive_run(method,kind,label,fingerprint,snapshot,seeds,seed_data) VALUES (?,?,?,?,?,?,?)',
                           (plan.key,plan.kind,'新命中跟进：'+plan.label,fingerprint,snapshot,len(seeds),payload))

    try:
        while not pause.flag.is_set() and used<budget:
            pending=pending_runs()
            if (not pending and refresh) or new_methods:
                only_new=bool(pending)
                if seedset is None:
                    with measure('method_preparation'):
                        corpus,training,held=seed_corpus(ctx,db,latest,pause.check,log,root/'cache')
                    seedset=(corpus,training,held)
                corpus,training,held=seedset
                for kind,key,name in db.execute('select kind,key,name from hit'):
                    if kind in corpus and core.matches(ctx,kind,int(key,16),name):
                        corpus[kind].add(name);held[kind].add(name)
                wanted={}
                for kind in methods.KINDS:
                    if kind not in ctx.pools or kind not in allowed:continue
                    known={core.name_key(n,kind) for n in held[kind]}
                    known.update(key for k,key in claims if k==kind)
                    targets=ctx.raw if kind=='sound_alias' else ctx.pools[kind]
                    wanted[kind]=targets-known
                pack={name:{k:sorted(v) for k,v in values.items()} for name,values in
                      (('corpus',corpus),('training',training),('held',held),('wanted',wanted))}
                snap=digest(pack)
                if not only_new:
                    if snap in seen_inputs:break
                    seen_inputs.add(snap)
                path=directory/(snap+'.json.gz')
                if not path.exists():atomic_bytes(path,gzip.compress(encode(pack),compresslevel=1))
                cache.clear();plan_index.clear()
                _,plans=prepared(snap)
                with db:
                    for plan in plans:
                        if plan.kind not in allowed:continue
                        if only_new and (plan.kind,plan.key) not in new_methods:continue
                        fingerprint=digest([methods.VERSION,ctx.game,plan.key,plan.kind,
                                            {k:[pack['corpus'].get(k,()),pack['training'].get(k,()),
                                                pack['held'].get(k,())] for k in plan.deps},
                                            pack['wanted'].get(plan.kind,())])
                        db.execute('insert or ignore into adaptive_run(method,kind,label,fingerprint,snapshot,seeds,state) values (?,?,?,?,?,?,?)',
                                   (plan.key,plan.kind,plan.label,fingerprint,snap,len(plan.seeds),
                                    'ready' if plan.seeds else 'done'))
                new_methods.clear()
                if not only_new:refresh=False
                pending=pending_runs()
                if not pending:break
            if not pending:break
            def score(row):
                rid,key,kind,label,snapshot,pos,length,seed_data=row
                _,plan=plan_for(row)
                attempted,hits=db.execute('select attempted,discovered from adaptive_run where id=?',(rid,)).fetchone()
                return (-core.PRIORITY[kind], (plan.priority+min(10,hits*100000/max(1,attempted))+(30 if seed_data else 0))/(1+pos/max(256,length)/2))
            # A prefetched shard may wait in memory while higher-yield feedback
            # runs. Its seed cursor is unchanged until we actually consume it.
            row=max(pending,key=score)
            rid,key,kind,label,snapshot,pos,length,seed_data=row
            known,plan=plan_for(row)
            limit=max(1,int(min(sizing.target,budget-used)))
            queued=db.execute('select id,name from adaptive_queue where run=? order by id limit ?',(rid,limit)).fetchall()
            if not queued and pos<length:
                generated=producer.take((rid,pos))
                if generated is None:
                    generated=producer.generate(plan,known[kind],pos,limit,ctx.source.keyword.lower(),pause.check)
                stop,candidates,seconds=generated
                with measure('sqlite_dedup'):
                    queue.enqueue(db,rid,kind,stop,candidates,seconds)
                pos=stop
                queued=db.execute('select id,name from adaptive_queue where run=? order by id limit ?',(rid,limit)).fetchall()
            # Produce while OpenCL/CPU verification runs; only this thread owns SQLite.
            if queued and pos<length and used+len(queued)<budget and not seed_data and not any(r[7] for r in pending):
                producer.start((rid,pos),plan,known[kind],pos,sizing.target,ctx.source.keyword.lower(),pause.check)
            if queued:
                log(f'自动方法：{label} / {core.LABEL[kind]}，本批 {len(queued):,} 个新候选，种子 {pos:,}/{length:,}。')
                started=time.monotonic()
                hits=execute((kind,),[name for _,name in queued])
                elapsed=time.monotonic()-started
                sizing.observe(len(queued),elapsed)
                fresh_rows=[]
                with db:
                    fresh=0
                    for k,h,name in hits:
                        if k!=kind or not core.matches(ctx,k,int(h,16),name):
                            raise ValueError('自动方法命中未通过 CPU 与资产池复核。')
                        result=db.execute('insert or ignore into hit values (?,?,?)',(k,h,name))
                        fresh+=result.rowcount
                        if result.rowcount:fresh_rows.append((k,h,name))
                    db.execute('delete from adaptive_queue where run=? and id<=?',(rid,queued[-1][0]))
                    db.execute('update adaptive_run set attempted=attempted+?,discovered=discovered+?,seconds=seconds+? where id=?',
                               (len(queued),fresh,time.monotonic()-started,rid))
                used+=len(queued)
                if metrics is not None:metrics.committed('adaptive',len(queued))
                if fresh:
                    refresh=True
                    feedback(snapshot,fresh_rows)
                    log(f'{label} 新增 {fresh:,} 个经复核名称；后续方法将使用这些新线索。')
                progress()
            if pos==length and not db.execute('select 1 from adaptive_queue where run=? limit 1',(rid,)).fetchone():
                with db:db.execute("update adaptive_run set state='done' where id=?",(rid,))
                attempts,hits=db.execute('select attempted,discovered from adaptive_run where id=?',(rid,)).fetchone()
                log(f'方法完成：{label} / {core.LABEL[kind]}，{attempts:,} 个新候选，新增 {hits:,}；当前输入不再重复。')
                # Feedback uses the parent's measured rules without restarting old cursors.
        remaining=len(pending_runs())
        return used, ('已暂停' if pause.flag.is_set() else '本轮预算已用完，仍可继续' if remaining
                      else '当前有依据的方法已完成，等待新的名称或线索')
    except InterruptedError:
        return used,'已暂停'
    finally:
        producer.close()
        report(db,root)
