#!/usr/bin/env python3
"""Convert an MPC Sample .xpj export to a REAPER project (.rpp).

Usage:
    python3 mpc2rpp.py [project.xpj] [--with-effects]

Layout choices:
  - One REAPER track per MPC pad that is actually played, named after its
    sample. Pad mixer level/pan -> track volume/pan (MPC fader law:
    gain = 2 * v^2, so the stored 0.7079 default maps to 0 dB).
  - Every note event becomes an audio item at the note position; pads are
    one-shots so items run the full sample length. Velocity -> item gain.
  - Each non-empty sequence becomes a region, laid out back-to-back with a
    one-bar gap, with a tempo marker at each region start.
  - Per-layer fine tune and per-note 16 Levels "Tuning (coarse)" modifiers
    (semitones = (modifierValue0 - 0.5) * 240) -> item playrate (resample,
    like the MPC), with the item length stretched to match.
  - Recorded Q-link automation: pad level (param 518) -> track volume
    envelope, pad pan (param 1044) -> pan envelope. Pad aftertouch (131) is
    skipped (not routed to anything in this program); other params warn.
  - Amp envelopes -> item fades. Attack/decay are stored normalised 0..1
    with no documented time scale, so they are approximated as a fraction
    of the sample length (decay only when "DecayFromEnd" is set).
  - Mute groups and monophonic pads -> items are trimmed at the next
    choking hit with a 5 ms fade to avoid clicks.
  - --with-effects: recreates the mixer topology for insert FX. MPC plugin
    state blobs are proprietary, so stock JS effects are inserted at
    default settings as starting points, and track/project notes record
    the original MPC effect names.
"""
import argparse
import gzip
import json
import struct
import sys
from pathlib import Path

PPQ = 960
GAP_BARS = 1
CHOKE_FADE = 0.005
PARAM_LEVEL = 518
PARAM_PAN = 1044
PARAM_SKIP = {
    131: "pad aftertouch (not routed to any target in this program)",
}
# audioRoute destinations observed in MPC Sample projects
DEST_SUBMIX = 1
DEST_MAIN = 2


def load_project(path):
    raw = gzip.decompress(path.read_bytes())
    # ACVS container: 5 header lines, then JSON
    json_start = 0
    for _ in range(5):
        json_start = raw.index(b"\n", json_start) + 1
    return json.loads(raw[json_start:])["data"]


def wav_duration(path):
    """Return duration in seconds by walking RIFF chunks (handles float WAVs)."""
    with open(path, "rb") as f:
        riff = f.read(12)
        assert riff[:4] == b"RIFF" and riff[8:12] == b"WAVE", path
        rate = None
        block_align = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"fmt ":
                fmt = f.read(size)
                rate = struct.unpack("<I", fmt[4:8])[0]
                block_align = struct.unpack("<H", fmt[12:14])[0]
            elif cid == b"data":
                return size / block_align / rate
            else:
                f.seek(size + (size & 1), 1)
    raise ValueError(f"no data chunk in {path}")


def fader_gain(v):
    return 2.0 * v * v


def esc(name):
    return name.replace('"', "'")


def env_value0(env, key, default=0.0):
    x = env.get(key, default)
    return x.get("value0", default) if isinstance(x, dict) else x


def collect_insert_names(mixable):
    names = []
    for eff in mixable.get("inserts", {}).get("effects", []):
        desc = eff.get("plugin", {}).get("description", {})
        if desc.get("name"):
            names.append(desc["name"])
    return names


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("xpj", help="MPC .xpj project file (expects the "
                    "<name>_[ProjectData] sample folder alongside it)")
    ap.add_argument("--with-effects", action="store_true",
                    help="recreate submix/master FX topology with stock JS "
                         "placeholders (MPC plugin settings are not portable)")
    args = ap.parse_args()

    xpj = Path(args.xpj).resolve()
    root = xpj.parent
    stem = xpj.stem
    data_dir = f"{stem}_[ProjectData]"
    out = root / f"{stem}.rpp"

    d = load_project(xpj)
    track = d["tracks"][0]
    instruments = track["program"]["drum"]["instruments"]

    # pad index -> sample/pad info (only pads with a sample layer)
    pads = {}
    for idx, inst in enumerate(instruments):
        layer = inst["layersv"][0]
        if not layer["sampleFile"]:
            continue
        amp = inst["synthSection"].get("ampEnvelope", {})
        pads[idx] = {
            "name": layer["sampleName"],
            "file": f"{data_dir}/{layer['sampleFile']}",
            "dur": wav_duration(root / data_dir / layer["sampleFile"]),
            "vol": fader_gain(inst["mixable"]["volume"]),
            "pan": (inst["mixable"]["pan"] - 0.5) * 2.0,
            "pitch_semis": layer["pitch"],
            "mono": inst["monophonic"] and inst["polyphony"] <= 1,
            "mute_group": inst["whichMuteGroup"],
            "attack": env_value0(amp, "Attack"),
            "decay": env_value0(amp, "Decay", 1.0),
            "decay_from_end": env_value0(amp, "DecayFromEnd", False),
            "route": inst["mixable"]["audioRoute"]["destination"],
        }

    # collect note + automation events per sequence, unrolling looped clips
    sequences = []
    skipped_params = {}
    for s in sorted(d["sequences"], key=lambda x: x["key"]):
        v = s["value"]
        notes = []
        autom = []  # (pulses, pad, param, value)
        for lane in v["trackClipMaps"]:
            for pair in lane:
                clip = pair["value"]
                if not isinstance(clip, dict):
                    continue
                events = clip.get("eventList", {}).get("events", [])
                if not events:
                    continue
                start = clip["startPulses"]
                end = clip["endPulses"]
                loop_len = clip["loopEndPulses"] - clip["loopStartPulses"]
                reps = 1
                if clip["loop"] and loop_len > 0:
                    reps = max(1, -(-(end - start) // loop_len))
                for e in events:
                    if e["muted"]:
                        continue
                    times = []
                    for r in range(reps):
                        t = start + e["time"] + r * loop_len
                        if t >= end:
                            break
                        times.append(t)
                    if e["type"] == 3:
                        n = e["note"]
                        tune = 0.0
                        if (n.get("modifierActiveState0")
                                and n.get("EnumCerealisationWrapper(selectedModifierType)")
                                == "Tuning (coarse)"):
                            tune = round((n["modifierValue0"] - 0.5) * 240.0)
                        for t in times:
                            notes.append({
                                "pulses": t,
                                "pad": n["note"] - 36,
                                "vel": n["velocity"],
                                "tune": tune,
                            })
                    elif e["type"] in (1, 2) and not e["invented"]:
                        a = e["automation"]
                        pad, param = a["note"] - 36, a["parameter"]
                        if param in (PARAM_LEVEL, PARAM_PAN):
                            for t in times:
                                autom.append((t, pad, param, a["value"]))
                        else:
                            key = (param, pad)
                            skipped_params[key] = skipped_params.get(key, 0) + 1
        if notes:
            sequences.append({
                "key": s["key"],
                "name": v["name"],
                "bpm": v["bpm"],
                "pulses": v["lengthPulses"],
                "notes": notes,
                "autom": autom,
            })

    # timeline: back-to-back regions with a one-bar gap, tempo per region
    cursor = 0.0
    tempo_points = []
    regions = []
    for seq in sequences:
        spb = 60.0 / seq["bpm"]
        seq["start_sec"] = cursor
        seq["spp"] = spb / PPQ
        length_sec = seq["pulses"] / PPQ * spb
        tempo_points.append((cursor, seq["bpm"]))
        regions.append((cursor, cursor + length_sec, f"{seq['key'] + 1:02d} {seq['name']}"))
        cursor += length_sec + GAP_BARS * 4 * spb

    used_pads = sorted({n["pad"] for seq in sequences for n in seq["notes"]})
    missing = [p for p in used_pads if p not in pads]
    if missing:
        sys.exit(f"notes reference pads with no sample: {missing}")

    # flatten items and automation to absolute seconds
    items = {p: [] for p in used_pads}
    envs = {}  # (pad, param) -> [(sec, value)]
    for seq in sequences:
        for n in seq["notes"]:
            items[n["pad"]].append({
                "pos": seq["start_sec"] + n["pulses"] * seq["spp"],
                "vel": n["vel"],
                "tune": n["tune"],
            })
        for t, pad, param, value in seq["autom"]:
            envs.setdefault((pad, param), []).append(
                (seq["start_sec"] + t * seq["spp"], value))
    # drop pan envelopes that never leave centre
    for key in [k for k, pts in envs.items()
                if k[1] == PARAM_PAN and all(abs(v - 0.5) < 1e-6 for _, v in pts)]:
        del envs[key]

    for (param, pad), count in sorted(skipped_params.items()):
        why = PARAM_SKIP.get(param, "unknown parameter")
        print(f"note: skipped {count} automation events on pad {pad + 1} "
              f"(param {param}: {why})")

    # choke: trim items at the next hit of the same mono pad or mute group
    all_hits = sorted((it["pos"], pad) for pad in used_pads for it in items[pad])
    group_hits = {}
    for pos, pad in all_hits:
        g = pads[pad]["mute_group"]
        if g:
            group_hits.setdefault(g, []).append(pos)

    def next_hit(times, t):
        for x in times:
            if x > t + 1e-6:
                return x
        return None

    n_choked = 0
    for pad in used_pads:
        p = pads[pad]
        own = [it["pos"] for it in sorted(items[pad], key=lambda x: x["pos"])]
        for it in items[pad]:
            rate = 2.0 ** ((p["pitch_semis"] + it["tune"]) / 12.0)
            natural = p["dur"] / rate
            cut = None
            if p["mono"]:
                cut = next_hit(own, it["pos"])
            if p["mute_group"]:
                gcut = next_hit(group_hits[p["mute_group"]], it["pos"])
                cut = gcut if cut is None else min(cut, gcut)
            length = natural
            choked = cut is not None and cut - it["pos"] < natural
            if choked:
                length = cut - it["pos"]
                n_choked += 1
            # amp envelope -> fades (normalised times approximated as a
            # fraction of the sample length)
            fadein = min(p["attack"] * natural, length)
            fadeout = CHOKE_FADE if choked else (
                p["decay"] * natural if p["decay_from_end"] else 0.0)
            fadeout = min(fadeout, length)
            if fadein + fadeout > length and fadein + fadeout > 0:
                scale = length / (fadein + fadeout)
                fadein *= scale
                fadeout *= scale
            it.update(rate=rate, len=length, fadein=fadein, fadeout=fadeout)

    # effects topology (only meaningful with --with-effects)
    submix_pads = [p for p in used_pads if pads[p]["route"] == DEST_SUBMIX]
    submix = d["mixer"]["submixes"][0] if d["mixer"]["submixes"] else None
    submix_fx = collect_insert_names(submix["mixable"]) if submix else []
    main_fx = collect_insert_names(d["mixer"]["outputs"][0]["mixable"])

    lines = []
    w = lines.append

    def js_fxchain(indent, js_effects):
        pad_ = " " * indent
        w(f"{pad_}<FXCHAIN")
        w(f"{pad_}  SHOW 0")
        w(f"{pad_}  LASTSEL 0")
        w(f"{pad_}  DOCKED 0")
        for js in js_effects:
            w(f"{pad_}  BYPASS 0 0 0")
            w(f'{pad_}  <JS {js} ""')
            w(f"{pad_}    - - - - -")
            w(f"{pad_}  >")
            w(f"{pad_}  WAK 0 0")
        w(f"{pad_}>")

    w('<REAPER_PROJECT 0.1 "7.0" 0')
    w(f"  TEMPO {sequences[0]['bpm']:g} 4 4")
    master_vol = fader_gain(d["mixer"]["volume"])
    w(f"  MASTER_VOLUME {master_vol:.6f} 0 -1 -1 1")
    if args.with_effects:
        skipped_fx = [n for n in main_fx if "compressor" not in n.lower()]
        proj_notes = ["Converted from MPC project: " + stem]
        for p in submix_pads:
            proj_notes.append(f"'{pads[p]['name']}' was routed to the MPC "
                              "submix (receive on the submix bus track)")
        if submix_fx and submix_pads:
            proj_notes.append("MPC submix inserts: " + ", ".join(submix_fx)
                              + " (settings not portable; stock JS delay at "
                              "defaults as a starting point)")
        comp = [n for n in main_fx if "compressor" in n.lower()]
        if comp:
            proj_notes.append("MPC main-output compressor: " + ", ".join(comp)
                              + " (stock JS compressor at defaults on master)")
        if skipped_fx:
            proj_notes.append(
                "MPC main-output FX not ported (performance-triggered): "
                + ", ".join(skipped_fx))
        # NOTES is only valid at project level; REAPER writes it as "NOTES 0 2"
        w("  <NOTES 0 2")
        for t in proj_notes:
            w(f"    |{esc(t)}")
        w("  >")
        if comp:
            # stock JS compressor at defaults; MPC state is not portable
            w("  <MASTERFXLIST")
            w("    SHOW 0")
            w("    LASTSEL 0")
            w("    DOCKED 0")
            w("    BYPASS 0 0 0")
            w('    <JS sstillwell/majortom ""')
            w("      - - - - -")
            w("    >")
            w("    WAK 0 0")
            w("  >")
    w("  <TEMPOENVEX")
    w("    ACT 1 -1")
    w("    VIS 1 0 1")
    w("    ARM 0")
    w("    DEFSHAPE 1 -1 -1")
    for pos, bpm in tempo_points:
        w(f"    PT {pos:.9f} {bpm:g} 1")
    w("  >")
    for i, (start, end, name) in enumerate(regions, 1):
        w(f'  MARKER {i} {start:.9f} "{esc(name)}" 1 0 1')
        w(f'  MARKER {i} {end:.9f} "" 1')

    for pad in used_pads:
        p = pads[pad]
        vol_pts = envs.get((pad, PARAM_LEVEL))
        pan_pts = envs.get((pad, PARAM_PAN))
        to_submix = args.with_effects and pad in submix_pads
        w("  <TRACK")
        w(f'    NAME "{esc(p["name"])}"')
        # with a volume envelope the envelope carries the level (fader
        # becomes trim in REAPER's default trim/read mode)
        fader = 1.0 if vol_pts else p["vol"]
        w(f"    VOLPAN {fader:.6f} {p['pan']:.6f} -1 -1 1")
        if to_submix:
            w("    MAINSEND 0 0")
        if vol_pts:
            w("    <VOLENV2")
            w("      ACT 1 -1")
            w("      VIS 1 1 1")
            w("      ARM 0")
            w("      DEFSHAPE 0 -1 -1")
            w(f"      PT 0.000000000 {p['vol']:.6f} 0")
            for t, v in vol_pts:
                w(f"      PT {t:.9f} {fader_gain(v):.6f} 0")
            w("    >")
        if pan_pts:
            w("    <PANENV2")
            w("      ACT 1 -1")
            w("      VIS 1 1 1")
            w("      ARM 0")
            w("      DEFSHAPE 0 -1 -1")
            w(f"      PT 0.000000000 {p['pan']:.6f} 0")
            for t, v in pan_pts:
                w(f"      PT {t:.9f} {(v - 0.5) * 2.0:.6f} 0")
            w("    >")
        for it in sorted(items[pad], key=lambda x: x["pos"]):
            w("    <ITEM")
            w(f"      POSITION {it['pos']:.9f}")
            w(f"      LENGTH {it['len']:.9f}")
            w(f"      VOLPAN {it['vel']:.6f} 0 1 -1")
            w("      SOFFS 0")
            if it["fadein"] > 0:
                w(f"      FADEIN 0 {it['fadein']:.9f} 0 0 0 0 0")
            if it["fadeout"] > 0:
                w(f"      FADEOUT 0 {it['fadeout']:.9f} 0 0 0 0 0")
            if it["rate"] != 1.0:
                w(f"      PLAYRATE {it['rate']:.9f} 0 0 -1 0 0.0025")
            else:
                w("      PLAYRATE 1 1 0 -1 0 0.0025")
            name = p["name"] if not it["tune"] else f"{p['name']} {it['tune']:+g}st"
            w(f'      NAME "{esc(name)}"')
            w("      <SOURCE WAVE")
            w(f'        FILE "{p["file"]}"')
            w("      >")
            w("    >")
        w("  >")

    if args.with_effects and submix_pads and submix:
        sub_vol = fader_gain(submix["mixable"]["volume"])
        sub_name = submix.get("name", "Submix")
        if submix_fx:
            sub_name += f" ({', '.join(submix_fx)})"
        w("  <TRACK")
        w(f'    NAME "{esc(sub_name)}"')
        w(f"    VOLPAN {sub_vol:.6f} 0 -1 -1 1")
        for p in submix_pads:
            w(f"    AUXRECV {used_pads.index(p)} 0 1 0 0 0 0 0 0 -1:U 0 -1 ''")
        if any("delay" in n.lower() for n in submix_fx):
            js_fxchain(4, ["delay/delay"])
        w("  >")
    w(">")

    out.write_text("\n".join(lines) + "\n")
    n_items = sum(len(v) for v in items.values())
    n_env = len(envs)
    print(f"wrote {out.name}: {len(used_pads)} tracks, {n_items} items "
          f"({n_choked} choked), {len(regions)} regions, {n_env} envelopes, "
          f"effects={'on' if args.with_effects else 'off'}, {cursor:.1f}s total")
    for start, end, name in regions:
        print(f"  region {name!r}: {start:.2f}s - {end:.2f}s")


if __name__ == "__main__":
    main()
