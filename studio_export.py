"""One exporter, immutable committed snapshots, no database access in the worker."""
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime
import json,multiprocessing,tempfile
from pathlib import Path
import time

_export_context=None


def export_initialize(ctx,root,latest):
    global _export_context
    _export_context=(ctx,root,latest)


def render_snapshot(ctx,root,latest,payload):
    import studio_core as core
    import studio_history as history
    import studio_community as community
    from studio_tasks import publish_live
    found,number,report=payload
    logs=[]
    with tempfile.TemporaryDirectory(dir=root,prefix='.live-stage-') as temporary:
        stage=Path(temporary)/'output';stage.mkdir()
        rows=core.results_export(ctx,found,stage,logs.append,latest_cdb=latest,previous=root/'live')
        history.complete_export(ctx,stage,latest)
        (stage/'project.json').write_text(json.dumps(asdict(ctx.source),ensure_ascii=False,indent=2),encoding='utf-8')
        if report is not None:(stage/'method_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        # The parent already removed its fixed preflight claims from this snapshot.
        community.local_preview(ctx,rows,stage,set())
        (stage/'.studio-live').write_text('Slasher Studio automatic export\n',encoding='ascii')
        (stage/'saved.json').write_text(json.dumps(dict(saved=datetime.now().isoformat(),hits=number,
            exported=sum(map(len,rows.values())))),encoding='utf-8')
        live=publish_live(root,stage)
    logs.append(f'后台保存完成：TXT、CDB 和本地 PR 预览已更新到 {live}')
    return logs


def render_initialized(payload):
    return render_snapshot(*_export_context,payload)


class ExportWorker:
    def __init__(self,ctx,root,latest):
        self.context=(ctx,root,latest);self.pool=None

    def render(self,payload):
        if self.pool is None:
            self.pool=ProcessPoolExecutor(max_workers=1,mp_context=multiprocessing.get_context('spawn'),
                initializer=export_initialize,initargs=self.context)
        try:return self.pool.submit(render_initialized,payload).result()
        except BrokenProcessPool:
            self.pool.shutdown(wait=True,cancel_futures=True);self.pool=None
            raise

    def close(self):
        if self.pool:self.pool.shutdown(wait=True,cancel_futures=True)


class BackgroundExport:
    def __init__(self,render,on_saved,log,interval,measure=lambda _:nullcontext()):
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='cdb-export')
        self.render=render;self.on_saved=on_saved;self.log=log;self.interval=interval
        self.measure=measure
        self.job=None;self.saved=-1;self.submitted_at=-float('inf')

    def collect(self,wait=False):
        if self.job is None:return
        number,future=self.job
        if not wait and not future.done():return
        self.job=None
        try:
            if future.done():
                future.result()
            else:
                with self.measure('export_flush'):
                    future.result()
            self.saved=number
            self.on_saved()
        except Exception as exc:
            self.log(f'后台导出暂未完成，命中已保存在任务数据库，稍后重试：{exc}')

    def pulse(self,number,capture,force=False):
        self.collect(wait=force)
        if self.job is not None or not number or number==self.saved:return
        if not force and time.monotonic()-self.submitted_at<self.interval:return
        payload=capture()
        self.submitted_at=time.monotonic()
        self.job=(number,self.pool.submit(self.render,payload))
        if force:self.collect(wait=True)

    def close(self):
        try:self.collect(wait=True)
        finally:self.pool.shutdown(wait=True,cancel_futures=True)
