"""OpenCL backend. The database cursor stays with studio_tasks."""

from array import array
from contextlib import ExitStack
import ctypes as c
import os
from pathlib import Path
import re
from time import perf_counter

import studio_core as core


class GpuError(RuntimeError):
    """This batch can be safely run on CPU instead."""


P, U, I, Q, S = c.c_void_p, c.c_uint32, c.c_int32, c.c_uint64, c.c_size_t
BASES = {'ordinary': core.IW_BASIS, 'sound_alias': core.ALIAS_BASIS}
WORK_SIZE = 262_144
EMPTY = core.UINT64
ASCII_SPACING = ' \t\r\n\v\f'


def status(code, operation):
    if code != 0:
        raise GpuError(f'{operation}（OpenCL {code}）')


class OpenCL:
    def __init__(self, resources):
        if os.name != 'nt':
            raise GpuError('这版 GPU 后端需要 Windows OpenCL 驱动。')
        try:
            dll = c.WinDLL(str(Path(os.environ['SystemRoot'])/'System32'/'OpenCL.dll'))
            for name, result, args in (
                ('GetPlatformIDs', I, (U, P, P)),
                ('GetDeviceIDs', I, (P, Q, U, P, P)),
                ('GetDeviceInfo', I, (P, U, S, P, P)),
                ('CreateContext', P, (P, U, P, P, P, P)),
                ('CreateCommandQueue', P, (P, P, Q, P)),
                ('CreateProgramWithSource', P, (P, U, P, P, P)),
                ('BuildProgram', I, (P, U, P, c.c_char_p, P, P)),
                ('GetProgramBuildInfo', I, (P, P, U, S, P, P)),
                ('CreateKernel', P, (P, c.c_char_p, P)),
                ('CreateBuffer', P, (P, Q, S, P, P)),
                ('SetKernelArg', I, (P, U, S, P)),
                ('EnqueueNDRangeKernel', I, (P, P, U, P, P, P, U, P, P)),
                ('EnqueueReadBuffer', I, (P, P, U, S, S, P, U, P, P)),
                *[(f'Release{n}', I, (P,)) for n in ('Context','CommandQueue','Program','Kernel','MemObject')],
            ):
                fn = getattr(dll, 'cl'+name)
                fn.restype, fn.argtypes = result, args
                setattr(self, name, fn)
        except (OSError, AttributeError, KeyError) as exc:
            raise GpuError('没有可用的 OpenCL 驱动。') from exc

        platforms = self.enumerate(self.GetPlatformIDs)
        devices = []
        for platform in platforms:
            for device in self.enumerate(self.GetDeviceIDs, platform, 4):  # CL_DEVICE_TYPE_GPU
                if all(self.property(device, prop, U) for prop in (0x1026, 0x1027, 0x1028)):
                    units = self.property(device, 0x1002, U)
                    memory = self.property(device, 0x101F, Q)
                    # Integrated GPUs usually report shared host memory (OpenCL 1.1+).
                    unified = self.property(device, 0x1035, U, default=0)
                    devices.append((not unified, units, memory, platform, device))
        if not devices:
            raise GpuError('未找到可编译 OpenCL 内核的 GPU。')
        failures = []
        for _, _, _, platform, device in sorted(devices, reverse=True):
            with ExitStack() as attempt:
                self.device = device
                self.name = self.info(device, 0x102B)
                props = (c.c_ssize_t*3)(0x1084, platform, 0)  # CL_CONTEXT_PLATFORM
                one = (P*1)(device)
                try:
                    self.context = self.create(attempt, 'Context', props, 1, one, None, None)
                    self.queue = self.create(attempt, 'CommandQueue', self.context, device, 0)
                    source = (core.ASSETS/'engine'/'fnv_search.cl').read_bytes()
                    strings = (c.c_char_p*1)(source)
                    program = self.create(attempt, 'ProgramWithSource', self.context, 1, strings,
                                          (S*1)(len(source)), release='Program')
                    code = self.BuildProgram(program, 1, one, b'-cl-std=CL1.2', None, None)
                    if code:
                        raise GpuError(f'内核编译失败（OpenCL {code}）：{self.build_log(program)}')
                    self.kernel = self.create(attempt, 'Kernel', program, b'find_names')
                    self.self_test()
                except GpuError as exc:
                    failures.append(f'{self.name}：{exc}')
                    continue
                resources.enter_context(attempt.pop_all())
                return
        raise GpuError(failures[0] if failures else '无法初始化 GPU。')

    @staticmethod
    def enumerate(fn, *args):
        n = U()
        code = fn(*args, 0, None, c.byref(n))
        if code in (-1, -1001):
            return ()
        status(code, '枚举 OpenCL 设备失败')
        if not n.value:
            return ()
        out = (P*n.value)()
        status(fn(*args, n, out, None), '读取 OpenCL 设备失败')
        return out

    def property(self, device, prop, typ, default=None):
        out = typ()
        code = self.GetDeviceInfo(device, prop, c.sizeof(out), c.byref(out), None)
        if code and default is not None:
            return default
        status(code, '读取 GPU 参数失败')
        return out.value

    def info(self, device, prop):
        n = S()
        status(self.GetDeviceInfo(device, prop, 0, None, c.byref(n)), '读取 GPU 名称失败')
        out = c.create_string_buffer(n.value)
        status(self.GetDeviceInfo(device, prop, n, out, None), '读取 GPU 名称失败')
        return out.value.decode('utf-8', errors='replace')

    def build_log(self, program):
        n = S()
        if self.GetProgramBuildInfo(program, self.device, 0x1183, 0, None, c.byref(n)) or not n.value:
            return ''
        out = c.create_string_buffer(n.value)
        self.GetProgramBuildInfo(program, self.device, 0x1183, n, out, None)
        return out.value.decode('utf-8', errors='replace').strip()[:700]

    def create(self, stack, action, *args, release=None):
        err = I()
        value = getattr(self, 'Create'+action)(*args, c.byref(err))
        status(err.value, '创建 OpenCL '+action+' 失败')
        if not value:
            raise GpuError('OpenCL 未返回 '+action)
        stack.callback(getattr(self, 'Release'+(release or action)), value)
        return value

    def buffer(self, stack, data, rw=False):
        val = c.c_char_p(data)
        flags = (1 if rw else 4) | 32  # CL_MEM_READ_WRITE / READ_ONLY | COPY_HOST_PTR
        return self.create(stack, 'Buffer', self.context, flags, len(data), val, release='MemObject')

    def launch(self, words, offsets, first_stem, first_end, np, ne, basis, table, mask, start, stop):
        capacity = min(4096, stop-start)
        while True:
            with ExitStack() as outputs:
                out = (U*(capacity+1))()
                result = self.buffer(outputs, bytes(out), rw=True)
                args = (P(words), P(offsets), U(first_stem), U(first_end), U(np), U(ne),
                        Q(basis), P(table), U(mask), U(start), U(stop), P(result), U(capacity))
                for i, arg in enumerate(args):
                    status(self.SetKernelArg(self.kernel, i, c.sizeof(arg), c.byref(arg)), '设置 GPU 内核参数失败')
                size = S(stop-start)
                status(self.EnqueueNDRangeKernel(self.queue, self.kernel, 1, None, c.byref(size),
                                                 None, 0, None, None), 'GPU 计算启动失败')
                status(self.EnqueueReadBuffer(self.queue, result, 1, 0, c.sizeof(out), out, 0, None, None),
                       '读取 GPU 搜索结果失败')
                count = out[0]
                if count <= capacity:
                    indexes = out[1:count+1]
                    if len(set(indexes)) != count or any(not start <= i < stop for i in indexes):
                        raise GpuError('GPU 返回的命中位置无效。')
                    return indexes
                if count > stop-start:
                    raise GpuError('GPU 返回的命中数无效。')
                capacity = count

    def self_test(self):
        for kind in ('ordinary', 'sound_alias'):
            with ExitStack() as stack:
                sample = 'm/gpu_check_alias'
                keys, mask = index_keys({core.name_key(sample, kind)&core.MASK})
                target = self.buffer(stack, keys)
                blob, offsets = pack_words(['','m/','gpu_check','_alias',''])
                words, offsets = self.buffer(stack, blob), self.buffer(stack, offsets)
                if self.launch(words, offsets, 2, 3, 2, 2, BASES[kind],
                               target, mask, 0, 4) != [2]:
                    raise GpuError('GPU 的 FNV-1a 自检结果不正确。')


def index_keys(keys):
    # Keep a guaranteed empty slot and at most 50% occupancy for negative lookups.
    size = 1 << (max(2, 2*len(keys))-1).bit_length()
    table = array('Q', [EMPTY])*size
    mask = size-1
    for key in keys:
        at = (((key & 0xFFFFFFFF) ^ (key >> 32))*0x9E3779B1) & mask
        while table[at] != EMPTY:
            at = (at+1)&mask
        table[at] = key
    return table.tobytes(), mask


def pack_words(names):
    blob = bytearray()
    offsets = array('I', [0])
    for name in names:
        blob.extend(name.translate(core.ASCII_FOLD).encode('utf-8'))
        offsets.append(len(blob))
    return bytes(blob or b'\0'), offsets.tobytes()


def plan_parts(values):
    result = sorted({s.partition('#')[0].strip() for s in values})
    if any(s.startswith('@') or '\0' in s or '\n' in s or '\r' in s for s in result):
        raise GpuError('包含需要由原引擎解释的组合计划。')
    return result


def manual_name(word):
    word = word.strip(ASCII_SPACING)
    head, sep, rest = word.partition(',')
    return rest.lstrip(ASCII_SPACING) if sep and core.HEX.fullmatch(head) else word


def unresolved(ctx, taken):
    census = ctx.path.with_suffix('.pools.txt').read_text(encoding='utf-8-sig')
    ranks = {core.canonical(m[2]):int(m[1]) for m in
             re.finditer(r'^\s*(\d+)[,\s]+(\S+?)[,\s]+(\d+)[ \t]*$', census, re.M)}
    result = {k: ids-taken for k, ids in ctx.pools.items()}
    for table in sorted(set(core.TABLE.values())|core.EXTRA_TABLES):
        path = ctx.table_dir/(table+'.csv')
        if not path.is_file():
            continue
        # Every modern table contributes low 63-bit keys to the Rust search.
        # Its optional recovered full-width forms retain exactly these low bits.
        with path.open(encoding='utf-8-sig') as stream:
            for row in stream:
                raw, sep, _ = row.partition(',')
                key = raw.strip().removeprefix('+')
                if sep and core.HEX.fullmatch(key):
                    key = int(key,16)&core.MASK
                    for pool in result.values():
                        pool.discard(key)
    return result, ranks


class GpuSearch:
    def __init__(self, stack, ctx, taken, log):
        self.stack = stack
        self.cl = OpenCL(stack)
        self.targets, self.ranks = unresolved(ctx, taken)
        self.prepared = {}
        log(f'已启用 GPU：{self.cl.name}（OpenCL）')

    def group(self, kinds):
        group = tuple(kinds)
        if group not in self.prepared:
            policy = {}
            # Slasher assigns a repeated ID to its first selected pool in the census.
            target = {}
            for kind in sorted(kinds, key=lambda k: self.ranks[k]):
                for key in self.targets[kind]:
                    target.setdefault(key,kind)
            for tag, basis in BASES.items():
                keys = {h:k for h,k in target.items() if (k=='sound_alias')==(tag=='sound_alias')}
                if keys:
                    entries,mask = index_keys(keys)
                    policy[basis] = (keys, self.cl.buffer(self.stack, entries), mask)
            self.prepared[group] = policy
        return self.prepared[group]

    def batch(self, ctx, kinds, mode, opens, ends, stems, taken, log):
        started = perf_counter()
        policy = self.group(kinds)
        if mode=='manual':
            opens, ends, stems = [''], [''], [manual_name(s) for s in stems]
            if any('\0' in s or len(s.splitlines())>1 for s in stems):
                raise GpuError('名单含需要由原引擎解释的字符。')
            if max(map(len,stems),default=0)>4096 or sum(map(len,stems))>64*1024**2:
                raise GpuError('候选名单单批过大。')
        else:
            opens, ends, stems = ['']+plan_parts(opens), ['']+plan_parts(ends), plan_parts(stems)
        np, ne = len(opens), len(ends)
        count = len(stems)*np*ne
        rows = []
        if count and policy:
            with ExitStack() as batch:
                blob, offsets = pack_words(opens+stems+ends)
                data, offsets = self.cl.buffer(batch, blob), self.cl.buffer(batch, offsets)
                for basis,(keys,buffer,mask) in policy.items():
                    for offset in range(0,count,WORK_SIZE):
                        indices = self.cl.launch(data, offsets, np, np+len(stems), np, ne,
                                                 basis, buffer, mask, offset,min(count,offset+WORK_SIZE))
                        for index in sorted(indices):
                            raw = opens[index//ne%np]+stems[index//(np*ne)]+ends[index%ne]
                            name = raw.translate(core.ASCII_FOLD)
                            is_alias = basis==core.ALIAS_BASIS
                            key = core.name_key(name, 'sound_alias' if is_alias else 'ordinary')
                            small = key&core.MASK
                            kind = keys.get(small)
                            if kind is None:
                                raise GpuError('GPU 命中未通过 CPU 回算。')
                            if small not in taken and core.matches(ctx,kind,key,name):
                                rows.append((kind,f'{key:016x}',name))
        elapsed=perf_counter()-started
        log(f'GPU：验证 {count:,} 个候选，命中 {len(rows):,}，耗时 {elapsed:.3f} 秒。')
        return rows
