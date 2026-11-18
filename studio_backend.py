"""Measure equivalent CPU suffix and OpenCL forward batches before selecting a backend."""
from pathlib import Path
import subprocess
import tempfile
from time import perf_counter

import studio_core as core
from studio_gpu import plan_parts

CPU_ADVANTAGE = .05


def eligible(ends, candidates, targets):
    # Reject disfavored plans before spending time building the reverse index.
    # GPU is also the standard route for explicit name lists and sound aliases.
    entries = 2 * len(ends) * targets
    return (len(ends) >= 8 and candidates >= 200_000 and targets > 0
            and entries <= 1_000_000 and entries * 8 <= candidates)


def peel_batch(work, ctx, kind, opens, ends, words, targets, log):
    opens, ends, words = (plan_parts(parts) for parts in (opens, ends, words))
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work, prefix='peel-') as folder:
        path = Path(folder) / 'plan.txt'
        with path.open('w', encoding='utf-8', newline='\n') as stream:
            stream.write(f'{len(opens)} {len(ends)} {len(words)} {len(targets)}\n')
            for name in (*opens, *ends, *words):
                stream.write(name + '\n')
            stream.writelines(f'{key:x}\n' for key in sorted(targets))
        result = subprocess.run([str(core.ASSETS/'engine'/'studio_peel.exe'), str(path)], cwd=work,
                                capture_output=True, encoding='utf-8', errors='strict',
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:600] or 'CPU 后缀反推失败。')
    lines = result.stdout.splitlines()
    if not lines or lines[-1] != 'STUDIO_DONE':
        raise ValueError('CPU 后缀反推返回不完整。')
    rows = set()
    for line in lines[:-1]:
        if not line.startswith('STUDIO_HIT\t'):
            continue
        _, raw_key, name = line.split('\t',2)
        key = int(raw_key,16)
        name = name.translate(core.ASCII_FOLD)
        if key not in targets or not core.matches(ctx,kind,key,name):
            raise ValueError('CPU 后缀反推命中未通过复核。')
        rows.add((kind,f'{key:016x}',name))
    log(f'CPU 后缀反推：命中 {len(rows):,} 条。')
    return sorted(rows)


def compare(gpu_rows, gpu_seconds, cpu, candidates):
    started = perf_counter()
    cpu_rows = cpu()
    cpu_seconds = perf_counter() - started
    equal = set(gpu_rows) == set(cpu_rows)
    chosen = 'cpu-peel' if equal and cpu_seconds <= gpu_seconds * (1-CPU_ADVANTAGE) else 'gpu-forward'
    info = dict(backend=chosen, candidates=candidates, matches_equal=equal,
                gpu_seconds=gpu_seconds, cpu_seconds=cpu_seconds)
    return (cpu_rows if chosen == 'cpu-peel' else gpu_rows), info
