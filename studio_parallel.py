"""Bounded spawned workers. Only the parent may commit candidates or cursors."""
from concurrent.futures import ProcessPoolExecutor, TimeoutError
from concurrent.futures.process import BrokenProcessPool
from dataclasses import replace
import hashlib
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
import time
import threading

import cloudpickle
import studio_evidence as evidence

_loaded=None
_cancel=None


def initialize(cancel):
    global _cancel
    _cancel=cancel


def shard(path,start,seeds,keyword):
    global _loaded
    if _loaded is None or _loaded[0]!=path:
        with open(path,'rb') as stream:emit=cloudpickle.load(stream)
        _loaded=(path,emit)
    _,emit=_loaded
    rows=[];size=0
    for pos,seed in enumerate(seeds,start):
        if _cancel is not None and _cancel.is_set():raise InterruptedError('candidate generation paused')
        found={}
        for i,name in enumerate(emit(seed)):
            if i%1024==0 and _cancel is not None and _cancel.is_set():raise InterruptedError('candidate generation paused')
            if (keyword and keyword not in name) or not evidence.SAFE.fullmatch(name):continue
            encoded=name.encode();found.setdefault(hashlib.sha256(encoded).digest(),name)
        # Return individual seed boundaries, so the parent can stop at exactly
        # the serial generator's limit without skipping speculative work.
        rows.append((pos,list(found.items())))
        size+=sum(len(n.encode()) for n in found.values())
        if size>=6*1024*1024:break
    return rows


class Generator:
    def __init__(self,log=lambda _:None,workers=None):
        self.workers=workers or max(1,min(4,(os.cpu_count() or 2)-1))
        self.log=log;self.pool=None;self.temp=None;self.serialized={}
        self.lock=threading.RLock();self.sequence=0
        self.context=mp.get_context('spawn');self.cancel=self.context.Event()

    def generate(self,plan,known,pos,target,keyword,check,serial,max_seeds,max_bytes):
        with self.lock:
            try:
                return self._generate(plan,known,pos,target,keyword,check,serial,max_seeds,max_bytes)
            except (BrokenProcessPool,OSError) as exc:
                self.log(f'候选工作进程不可用，从未提交的原断点改用单进程生成：{exc}')
                self.close();self.workers=1
                return serial(plan,known,pos,target,keyword,check)

    def _generate(self,plan,known,pos,target,keyword,check,serial,max_seeds,max_bytes):
        if self.workers<2 or len(plan.seeds)-pos<256:
            return serial(plan,known,pos,target,keyword,check)
        started=time.monotonic();check()
        if self.pool is None:
            self.temp=tempfile.TemporaryDirectory(prefix='slasher-workers-')
            self.pool=ProcessPoolExecutor(max_workers=self.workers,mp_context=self.context,
                initializer=initialize,initargs=(self.cancel,))
            self.log(f'多核候选生成：启用 {self.workers} 个工作进程；断点仍由主任务统一提交。')
        token=id(plan.emit)
        if token not in self.serialized:
            path=Path(self.temp.name)/(str(self.sequence)+'.pkl');self.sequence+=1
            # This is an ephemeral, locally generated worker payload, never an
            # imported cache or user-supplied pickle. Keep object refs against id reuse.
            with path.open('wb') as stream:cloudpickle.dump(plan.emit,stream,protocol=5)
            if len(self.serialized)>=2:self.serialized.pop(next(iter(self.serialized)))
            self.serialized[token]=(str(path),plan.emit)
        path=self.serialized[token][0]
        found={};size=0;stop=pos;end=min(len(plan.seeds),pos+max_seeds)
        try:
            while stop<end:
                check()
                # Speculation is bounded to one wave. Results are consumed in
                # seed order regardless of which process finishes first.
                width=64
                futures=[(at,self.pool.submit(shard,path,at,plan.seeds[at:min(at+width,end)],keyword))
                         for at in range(stop,min(end,stop+width*self.workers),width)]
                try:
                    for at,future in futures:
                        if at!=stop:break  # a size-limited shard left a gap; retry it next wave
                        while True:
                            check()
                            try:rows=future.result(timeout=.1);break
                            except TimeoutError:pass
                        for seed,items in rows:
                            if seed!=stop:raise RuntimeError('candidate worker returned a noncontiguous shard')
                            for digest,name in items:
                                if name not in known and digest not in found:found[digest]=name;size+=len(name.encode())
                            stop+=1
                            if len(found)>=target or size>=max_bytes:
                                return stop,list(found.items()),time.monotonic()-started
                finally:
                    for _,future in futures:future.cancel()
            return stop,list(found.items()),time.monotonic()-started
        except InterruptedError:
            self.cancel.set()
            raise

    def close(self):
        self.cancel.set()
        if self.pool:self.pool.shutdown(wait=True,cancel_futures=True)
        self.serialized.clear()
        if self.temp:self.temp.cleanup()
