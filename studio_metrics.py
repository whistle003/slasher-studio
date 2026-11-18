"""Wall-clock measurements for one search round, including overlapping work."""
from contextlib import contextmanager
from threading import Lock
from time import perf_counter


STAGES = {
    'candidate_generation': '候选生成与取批',
    'candidate_wait': '等待预生成',
    'sqlite_dedup': 'SQLite 去重与入队',
    'method_preparation': '语料与规则准备',
    'gpu_verification': 'GPU 验证',
    'cpu_verification': 'CPU 验证',
    'background_export': '后台导出',
    'export_flush': '收尾等待后台导出',
    'final_export': '最终导出',
}


class RoundMetrics:
    def __init__(self):
        self.started = perf_counter()
        self.lock = Lock()
        self.stages = {name: {'seconds': 0., 'calls': 0} for name in STAGES}
        self.batches = {'fixed': 0, 'adaptive': 0}
        self.candidates = {'fixed': 0, 'adaptive': 0}

    @contextmanager
    def measure(self, name):
        start = perf_counter()
        try:
            yield
        finally:
            elapsed = perf_counter() - start
            with self.lock:
                self.stages[name]['seconds'] += elapsed
                self.stages[name]['calls'] += 1

    def committed(self, mode, candidates):
        self.batches[mode] += 1
        self.candidates[mode] += candidates

    def report(self, status, done, maximum):
        return {
            'status': status,
            'wall_seconds': round(perf_counter() - self.started, 6),
            'timings_can_overlap': True,
            'batches': {**self.batches, 'total': sum(self.batches.values())},
            'candidates': {**self.candidates, 'total': sum(self.candidates.values())},
            'progress': {'checked': done, 'queued_or_checked': maximum},
            'stages': {name: {**data, 'seconds': round(data['seconds'], 6)}
                       for name, data in self.stages.items()},
        }


def timing_summary(report):
    timings = [f'{label} {report["stages"][name]["seconds"]:.3f} 秒'
               for name, label in STAGES.items()]
    return (f'本轮总耗时 {report["wall_seconds"]:.3f} 秒。分阶段耗时（可能重叠）：'
            + '，'.join(timings) + '。')
