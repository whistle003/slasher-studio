"""Specialized modern-name rules adapted from the upstream scripts."""
from collections import defaultdict
import itertools
import re


METHODS = {
    'xanim': {'visual-variants'},
    'sound_asset': {'sound-basename-byte'},
    'material': {'visual-variants'},
    'image': {'visual-variants', 'image-joint-wrappers'},
}
TRANSFORMS = (('_col', '_nml'), ('_nml', '_col'), ('_col', '_mask'),
              ('_mp', '_sp'), ('_sp', '_mp'), ('_lod0', '_lod1'))
NUMERIC_SLOT = re.compile(r'\d+')
SOUND_BYTES = 'abcdefghijklmnopqrstuvwxyz0123456789_-. '


def visual_variants(name):
    """A verified single seed is sufficient, even without established siblings."""
    for match in NUMERIC_SLOT.finditer(name):
        digits = match[0]
        if len(digits) > 3:
            continue
        for number in range(17):
            spelling = str(number).zfill(len(digits))
            if spelling != digits:
                yield name[:match.start()] + spelling + name[match.end():]
    for before, after in TRANSFORMS:
        if before in name:
            yield name.replace(before, after)


def sound_basename_byte(seed):
    prefix, tail = seed
    for char in SOUND_BYTES:
        yield prefix + char + tail


def image_cuts(name):
    tokens = tuple(name.split('_'))
    if not 5 <= len(tokens) <= 16 or not all(t.isascii() and t.isalnum() for t in tokens):
        return
    for left in (1, 2, 3):
        for right in (1, 2):
            if len(tokens) - left - right >= 3:
                yield tokens[left:-right], (tokens[:left], tokens[-right:])


def image_wrapper_rules(verified, check):
    frames = defaultdict(dict)
    for i, name in enumerate(sorted(verified)):
        if i % 4096 == 0:
            check()
        for body, wrapper in image_cuts(name):
            frames[body][wrapper] = name
    pairs = defaultdict(set)
    for i, wrappers in enumerate(frames.values()):
        if i % 4096 == 0:
            check()
        if 2 <= len(wrappers) <= 32:
            for a, b in itertools.combinations(sorted(wrappers), 2):
                if a[0] != b[0] and a[1] != b[1]:
                    pairs[a,b].add(tuple(sorted((wrappers[a],wrappers[b]))))
    rules = defaultdict(list)
    for i, ((a,b), witnesses) in enumerate(sorted(pairs.items())):
        if i % 4096 == 0:
            check()
        independent = set()
        count = 0
        for names in sorted(witnesses):
            if independent.isdisjoint(names):
                independent.update(names)
                count += 1
                if count == 3:
                    rules[a].append(b)
                    rules[b].append(a)
                    break
    return {source: tuple(sorted(targets)) for source,targets in rules.items()}


def image_replacements(name, rules):
    for body, wrapper in image_cuts(name):
        for beginning, ending in rules.get(wrapper, ()):
            yield '_'.join(beginning + body + ending)
