# MPC `.xpj` format notes (reverse-engineered)

These notes document what was learned building the converter, from projects
saved by MPC 3 software in **Sample engine mode** (`"engineMode": "Sample"`,
product identifier `AC50`). Field names are as they appear in the JSON.
Everything here was verified against real project data, not documentation —
treat it as observed behavior, not a spec.

## Container

A `.xpj` file is gzip-compressed. Decompressed, it is an **ACVS container**:
five header lines followed by a JSON document.

```
ACVS
1.3.0.12
SerialisableProjectData
json
Linux
{ "data": { ... } }
```

Everything below lives under `data`.

## Top level

| Key | Notes |
|---|---|
| `samples[]` | `{name, path, loadImpl}` — paths relative to the `_[ProjectData]` folder |
| `tracks[]` | index 0 is the pad track; also `Submix N` and `Outputs N/M` mixer tracks |
| `sequences[]` | `{key, value}` pairs; `key` is the 0-based sequence slot |
| `songs[]` | 32 slots; empty (`"(unnamed)"`) unless Song mode was used |
| `mixer` | master `volume`, `submixes[]`, `outputs[]` |
| `masterTempo`, `masterTempoEnabled` | per-sequence `bpm` applies when disabled |

## Fader law

Levels (master, pad, submix) are stored normalized 0..1. The stored default
`0.7079` displays as 0 dB on the MPC, and `1.0` as +6 dB, which fits:

```
gain = 2 * v^2        # 0.7079 -> 1.0023 (~0 dB), 1.0 -> 2.0 (+6 dB)
```

Pan is 0..1 with 0.5 center.

## Drum program (`tracks[0].program.drum`)

`instruments[]` has 128 entries (one per pad). Pad *n* is triggered by MIDI
note `36 + n`. Each instrument:

- `layersv[]` — up to 8 sample layers: `sampleName`, `sampleFile`,
  `pitch` (semitones, = `fineTune` cents / 100), `sampleStart/End`,
  loop fields, velocity range.
- `mixable` — `volume`, `pan`, `audioRoute`, `sends[4]`, `inserts`.
- `monophonic` / `polyphony` — pads default to mono, poly 1 (retrigger cuts).
- `whichMuteGroup` — 0 = none; pads sharing a nonzero group choke each other.
- `zonePlayTime` — 1 = one-shot (note length is ignored on playback).
- `synthSection.ampEnvelope` — `Attack`, `Decay`, `Sustain`, `Release`,
  `Hold`, `AD`, `OneShot`, `DecayFromEnd`, each wrapped as `{value0: x}`.
  Times are normalized 0..1 with no documented seconds mapping.
- `synthSection.filterData` — cutoff/resonance per filter; cutoff 1.0 = open.

## Sequences

`sequences[].value`:

| Field | Notes |
|---|---|
| `bpm`, `tempoEnable` | per-sequence tempo |
| `lengthBars`, `lengthPulses` | **960 PPQ**; 4/4 bar = 3840 pulses |
| `trackClipMaps` | list of lanes; each lane is a list of `{key: trackName, value: clip}` |

A clip has `startPulses`, `endPulses`, `loopStartPulses`, `loopEndPulses`,
`loop`, and an `eventList.events[]`. If a clip loops shorter than its span,
events repeat (unroll them when flattening).

### Events

Every event: `{time (pulses), type, muted, invented, ...}`.

| `type` | Meaning |
|---|---|
| 3 | Note — payload in `note` |
| 1, 2 | Automation — payload in `automation: {note, value, parameter}` |

**`invented: true` marks auto-generated default points — only
`invented: false` events are real recorded data.**

Note payload: `note` (36 + pad), `velocity` (0..1), `length` (pulses),
`probability`, `ratchet`, and 16 modifier slots.

### 16 Levels tuning

Notes recorded in 16 Levels (Tuning) mode keep their pad's MIDI note and
store the pitch in modifier slot 0:

```
modifierActiveState0: true
EnumCerealisationWrapper(selectedModifierType): "Tuning (coarse)"
semitones = (modifierValue0 - 0.5) * 240      # exact integers
```

### Automation parameter IDs (observed)

| `parameter` | Meaning |
|---|---|
| 131 | Pad aftertouch / pressure (type-1 events; check `afterTouchToFilter` etc. before assuming it's audible) |
| 518 | Pad level (same 0..1 scale as the stored `mixable.volume`) |
| 519, 520 | Seen on a fine-tuned pad; likely tuning coarse/fine (unconfirmed) |
| 1044 | Pad pan (0.5 = center) |

## Routing

`mixable.audioRoute.destination` (observed values):

| Value | Destination |
|---|---|
| 1 | Submix 1 |
| 2 | Outputs 1/2 (main) |

`sends[4]` are the four send levels. Insert FX live in
`mixable.inserts.effects[]`; each has a `plugin.description` (name,
manufacturer, uid) and a `state` string — a base64-ish MPC-proprietary blob
that has resisted decoding. The effect *names* are readable; the settings
are not.

## Gotchas

- Sequence-level `seqEventList` is typically empty — the real events are in
  the per-track clips under `trackClipMaps`.
- Several distinct sequence slots can share one display name.
- `lengthPulses` is authoritative for sequence length; a 266.67 bpm
  "double-time" sequence is stored exactly like any other.
- WAV files may be float format; read RIFF chunks rather than assuming PCM.
