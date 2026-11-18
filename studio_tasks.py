"""Persistent candidate queue, committed results, and pausable Slasher batches."""

from collections import defaultdict
from contextlib import closing, contextmanager, ExitStack
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid

import studio_community as community
import studio_animpkg as animpkg
import studio_cache as cache
import studio_core as core
import studio_evidence as evidence
import studio_adaptive as adaptive
import studio_backend as backends
import studio_sync as sync
import studio_history as history
from studio_export import BackgroundExport, ExportWorker
from studio_gpu import GpuError, GpuSearch
from studio_metrics import RoundMetrics, timing_summary
from studio_sources import atomic_bytes, capture_identity, cdb_claims, latest_cdb, read_survey


BATCH = 1_000_000
PLAN_BATCH = 4 * BATCH
AUTO_NARROW_PLAN_BATCH = 512 * BATCH
QUEUE = 'task.sqlite3'
AUTOSAVE_SECONDS = 120
KINDS = [(kind,) for phase in core.SEARCH_PHASES for kind in phase]


def connect(root):
    path=(root/QUEUE).resolve()
    if not path.is_file():raise ValueError('这个目录没有 task.sqlite3。')
    return sqlite3.connect(path,timeout=3)


@contextmanager
def exclusive(root):
    with (root/'.search-lock').open('a+b') as lock:
        if not lock.tell():lock.write(b'\0');lock.flush()
        lock.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            raise ValueError('这个任务已在另一个窗口搜索。') from None
        yield


def record(db):
    meta=dict(db.execute('select name,value from meta'))
    if meta.get('schema') not in ('2','3','4'):raise ValueError('任务版本不支持。')
    return meta


def total(db):
    done,maximum=db.execute('select coalesce(sum(pos*width),0),coalesce(sum(count*width),0) from groups').fetchone()
    adone,amaximum=adaptive.counts(db)
    return done+adone,maximum+amaximum


def overview(root):
    with closing(connect(root)) as db:
        meta=record(db)
        done, maximum=total(db)
        return json.loads(meta['inputs']),done,maximum,meta.get('export','')


def publish_live(root, stage):
    """Replace a complete, verified export; keep the prior copy for recovery."""
    live=root/'live'
    previous=root/'live.previous'
    marker='.studio-live'
    for folder in (live,previous):
        if folder.exists() and (folder.is_symlink() or not (folder/marker).is_file()):
            raise ValueError(f'自动导出目录被其他文件占用：{folder}')
    if not live.exists() and previous.exists():
        previous.rename(live)
    if previous.exists():
        shutil.rmtree(previous)
    if live.exists():
        live.rename(previous)
    try:
        stage.rename(live)
    except OSError:
        if previous.exists() and not live.exists():previous.rename(live)
        raise
    return live


class Pause:
    def __init__(self): self.flag=threading.Event()
    def stop(self): self.flag.set()
    def check(self):
        if self.flag.is_set():raise InterruptedError('已取消任务准备，未开始搜索。')


def create(ctx, pause, log):
    root=ctx.out/('task-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:5])
    root.mkdir(parents=True)
    db=sqlite3.connect(root/QUEUE)
    try:
        db.executescript('''
            CREATE TABLE meta(name TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE groups(id INTEGER PRIMARY KEY, kinds TEXT, mode TEXT, width INTEGER, count INTEGER, pos INTEGER DEFAULT 0, words TEXT);
            CREATE TABLE manual(idx INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
            CREATE TABLE hit(kind TEXT NOT NULL,key TEXT NOT NULL,name TEXT NOT NULL,PRIMARY KEY(kind,key));
        ''')
        with db:
            db.executemany('insert into meta values (?,?)',[('schema','4'),('identity',capture_identity(ctx)),('inputs',json.dumps(asdict(ctx.source),ensure_ascii=False))])
            if ctx.source.evidence.strip():
                verified=evidence.read(ctx,ctx.source.evidence,pause.check,log)
                db.executemany('insert or ignore into hit values (?,?,?)',
                               ((kind,f'{key:016x}',name) for kind,names in verified.items() for key,name in names.items()))
                log(f'名称线索：{sum(map(len,verified.values())):,} 条通过快照与 hash 验证，包含已有名称。')
            if ctx.source.candidates.strip():
                path=Path(ctx.source.candidates).resolve()
                paths=sorted(path.glob('*.txt')) if path.is_dir() else [path] if path.is_file() else []
                if not paths:raise ValueError('候选目录里没有 .txt 名单。')
                for f in paths:
                    with f.open(encoding='utf-8-sig') as stream:
                        for line in stream:
                            pause.check()
                            line=line.strip()
                            if not line:continue
                            h, sep, rest=line.partition(',')
                            if sep and core.HEX.fullmatch(h.strip()):line=rest.strip()
                            if line:db.execute('insert or ignore into manual(name) values (?)',(line,))
                n=db.execute('select count(*) from manual').fetchone()[0]
                # Keep model/package verification separate from ordinary pools,
                # even if linked assets happen to share the same name and ID.
                if n:
                    for family in KINDS:
                        kinds=[k for k in family if ctx.pools.get(k)]
                        if kinds:
                            db.execute('insert into groups(kinds, mode, width, count, words) values (?,?,?,?,?)',
                                       (json.dumps(kinds),'manual',1,n,''))
            elif not ctx.source.evidence.strip():
                for family in KINDS:
                    kinds=[k for k in family if ctx.pools.get(k) and
                           (not ctx.source.adaptive or k not in adaptive.methods.KINDS)]
                    if not kinds:continue
                    plan=core.plan_words(ctx,kinds,pause,log)
                    if not plan:continue
                    opens,ends,stems=plan
                    db.execute('insert into groups(kinds, mode, width, count, words) values (?,?,?,?,?)',
                               (json.dumps(kinds),'plan',(len(opens)+1)*(len(ends)+1),len(stems),json.dumps(plan)))
            if not total(db)[1] and not ctx.source.evidence.strip() and not ctx.source.adaptive:
                raise ValueError('没有候选名称可供搜索。请调整关键词或名单。')
        return root
    except Exception:
        db.close()
        (root/QUEUE).unlink(missing_ok=True)
        root.rmdir()
        raise
    finally:
        db.close()


def process_batch(work, ctx, kinds, mode, opens, ends, words, log):
    with tempfile.TemporaryDirectory(dir=work,prefix='batch-') as p:
        scratch=Path(p)
        fields=f'game = "{ctx.game}"\npools = {json.dumps(kinds)}\n'
        if 'xmodel' in kinds:
            fields+='search_modern_models = true\n'
        cfg=f'[search]\nalternate_games = false\n{fields}[paths]\ntables = {json.dumps(ctx.table_dir.as_posix(),ensure_ascii=False)}\nfindings = {json.dumps(scratch.as_posix(),ensure_ascii=False)}\n'
        (work/'config.toml').write_text(cfg,encoding='utf-8')
        file=scratch/'candidates.txt'
        if mode=='manual':
            file.write_text('\n'.join(words)+'\n',encoding='utf-8'); tool='confirm_list.exe'
        else:
            core.write_plan(file,opens,ends,words); tool='confirm_plan.exe'
        core.Job().call([str(core.ASSETS/'engine'/tool),str(file),'--game',ctx.game,'--anyway'],work,log)
        rows=[]
        for k in kinds:
            f=scratch/ctx.game.lower()/(k+'.txt')
            if f.is_file():
                rows.extend((k,f'{key:016x}',name) for key,name in core.parse_finding_rows(f.read_text(encoding='utf-8')) if core.matches(ctx,k,key,name))
        return rows


def upgrade(root, db, meta, ctx, pause, log):
    """Split legacy mixed pools at their saved cursor, and append new types only."""
    old=json.loads(meta['inputs'])
    added=set(ctx.pools)-set(old['kinds'])
    if meta['schema']=='4' and not added:return
    backup=root/('progress-before-1.10.sqlite3' if meta['schema']=='2' else 'progress-before-1.11.sqlite3')
    if not backup.exists():
        with tempfile.TemporaryDirectory(dir=root,prefix='.upgrade-') as folder:
            copy=Path(folder)/'progress.sqlite3'
            with closing(sqlite3.connect(copy)) as saved:db.backup(saved)
            copy.rename(backup)
    with db:
        for gid,raw,mode,width,count,pos,words in db.execute('select id,kinds,mode,width,count,pos,words from groups').fetchall():
            selected=set(json.loads(raw))
            phases=[[k for k in phase if k in selected] for phase in KINDS]
            phases=[p for p in phases if p]
            db.execute('update groups set kinds=? where id=?',(json.dumps(phases[0]),gid))
            for phase in phases[1:]:
                db.execute('insert into groups(kinds,mode,width,count,pos,words) values (?,?,?,?,?,?)',
                           (json.dumps(phase),mode,width,count,pos,words))
        n=db.execute('select count(*) from manual').fetchone()[0]
        for family in KINDS:
            kinds=[k for k in family if k in added]
            if not kinds:continue
            if n:
                db.execute('insert into groups(kinds,mode,width,count,words) values (?,?,?,?,?)',
                           (json.dumps(kinds),'manual',1,n,''))
            elif not old.get('evidence'):
                kinds=[k for k in kinds if not ctx.source.adaptive or k not in adaptive.methods.KINDS]
                if not kinds:continue
                plan=core.plan_words(ctx,kinds,pause,log)
                if plan:
                    opens,ends,stems=plan
                    db.execute('insert into groups(kinds,mode,width,count,words) values (?,?,?,?,?)',
                               (json.dumps(kinds),'plan',(len(opens)+1)*(len(ends)+1),len(stems),json.dumps(plan)))
        old['kinds']=list(ctx.pools)
        db.execute("update meta set value='4' where name='schema'")
        db.execute("update meta set value=? where name='inputs'",(json.dumps(old,ensure_ascii=False),))
    log('任务已升级并备份；原候选断点、已尝试指纹和命中保留，只为新增类型添加队列。')


def resume_animation_evidence(db,ctx,latest,claims,check,log):
    folder=evidence.mw7_export_root(ctx)
    if folder is None:
        return {}
    clips=animpkg.clip_files((folder/'animations').resolve(),ctx.pools['xanim'],check)
    saved={int(key,16):name for key,name in db.execute("select key,name from hit where kind='xanim'")}
    excluded={key for kind,key in claims if kind=='xanim'}
    if all(key in excluded or key in saved for key,_ in clips):
        return {}
    files=[path for _,path in clips]
    files.extend(evidence.within((folder/'animpkgs').resolve(),'*.json'))
    for kind in animpkg.KINDS:
        table=core.TABLE[kind]
        files.append(ctx.table_dir/(table+'.csv'))
        files.extend(root/(table+'.cdb') for root in (ctx.saluki_dir,latest) if root)
    summary=hashlib.sha256(core.VERSION.encode())
    for file in sorted(set(files)):
        check()
        if file.is_file():summary.update(json.dumps(cache.stamp(file)).encode())
    refs={key for key,_ in clips}
    summary.update(json.dumps(sorted(refs & excluded)).encode())

    def revision(names):
        token=summary.copy()
        token.update(json.dumps(sorted(names.items()),ensure_ascii=True).encode())
        return token.hexdigest()

    previous=db.execute("select value from meta where name='animation_evidence'").fetchone()
    if previous and previous[0]==revision(saved):
        log('MW7 动画包与 CAST 线索已验证，沿用任务记录。')
        return {}
    log(f'MW7 自动动画线索：{len(clips):,} 个本地 CAST 文件。')
    rows=defaultdict(dict,{'xanim':dict(saved)})
    evidence.animation_rows(ctx,folder,rows,check,log,latest=latest,clips=clips,excluded=excluded)
    found={key:name for key,name in rows['xanim'].items() if key not in saved and key not in excluded}
    check()
    with db:
        db.executemany("insert or ignore into hit values ('xanim',?,?)",
                       ((f'{key:016x}',name) for key,name in found.items()))
        db.execute("insert or replace into meta values ('animation_evidence',?)",(revision(saved|found),))
    log(f'MW7 自动动画线索：新增 {len(found):,} 条，已保存到当前任务。')
    return found


def resume(root, ctx, sources, pause, logger, progress=lambda *_:None, *, engine=process_batch, override_claims=None):
    root=Path(root).resolve()
    identity=capture_identity(ctx)
    log_lock=threading.Lock()
    def log(message):
        with log_lock, (root/'run.log').open('a',encoding='utf-8') as f: f.write(str(message)+'\n')
        logger(message)
    with exclusive(root), closing(connect(root)) as db, ExitStack() as gpu_resources, ExitStack() as background_resources:
        metrics=RoundMetrics()
        meta=record(db)
        if meta['identity']!=identity or not set(json.loads(meta['inputs'])['kinds']).issubset(ctx.pools):
            raise ValueError('选择的快照、别名证据或资产类型与这个任务不同。可以更换名称库；新快照需另建任务。')
        latest,tag=latest_cdb(sources)
        if tag:log(f'使用已同步的官方 CDB {tag}。')
        if override_claims is None:
            try:
                claims,stamp=sync.sync(ctx,sources,log,pause.check)
                latest,model_claims,info=history.prepare(ctx,sources,root,latest,log,pause.check)
            except InterruptedError:raise
            except Exception as exc:
                raise ValueError(f'运行前同步/合库失败，搜索尚未开始，任务进度保留：{exc}') from exc
            atomic_bytes(root/'preflight.json',json.dumps(dict(pr=stamp,libraries=info,
                priority=KINDS),ensure_ascii=False,indent=2).encode())
            claims |= model_claims
        else:claims=set(override_claims)
        upstream_claims=claims.copy()
        claims |= cdb_claims(ctx,latest,log)
        upgrade(root,db,meta,ctx,pause,log)
        log('内置搜索顺序：模型 → 动画（含动画包）→ 声音（文件/别名）→ 材质 → 图片。')
        maximum=total(db)[1]
        done=total(db)[0]
        has_evidence=bool(json.loads(meta['inputs']).get('evidence'))
        auto=ctx.source.adaptive and bool(set(ctx.pools)&set(adaptive.methods.KINDS))
        if auto:adaptive.setup(db)
        progress(done,maximum)
        work=root/'engine'
        count=0
        budget=(float('inf') if auto else maximum-done) if ctx.source.continuous else ctx.source.millions*1_000_000
        if ctx.source.continuous and done < maximum:
            log('持续搜索已开启。完成一批后自动接着跑，直到暂停或候选跑完。')
        gpu=None
        preference={}
        backend_decisions=[]
        exclusions=community.SearchExclusions(ctx,claims,((k,int(h,16)) for k,h in db.execute('select kind,key from hit')))
        safe=exclusions.safe
        claims_path=work/'state/claimed.txt'
        cpu_prepared=False
        def capture_export():
            found=defaultdict(dict)
            for kind,key,name in db.execute('select kind,key,name from hit'):
                if (kind,int(key,16)) not in claims:found[kind][int(key,16)]=name
            number=db.execute('select count(*) from hit').fetchone()[0]
            report=adaptive.report_rows(db) if auto else None
            return found,number,report

        export_worker=ExportWorker(ctx,root,latest)
        background_resources.callback(export_worker.close)
        def render_export(payload):
            with metrics.measure('background_export'):
                messages=export_worker.render(payload)
            for message in messages:log(message)

        def exported():
            with db:db.execute('insert or replace into meta values (?,?)',('export','live'))

        exporter=BackgroundExport(render_export,exported,log,AUTOSAVE_SECONDS,metrics.measure)
        background_resources.callback(exporter.close)
        def autosave(force=False):
            number=db.execute('select count(*) from hit').fetchone()[0]
            exporter.pulse(number,capture_export,force)

        def claim_bytes(ids):
            return ''.join(f'{key:x}\n' for key in sorted(ids)).encode('ascii')

        def prepare_cpu():
            nonlocal cpu_prepared
            if cpu_prepared:
                return
            (work/'snapshots').mkdir(parents=True,exist_ok=True)
            (work/'data').mkdir(exist_ok=True)
            shutil.copy2(core.ASSETS/'engine/suffixes.txt',work/'data/suffixes.txt')
            # Stage a fresh copy only if the CPU engine is actually called.
            with tempfile.TemporaryDirectory(dir=work,prefix='snapshot-') as dir_name:
                for ext in ('.ids','.pools.txt'):
                    dest=work/'snapshots'/(ctx.game.lower()+ext)
                    staged=Path(dir_name)/dest.name
                    shutil.copyfile(ctx.path.with_suffix(ext),staged)
                    os.replace(staged,dest)
            atomic_bytes(claims_path,claim_bytes(safe))
            cpu_prepared=True

        def execute_batch(kinds,mode,opens,ends,words):
            nonlocal gpu
            if gpu is not None:
                try:
                    with metrics.measure('gpu_verification'):
                        return gpu.batch(ctx,kinds,mode,opens,ends,words,safe,log)
                except (GpuError,OSError,ValueError) as exc:
                    gpu_resources.close()
                    gpu=None
                    log(f'GPU 未完成此批，改用 CPU 补跑：{exc}')
            prepare_cpu()
            with metrics.measure('cpu_verification'):
                return engine(work,ctx,kinds,mode,opens,ends,words,log)

        def execute_plan(gid,kinds,opens,ends,words,candidates):
            nonlocal gpu
            if (ctx.source.engine!='auto' or gpu is None or len(kinds)!=1 or kinds[0]=='sound_alias'
                    or preference.get(gid)=='gpu-forward'
                    or not (core.ASSETS/'engine'/'studio_peel.exe').is_file()):
                return execute_batch(kinds,'plan',opens,ends,words)
            kind=kinds[0]
            target=gpu.targets[kind]-safe
            if not backends.eligible(ends,candidates,len(target)):
                return execute_batch(kinds,'plan',opens,ends,words)

            def cpu_sample():
                with metrics.measure('cpu_verification'):
                    return backends.peel_batch(work,ctx,kind,opens,ends,words,target,log)

            def gpu_sample():
                with metrics.measure('gpu_verification'):
                    return gpu.batch(ctx,kinds,'plan',opens,ends,words,safe,log)

            if preference.get(gid)=='cpu-peel':
                try:return cpu_sample()
                except (OSError,ValueError,RuntimeError) as exc:
                    preference[gid]='gpu-forward'
                    log(f'CPU 后缀反推未完成，交给 GPU 重试：{exc}')
                    return execute_batch(kinds,'plan',opens,ends,words)

            started=time.perf_counter()
            try:gpu_rows=gpu_sample()
            except (GpuError,OSError,ValueError) as exc:
                gpu_resources.close();gpu=None
                log(f'GPU 样本未完成，使用 CPU 原版引擎：{exc}')
                return execute_batch(kinds,'plan',opens,ends,words)
            gpu_seconds=time.perf_counter()-started
            if pause.flag.is_set():return gpu_rows
            try:
                rows,decision=backends.compare(gpu_rows,gpu_seconds,cpu_sample,candidates)
            except (OSError,ValueError,RuntimeError) as exc:
                preference[gid]='gpu-forward'
                log(f'CPU 样本未完成，保留已核对的 GPU 结果：{exc}')
                return gpu_rows
            preference[gid]=decision['backend']
            backend_decisions.append(dict(group=gid,kind=kind,targets=len(target),**decision))
            log(f'引擎实测 / {core.LABEL[kind]}，{candidates:,} 个候选：GPU 正向 {decision["gpu_seconds"]:.3f} 秒，'
                f'CPU 反推 {decision["cpu_seconds"]:.3f} 秒，选择 {"CPU 后缀反推" if decision["backend"]=="cpu-peel" else "GPU 正向"}。')
            if not decision['matches_equal']:log('两种引擎的样本命中集合不同，已沿用 GPU 结果。')
            return rows

        log(f'从已完成的 {done:,}/{maximum:,} 个候选继续；社区、CDB、任务结果共排除 {len(safe):,} 个 ID。')
        if engine is process_batch and budget>0 and (done<maximum or auto) and not pause.flag.is_set():
            if ctx.source.engine in ('auto','gpu'):
                log('正在检查 OpenCL GPU…')
                try:
                    gpu=GpuSearch(gpu_resources,ctx,safe,log)
                except (GpuError,OSError,ValueError) as exc:
                    gpu_resources.close()
                    log(f'GPU 不可用，使用 CPU：{exc}')
            else:
                log('已选择 CPU：使用 Hash Slinging Slasher 原版引擎。')
        auto_status=None
        for phase in core.SEARCH_PHASES:
            if auto and ctx.game=='MODWAR7' and 'xanim' in phase and ctx.pools.get('xanim') and count<budget:
                try:
                    with metrics.measure('method_preparation'):
                        animation_hits=resume_animation_evidence(db,ctx,latest,claims,pause.check,log)
                except InterruptedError:
                    break
                except (OSError,ValueError) as exc:
                    log(f'MW7 自动动画线索暂时无法读取，将继续搜索：{exc}')
                    animation_hits={}
                if animation_hits:
                    added=exclusions.add(('xanim',key) for key in animation_hits)
                    if added and cpu_prepared:
                        with claims_path.open('ab') as feed:
                            feed.write(claim_bytes(added))
                    autosave()
            for gid,kinds,mode,width,length,pos,raw_plan in db.execute('select id,kinds,mode,width,count,pos,words from groups order by id').fetchall():
                kinds=json.loads(kinds)
                if not set(kinds).issubset(phase):continue
                if pause.flag.is_set() or count>=budget:break
                if pos>=length:continue
                opens,ends,stems=json.loads(raw_plan) if mode=='plan' else ([],[],[])
                while pos<length and not pause.flag.is_set():
                    # Amortize process startup / writes for combinations. Keep the
                    # smaller limit for lists, whose strings must fit in memory.
                    limit=PLAN_BATCH if engine is process_batch and mode=='plan' else BATCH
                    if (mode=='plan' and gpu is not None and ctx.source.engine=='auto' and len(kinds)==1
                            and kinds[0]!='sound_alias' and len(ends)>=32
                            and (core.ASSETS/'engine'/'studio_peel.exe').is_file()):
                        # The reverse index is rebuilt per batch. Give it a
                        # larger comparable workload only for small target sets.
                        targets=len(gpu.targets[kinds[0]])
                        probe_size=min((length-pos)*width,budget-count,AUTO_NARROW_PLAN_BATCH)
                        if targets<=2048 and backends.eligible(ends,probe_size,targets):
                            limit=AUTO_NARROW_PLAN_BATCH
                    step=min(length-pos,max(1,limit//width),(budget-count)//width)
                    if step<=0:break
                    with metrics.measure('candidate_generation'):
                        words=stems[pos:pos+step] if mode=='plan' else [r[0] for r in db.execute('select name from manual order by idx limit ? offset ?',(step,pos))]
                    if len(words)!=step:raise RuntimeError('任务候选队列损坏。')
                    log(f'本批：{pos*width:,} 至 {(pos+step)*width:,}，{step*width:,} 个候选（{"、".join(core.LABEL[k] for k in kinds)}）。')
                    if mode=='plan':batch=execute_plan(gid,kinds,opens,ends,words,step*width)
                    else:batch=execute_batch(kinds,mode,opens,ends,words)
                    with db:
                        db.executemany('insert or ignore into hit values (?,?,?)',batch)
                        db.execute('update groups set pos=? where id=?',(pos+step,gid))
                    pos+=step
                    count+=step*width
                    metrics.committed('fixed',step*width)
                    done+=step*width
                    progress(done,maximum)
                    autosave()
                    if batch:
                        added=exclusions.add((k,int(h,16)) for k,h,_ in batch)
                        if added and cpu_prepared:
                            # The original CPU loader reads an unordered set. This
                            # scratch file is rebuilt from the saved task on resume.
                            with claims_path.open('ab') as feed:
                                feed.write(claim_bytes(added))
            unfinished=any(set(json.loads(k)).issubset(phase) and p<n for k,p,n in db.execute('select kinds,pos,count from groups'))
            if unfinished or pause.flag.is_set() or count>=budget:break
            if auto and set(phase)&set(adaptive.methods.KINDS)&set(ctx.pools) and count<budget and not pause.flag.is_set():
                def execute(kinds,words):
                    batch=execute_batch(kinds,'manual',[],[],words)
                    for kind,key,name in batch:
                        if not core.matches(ctx,kind,int(key,16),name):
                            raise ValueError('命中未通过 CPU 复核。')
                    added=exclusions.add((k,int(h,16)) for k,h,_ in batch)
                    if added and cpu_prepared:
                        with claims_path.open('ab') as feed:feed.write(claim_bytes(added))
                    return batch
                def advance():
                    progress(*total(db))
                    autosave()
                tried,auto_status=adaptive.run(root,db,ctx,latest,claims,pause,log,execute,budget-count,advance,
                                               allowed_kinds=phase,metrics=metrics)
                count+=tried
                done,maximum=total(db)
                progress(done,maximum)
        autosave(force=True)
        export_rel='exports/'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:5]
        export=root/export_rel
        with metrics.measure('final_export'):
            export.mkdir(parents=True)
            found=defaultdict(dict)
            for k,h,n in db.execute('select kind,key,name from hit'):
                key=int(h,16)
                if (k,key) not in claims:found[k][key]=n
            rows=core.results_export(ctx,found,export,log,latest_cdb=latest,previous=root/'live')
            history.complete_export(ctx,export,latest)
            if has_evidence or auto:
                counts=evidence.coverage(ctx,upstream_claims,found,export,latest)
                for kind, summary in counts.items():
                    log(f'{core.LABEL[kind]}：{summary["captured"]:,} 个键，仍缺 {summary["unresolved"]:,} 个名称。')
            (export/'project.json').write_text(json.dumps(asdict(ctx.source),ensure_ascii=False,indent=2),encoding='utf-8')
            if auto:
                adaptive.report(db,export)
            community.local_preview(ctx,rows,export,claims)
            with db:db.execute('insert or replace into meta values (?,?)',('export',export_rel))
        status=('已暂停' if pause.flag.is_set() else '本轮预算已用完，仍可继续' if count>=budget and done<maximum else None) or auto_status or ('名称线索已验证' if has_evidence and maximum == 0 else '候选已跑完' if done==maximum
                else '已暂停' if pause.flag.is_set() else '本轮预算已用完，仍可继续')
        stats=metrics.report(status,done,maximum)
        stats['backend_decisions']=backend_decisions
        batches=stats['batches']
        log(f'{status}。本轮提交 {batches["total"]:,} 批（固定 {batches["fixed"]:,}，自动 {batches["adaptive"]:,}），'
            f'验证 {stats["candidates"]["total"]:,} 个候选，累计进度 {done:,}/{maximum:,}。')
        log(timing_summary(stats))
        payload=json.dumps(stats,ensure_ascii=False,indent=2).encode('utf-8')
        for target in (root/'performance.json',export/'performance.json'):
            try:atomic_bytes(target,payload)
            except OSError as exc:log(f'无法写入耗时报告 {target}：{exc}')
        return export,rows,done,maximum,status
