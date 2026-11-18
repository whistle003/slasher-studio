// Same name normalization and bases as the packaged Slasher CPU programs.
inline ulong add_word(ulong h, __global const uchar *s, uint lo, uint hi) {
    for (uint i = lo; i < hi; ++i)
        h = (h ^ s[i]) * 0x100000001b3UL;
    return h;
}

__kernel void find_names(
    __global const uchar *words, __global const uint *offsets,
    uint first_stem, uint first_end, uint nprefix, uint nend,
    ulong basis, __global const ulong *targets, uint mask,
    uint start, uint limit, __global uint *matches, uint capacity)
{
    const uint index = start + (uint)get_global_id(0);
    if (index >= limit)
        return;
    const uint stem = first_stem + index / (nprefix * nend);
    const uint begin = (index / nend) % nprefix;
    const uint end = first_end + index % nend;
    ulong h = add_word(basis, words, offsets[begin], offsets[begin + 1]);
    h = add_word(h, words, offsets[stem], offsets[stem + 1]);
    h = add_word(h, words, offsets[end], offsets[end + 1]);
    const ulong key = h & 0x7fffffffffffffffUL;
    uint slot = ((uint)key ^ (uint)(key >> 32)) * 0x9e3779b1U & mask;
    for (;;) {
        const ulong found = targets[slot];
        if (found == 0xffffffffffffffffUL)
            return;
        if (found == key) {
            const uint row = atomic_inc((volatile __global uint *)matches);
            if (row < capacity)
                matches[row + 1] = index;
            return;
        }
        slot = (slot + 1U) & mask;
    }
}
