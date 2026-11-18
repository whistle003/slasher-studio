# Engine and converter sources

Slasher Studio orchestrates hash-slinging-slasher verification, exports confirmed
`hash,name` TXT files, merges their names with the selected databases as CSV, and
runs the unmodified cod-name-db `NameIndex.exe` to create Saluki CDBs. Every CDB
is read back and compared with all expected entries before being exposed.

- CPU executables: KingslayerKyle/hash-slinging-slasher at
  `375cbfb81ab2ff0db474d18184e29dc50b02d6e5`, GPL-3.0-or-later.
- Candidate methods: same repository at `208c7d641e665475bc385e8446e0189d7f407bc5`.
  Studio adapts these generators for a persistent task queue; it does not run
  upstream's autonomous GitHub submission workflow.
- NameIndex: echo000/cod-name-db at `d561dabe7b73c766b8ea04643af07ecca52a9422`,
  built from unchanged `src`, using the included Cargo.lock.
  porter-lib dependency resolves to `4642e89d` in that lockfile.
- GPU: Studio OpenCL implementation; recovered names are independently rehashed
  and checked against the typed capture, including full-width audio alias keys.
- CPU suffix comparison: `studio_peel.rs` calls the original `slasher::search::Meet`
  at `375cbfb81ab2ff0db474d18184e29dc50b02d6e5` with the modern ordinary basis.
  `build_peel.ps1` accepts the pinned upstream checkout (or the bundled source
  archive) and a Rust compiler with the Windows C++ linker environment.

## Adapted candidate methods

All following paths are under hash-slinging-slasher's `scripts/`:

- `final_byte.py`: inverse FNV last-byte recovery, both possible high bits for
  ordinary assets, exact 64 bits for aliases.
- `token_edits.py`: position-based substitution/insertion/deletion. Studio keeps
  the first token and uses at most 12 alternatives with at least 3 witnesses.
- `contributed/witnessed_animation_block_lengths.py`: one/two-token internal
  blocks; at least 3 independent table frames with matching anchors.
- `contributed/verified_modern_animation_pairs_20261009-181353.py`: coupled
  substitutions at gaps 2–6, at least 3 complete target-game sibling frames.
- `contributed/sound_tail_retarget_lang_20261015-093704.py`: verified source
  stems retargeted to codec tails observed at least 3 times in the current game.
- `contributed/sound_tail_grammar_20261014-064103.py`: combine codec, quality,
  sample rate and language components witnessed at least 5 times. Studio uses
  measured fields instead of a hardcoded list of possible formats.
- `contributed/alias_to_file_placed_20261016-053603.py` and
  `contributed/file_to_alias_20261008-204342.py`: shared 2–8-token family placement
  in observed sound directories, and reverse basename/take-prefix reductions.
- `contributed/mw7_modern_numeric_siblings_20261008-075016.py`: try indices 0–16
  and literal channel, MP/SP and LOD variants from a single verified visual
  name, covering seeds with no sibling family to train a numeric gap rule.
- `contributed/verified_sound_basename_byte_20261009-075239.py`: change the last
  character before a sound's encoding tail. Studio bounds the variant alphabet
  and keeps each seed's own verified directory and tail. The normal engine
  confirms these candidates in place of the upstream script's broad inverse cross product.
- `contributed/verified_image_joint_wrappers_20261009-213636.py`: preserve the
  image body and change both wrappers together. Three independent verified
  target-table name pairs are required to learn a rule.

Studio also fills small numeric gaps in witnessed name families (at least two
siblings, observed span at most 256, extend by two). Generic token, number and
last-byte methods apply to animations, materials, images and audio aliases;
audio files keep their directory and format tail intact.

Each method records inputs, progress, tested candidates and verified hits.
Successful names feed later passes. Identical inputs and duplicate candidates
are skipped. Once the available methods finish without new inputs, the task
stops. Completion does not mean that every remaining hash has been recovered.

In 1.10, new names also create durable delta-seed runs using the parent's
measured rules. These runs receive scheduling priority before the old backlog
finishes. Number-family rule training still waits for the full refresh.
Speculative candidate production uses one bounded background thread; only the
task thread writes SQLite. Existing SHA-256 candidate keys are retained, and
an old task is backed up before upgrading its metadata and adding seed payloads.

PR output is a local draft only. Cached claims are not proof of current upstream
novelty. No search or conversion action publishes a PR.
