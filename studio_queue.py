"""Bounded producer and batched durable candidate insertion; SQLite stays on its owner thread."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from functools import lru_cache
import hashlib
import sqlite3
import time

import studio_evidence as evidence
from studio_parallel import Generator


MIN_BATCH=25_000
MAX_BATCH=250_000
MAX_SEEDS=8192
MAX_BYTES=24*1024*1024
SQLITE_INSERT_CHUNK=2048


class BatchSize:
    def __init__(self):self.target=100_000
    def observe(self,count,seconds):
        if count<MIN_BATCH or seconds<=0:return
        wanted=int(count*.25/seconds)
        wanted=max(self.target//2,min(self.target*2,wanted))
        self.target=max(MIN_BATCH,min(MAX_BATCH,(self.target+wanted)//2))


def generate(plan,known,pos,target,keyword,check):
    started=time.monotonic()
    found={}
    size=0
    stop=pos
    for seed in plan.seeds[pos:min(len(plan.seeds),pos+MAX_SEEDS)]:
        check()
        for i,name in enumerate(plan.emit(seed)):
            if i%1024==0:check()
            if name in known or (keyword and keyword not in name) or not evidence.SAFE.fullmatch(name):continue
            encoded=name.encode()
            key=hashlib.sha256(encoded).digest()
            if key not in found:
                found[key]=name;size+=len(encoded)
        stop+=1
        if len(found)>=target or size>=MAX_BYTES:break
    return stop,list(found.items()),time.monotonic()-started


@lru_cache(maxsize=32)
def seen_insert(size):
    values=','.join(f'(?1,?{i})' for i in range(2,size+2))
    return f'INSERT INTO adaptive_seen(kind,digest) VALUES {values} ON CONFLICT(kind,digest) DO NOTHING RETURNING digest'


@lru_cache(maxsize=32)
def queue_insert(size):
    values=','.join(f'(?1,?{i})' for i in range(2,size+2))
    return f'INSERT INTO adaptive_queue(run,name) VALUES {values}'


def enqueue(db,rid,kind,stop,rows,seconds):
    """Insert and deduplicate in one index operation, then commit the shard."""
    names={}
    for digest,name in rows:
        names.setdefault(digest,name)
    ordered=sorted(names)
    fresh=set()
    limit=min(SQLITE_INSERT_CHUNK,db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)-1)
    with db:
        # Sorted keys retain locality in the existing index. RETURNING only
        # yields inserted keys, so a separate history lookup is unnecessary.
        for offset in range(0,len(ordered),limit):
            chunk=ordered[offset:offset+limit]
            fresh.update(digest for digest, in db.execute(seen_insert(len(chunk)),(kind,*chunk)))
        # The queue follows the original candidate order, independent of index order.
        accepted=[name for digest,name in names.items() if digest in fresh]
        for offset in range(0,len(accepted),limit):
            chunk=accepted[offset:offset+limit]
            db.execute(queue_insert(len(chunk)),(rid,*chunk))
        db.execute('UPDATE adaptive_run SET seed_pos=?,seconds=seconds+? WHERE id=?',(stop,seconds,rid))
    return len(fresh)


class Producer:
    """At most one speculative shard; it never changes persisted progress."""
    def __init__(self,log=lambda _:None,measure=lambda _:nullcontext()):
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='candidate-producer')
        self.job=None
        self.generator=Generator(log)
        self.measure=measure
    def generate(self,*args):
        with self.measure('candidate_generation'):
            return self.generator.generate(*args,serial=generate,max_seeds=MAX_SEEDS,max_bytes=MAX_BYTES)
    def start(self,token,*args):
        if self.job is None:self.job=(token,self.pool.submit(self.generate,*args))
    def take(self,token):
        if self.job is None or self.job[0]!=token:return None
        _,future=self.job;self.job=None
        if future.done():return future.result()
        with self.measure('candidate_wait'):
            return future.result()
    def close(self):
        if self.job:self.job[1].cancel()
        self.generator.cancel.set()
        self.pool.shutdown(wait=True,cancel_futures=True)
        self.generator.close()
