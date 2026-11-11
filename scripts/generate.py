#!/usr/bin/env python3
"""Generate sound effects with ElevenLabs (text to sound, eleven_text_to_sound_v2) and file them as a library.

Every generated take is saved as a 48 kHz WAV in the generated library (one folder for all films, outside the skill),
with a row in its catalog.json (role, prompt, length, cost). The library is registered in config.json as
`elevenlabs` (tier main) and indexed by build_index.py, so search.py, the picker, from_cues.py, mix.py, bed.py and
the critique treat a generated take like any recorded sound. A take is keyed by (model, prompt, length, influence,
loop, take number): asking for the same take again reuses the file and spends nothing.

  python generate.py check
  python generate.py kit EVENTS.json|SHEET.json [--per-kind 3] [--bed] [--dry] [--yes]
  python generate.py prompt "dry paper page turn, close" --role paper [--dur 0.8] [--takes 2] [--yes]
  python generate.py sheet SHEET.json [--dry] [--yes]          # cues with "gen" get their file
  python generate.py import work/gen_jobs.json                 # connector route: file downloaded takes
  python generate.py list [--role whoosh]

kit       reads an event list (the from_cues input: [{"t":..,"type":"whoosh"}, ...]) or a cue sheet, and generates a
          few takes for every kind/role it needs that the generated library does not have yet. Run it BEFORE
          from_cues.py: from_cues only routes a kind to the library when the library holds candidates for it.
          Takes per kind: --per-kind (default 3, two for a kind that occurs once; typing gets 2 longer takes).
          --bed adds two long steady loops for bed.py.
sheet     a cue with "gen" is generated and gets "file" (take 1) and "gen_takes" (all takes):
            {"t": 2.0, "role": "paper", "gen": "dry paper page turn, close, single flip"}
            {"t": 5.1, "role": "hit", "gen": {"prompt": "...", "dur": 1.2, "influence": 0.6, "takes": 3}}
          Use it for the moments that deserve their own sound (the hook, the logo, a named object); the kit covers
          the bulk.
Backends  api        ELEVENLABS_API_KEY in the environment (or the variable named by config elevenlabs.api_key_env):
                     POST /v1/sound-generation, straight from this script.
          connector  no key: the jobs are written to work/gen_jobs.json (prompt, length, file to save to) for the
                     ElevenLabs connector (creative_generate_in_flow, node_type sfx); after the downloads,
                     `generate.py import JOBS` files them and the same kit/sheet command runs again from cache.
          --backend api|connector forces one. Nothing is spent without --yes (the plan is printed first);
          --max N caps the calls of one run (default 24).
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import SKILL_DIR, config_path, index_path, load_config, read_text_any, tag_roles

MODEL = "eleven_text_to_sound_v2"
API_URL = os.environ.get("REAL_SFX_GEN_API_URL") or "https://api.elevenlabs.io/v1/sound-generation"   # env: tests
LIB_NAME = "elevenlabs"
TAIL = "isolated sound effect, no music, no voices"
MIN_DUR, MAX_DUR = 0.5, 30.0

# kind or role -> (prompt, variants that keep the takes apart, length in s, prompt influence)
# Physical source, attack, length, tail and distance: the model follows concrete words, not "cinematic".
PROMPTS = {
    "whoosh": ("fast whoosh past the microphone, {v}, clean attack and quick tail",
               ["air rushing", "soft fabric swish", "smooth airy sweep", "short tight swipe"], 0.8, 0.5),
    "wipe": ("transition whoosh, {v}, smooth swell into a soft landing",
             ["wide airy sweep", "deep fabric swoosh", "filtered air pass"], 1.0, 0.5),
    "hit": ("short punchy impact, {v}, tight attack, short tail",
            ["deep wooden thud", "low drum-like body hit", "muffled heavy door slam", "dense leather punch"], 1.0, 0.5),
    "thump": ("dull low thump, {v}, soft attack, no ring",
              ["heavy object on a wooden floor", "fist on a padded table", "low muffled knock"], 0.6, 0.6),
    "drop": ("deep sub bass drop, {v}, clean low end, fades out", ["slow pitch fall", "short boom and decay"], 1.8, 0.5),
    "sub_drop": ("deep sub bass drop, {v}, clean low end, fades out", ["slow pitch fall", "short boom and decay"], 1.8, 0.5),
    "riser": ("tension riser, {v}, builds steadily and ends at its peak",
              ["airy noise swell", "rising tonal shimmer", "reversed air sweep"], 2.0, 0.5),
    "click": ("single short click, {v}, dry, close microphone, one click only",
              ["small plastic button", "soft mouse click", "light metal switch"], 0.5, 0.7),
    "tick": ("single tiny tick, {v}, bright, dry, one tick only", ["clock tick", "light plastic tap"], 0.5, 0.7),
    "tock": ("single soft tock, {v}, dry, one hit only", ["wooden block knock", "low plastic tap"], 0.5, 0.7),
    "pop": ("single soft pop, {v}, dry, close, one pop only",
            ["round bubble pop", "small cork plop", "small water-like blip"], 0.5, 0.7),
    "chime": ("soft notification chime, {v}, clean, short decay",
              ["two glassy notes", "small bell ding", "warm marimba note"], 1.2, 0.6),
    "success": ("gentle success chime, {v}, rising two notes, short decay", ["glassy", "soft bell"], 1.2, 0.6),
    "magic": ("soft sparkle shimmer, {v}, short", ["glittering twinkle", "airy glass shimmer"], 1.5, 0.5),
    "water": ("single water drop, {v}, dry, close", ["into a glass", "small droplet plip"], 0.5, 0.6),
    "coins": ("coins dropped on a counter, {v}", ["a few metal coins", "short cash register ding"], 1.2, 0.6),
    "coin": ("coins dropped on a counter, {v}", ["a few metal coins", "short cash register ding"], 1.2, 0.6),
    "glitch": ("short digital glitch, {v}, stuttering, dry", ["data stutter", "crackling static burst"], 0.6, 0.6),
    "error": ("short soft error buzz, {v}, dry", ["low double beep", "dull denied tone"], 0.7, 0.6),
    "camera": ("camera shutter click, {v}, close", ["mechanical film camera", "dry digital shutter"], 0.6, 0.7),
    "paper": ("paper sound, {v}, dry, close microphone", ["single page turn", "card slid on a table",
                                                         "sheet of paper flick"], 0.8, 0.6),
    "glass": ("glass shattering, {v}, short", ["small glass break", "crystal crack and scatter"], 1.2, 0.5),
    "shatter": ("glass shattering, {v}, short", ["small glass break", "crystal crack and scatter"], 1.2, 0.5),
    "data": ("soft digital data processing chirps, {v}, short", ["quick computer blips", "scanning beeps"], 1.0, 0.5),
    "typing": ("typing on a keyboard, {v}, steady rhythm, close microphone, dry room",
               ["mechanical keyboard", "quiet laptop keyboard"], 4.0, 0.6),
    "key": ("typing on a keyboard, {v}, steady rhythm, close microphone, dry room",
            ["mechanical keyboard", "quiet laptop keyboard"], 4.0, 0.6),
    "foley": ("{v}, close microphone, dry", ["object set down on a table", "cloth movement"], 1.0, 0.5),
    "ambience": ("quiet dark steady room tone, {v}, continuous, no events, no birds",
                 ["low air conditioning hum", "distant soft wind", "empty hall air"], 20.0, 0.4),
    "ui": ("soft interface sound, {v}, dry, short", ["gentle tap", "small confirmation blip"], 0.6, 0.6),
}
KIND_ROLE = {"wipe": "whoosh", "thump": "hit", "drop": "sub_drop", "tick": "click", "tock": "click",
             "success": "chime", "magic": "chime", "coins": "coin", "shatter": "glass", "key": "typing"}
# a pack type build_index.py turns into the right roles (common.PACK_TYPE_ROLES)
ROLE_TYPE = {"hit": "impact"}
TONE_WORDS = {"dark": "low, dark, warm", "bright": "bright, light, crisp"}


# ------------------------------------------------------------------ places and config
def gen_cfg(cfg):
    return cfg.get("elevenlabs") or {}


def gen_dir(cfg):
    env = os.environ.get("REAL_SFX_GEN_DIR")
    d = env or gen_cfg(cfg).get("dir") or str(Path.home() / ".real-sfx" / "elevenlabs")
    return Path(os.path.expanduser(d))


def api_key(cfg):
    var = gen_cfg(cfg).get("api_key_env") or "ELEVENLABS_API_KEY"
    return os.environ.get(var, "").strip()


def ensure_library(cfg):
    """Register the generated folder as library `elevenlabs` (tier main) in config.json once. Returns cfg."""
    d = gen_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    libs = cfg.setdefault("libraries", [])
    lib = next((l for l in libs if l.get("name") == LIB_NAME), None)
    if lib and Path(lib["path"]) == d:
        return cfg
    if lib:
        lib["path"] = str(d)
    else:
        libs.append({"name": LIB_NAME, "path": str(d), "tier": "main", "generated": True})
    config_path().write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"config: library '{LIB_NAME}' -> {d}")
    return cfg


def catalog_path(cfg):
    return gen_dir(cfg) / "catalog.json"


def load_catalog(cfg):
    p = catalog_path(cfg)
    if not p.exists():
        return []
    d = json.loads(read_text_any(p))
    return d.get("sounds", []) if isinstance(d, dict) else d


def save_catalog(cfg, rows):
    p = catalog_path(cfg)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"generator": "real-sfx generate.py", "sounds": rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(p)


# ------------------------------------------------------------------ jobs
def slug(text, n=40):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "sfx"


def clamp_dur(d):
    return None if d is None else round(max(MIN_DUR, min(MAX_DUR, float(d))), 2)


def make_job(role, prompt, dur=None, influence=0.5, loop=False, take=1, flavor=""):
    text = prompt.strip().rstrip(".")
    if flavor and flavor.lower() not in text.lower():
        text += f", {flavor}"
    if "no music" not in text.lower():
        text += f", {TAIL}"
    dur = clamp_dur(dur)
    influence = round(max(0.0, min(1.0, float(influence))), 2)
    key = hashlib.sha1(json.dumps([MODEL, text, dur, influence, bool(loop), int(take)]).encode()).hexdigest()[:12]
    return {"key": key, "role": role, "prompt": text, "duration": dur, "influence": influence, "loop": bool(loop),
            "take": int(take), "file": f"{role}/{slug(prompt)}_{key}.wav"}


def from_template(name, take, dur=None, tone=None, flavor="", loop=False):
    tmpl, variants, d0, infl = PROMPTS[name]
    v = variants[(take - 1) % len(variants)]
    text = tmpl.format(v=v)
    if tone in TONE_WORDS:
        text = f"{TONE_WORDS[tone]} {text}"
    role = KIND_ROLE.get(name, name)
    want = dur if dur is not None else d0
    return make_job(role, text, want, infl, loop, take, flavor)


def cached(cfg, rows, job):
    for r in rows:
        if r.get("key") == job["key"] and (gen_dir(cfg) / r["file"]).exists():
            return r
    return None


# ------------------------------------------------------------------ backends
def to_wav(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ar", "48000", "-c:a", "pcm_s24le", str(dst)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg could not convert {src}: {r.stderr.strip()[:300]}")


def call_api(key, job, out_mp3):
    body = {"text": job["prompt"], "model_id": MODEL, "prompt_influence": job["influence"]}
    if job["duration"] is not None:
        body["duration_seconds"] = job["duration"]
    if job["loop"]:
        body["loop"] = True
    req = urllib.request.Request(API_URL + "?output_format=mp3_44100_128", data=json.dumps(body).encode(),
                                 headers={"xi-api-key": key, "Content-Type": "application/json",
                                          "Accept": "audio/mpeg"}, method="POST")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                out_mp3.write_bytes(resp.read())
                return resp.headers.get("character-cost")
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 and attempt < 2:          # rate limit: wait and try once more (not charged)
                time.sleep(4 * (attempt + 1))
                continue
            hint = {401: "the API key is wrong or has no sound-effects permission",
                    402: "no credits left", 422: "a parameter is out of range"}.get(e.code, "")
            raise RuntimeError(f"ElevenLabs HTTP {e.code} {hint}: {msg}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"cannot reach ElevenLabs ({e.reason}); in a sandbox without network use "
                               f"--backend connector")
    raise RuntimeError("ElevenLabs kept answering 429")


def fake_take(job, out_wav):
    """Test fixture only (REAL_SFX_GEN_FAKE=1): a noise burst shaped by role. Never used for a film."""
    d = job["duration"] or 1.0
    att = 0.4 if job["role"] in ("riser", "ambience") else 0.005
    tone = 200 + int(job["key"][:3], 16) % 1800
    expr = (f"(0.6*sin(2*PI*{tone}*t)+0.4*(random(0)*2-1))*min(t/{att},1)*exp(-t*{1.5 if att > 0.1 else 9})"
            if job["role"] != "ambience" else "0.05*(random(0)*2-1)")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d={d}",
                    "-ac", "2", "-c:a", "pcm_s24le", str(out_wav)], check=True)


def record(cfg, rows, job, cost, backend):
    row = {"file": job["file"], "type": ROLE_TYPE.get(job["role"], job["role"]), "role": job["role"],
           # a neutral source name: the index corrects pack types by words of the source ("thud" would make a
           # hit foley), and the prompt is kept in its own field (generate.py list shows it)
           "source": f"elevenlabs/{job['role']}/{job['role']} take {job['take']} {job['key'][:6]}",
           "prompt": job["prompt"], "duration_s": job["duration"], "influence": job["influence"],
           "loop": job["loop"], "take": job["take"], "model": MODEL, "key": job["key"], "backend": backend,
           "cost": float(cost) if cost not in (None, "") else None, "created": time.strftime("%Y-%m-%d %H:%M")}
    rows[:] = [r for r in rows if r.get("key") != job["key"]] + [row]
    return row


def backend_for(cfg, forced):
    if os.environ.get("REAL_SFX_GEN_FAKE") == "1":
        return "fake"
    if forced:
        return forced
    return "api" if api_key(cfg) else "connector"


def run_jobs(cfg, jobs, a, jobs_out):
    """Generate what is not cached. Returns (rows by key for every job that has a file, pending jobs)."""
    rows = load_catalog(cfg)
    have, todo = {}, []
    for j in jobs:
        r = cached(cfg, rows, j)
        if r:
            have[j["key"]] = r
        elif j["key"] not in {t["key"] for t in todo}:
            todo.append(j)
    backend = backend_for(cfg, a.backend)
    secs = sum(j["duration"] or 2.0 for j in todo)
    costs = [r["cost"] for r in rows if r.get("cost")]
    per = f", about {statistics.median(costs):.0f} credits per call so far" if costs else ""
    print(f"plan: {len(jobs)} take(s), {len(have)} already generated, {len(todo)} to generate "
          f"({secs:.0f} s of audio{per}) via {backend}")
    for j in todo[:40]:
        print(f"  {j['role']:9s} {(str(j['duration']) + ' s') if j['duration'] else 'auto':>7s}  {j['prompt']}")
    if len(todo) > 40:
        print(f"  ... {len(todo) - 40} more")
    if not todo or a.dry:
        return have, todo
    if len(todo) > a.max:
        sys.exit(f"{len(todo)} calls is more than --max {a.max}: lower --per-kind, or raise --max on purpose")
    if backend == "connector":
        write_jobs(cfg, todo, jobs_out)
        return have, todo
    if not a.yes:
        print("nothing spent: run again with --yes to generate")
        return have, todo
    cfg = ensure_library(cfg)
    key = api_key(cfg)
    if backend == "api" and not key:
        sys.exit("no ELEVENLABS_API_KEY in the environment (or use --backend connector)")
    done = 0
    for j in todo:
        dst = gen_dir(cfg) / j["file"]
        try:
            if backend == "fake":
                fake_take(j, dst)
                cost = None
            else:
                with tempfile.TemporaryDirectory() as td:
                    mp3 = Path(td) / "take.mp3"
                    cost = call_api(key, j, mp3)
                    to_wav(mp3, dst)
        except RuntimeError as e:
            print(f"FAIL {j['role']} '{j['prompt'][:60]}': {e}", file=sys.stderr)
            if "HTTP 401" in str(e) or "HTTP 402" in str(e) or "cannot reach" in str(e):
                break
            continue
        have[j["key"]] = record(cfg, rows, j, cost, backend)
        save_catalog(cfg, rows)                          # after every take: a crash keeps what was paid for
        done += 1
        print(f"  ok {j['role']:9s} {dst.name}" + (f"  ({cost} credits)" if cost else ""))
    if done:
        reindex()
    return have, [j for j in todo if j["key"] not in have]


def write_jobs(cfg, todo, out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    d = gen_dir(cfg)
    payload = {"dir": str(d), "model_id": MODEL, "node_type": "sfx", "jobs": []}
    for j in todo:
        hint = f", about {j['duration']:g} seconds long" if j["duration"] else ""
        payload["jobs"].append(dict(j, connector_prompt=j["prompt"] + hint,
                                    save_as=str((d / "_incoming" / f"{j['key']}.mp3").resolve())))
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nconnector route: {len(todo)} job(s) -> {out}\n"
          f"  for each job: creative_generate_in_flow(node_type='sfx', model_id='{MODEL}', "
          f"prompt=connector_prompt, generations_count=1, one flow_id for all), poll creative_get_flow_run_status, "
          f"download the url to save_as (URLs expire in about 2 h)\n"
          f"  then: python scripts/generate.py import {out}  and run this command again")


def reindex():
    r = subprocess.run([sys.executable, str(Path(__file__).parent / "build_index.py")], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    last = (r.stdout.strip().splitlines() or [""])[-1]
    print(f"index: {last}" if r.returncode == 0 else f"WARN build_index failed: {r.stderr.strip()[-300:]}")


# ------------------------------------------------------------------ what a film needs
def load_any(path):
    if path == "-":
        return json.loads(sys.stdin.read().lstrip("﻿"))
    return json.loads(read_text_any(Path(path)))


def needs_from(data):
    """{(template name, tone): [lengths]} from an event list or a cue sheet."""
    import from_cues as fc
    out = {}
    if isinstance(data, dict) and "cues" in data:
        cues = []
        for c in data["cues"]:
            cues.append(c)
            cues += [dict(l, role=l.get("role") or c.get("role"), pick=l.get("pick") or c.get("pick"))
                     for l in c.get("layers") or [] if not any(k in l for k in ("file", "id", "uisfx"))]
        for c in cues:
            if any(k in c for k in ("file", "id", "uisfx", "gen")) or c.get("bed"):
                continue
            spec = c.get("pick") or {}
            name = spec.get("role") or c.get("role")
            if name not in PROMPTS:
                continue
            tone = "dark" if spec.get("dark") else "bright" if spec.get("bright") else None
            out.setdefault((name, tone), []).append(spec.get("target_dur") or c.get("fit_dur")
                                                    or spec.get("max_dur"))
        return out
    events = data.get("events", data) if isinstance(data, dict) else data
    for e in events:
        kind = str(e.get("type") or e.get("kind") or "").lower().replace("-", "_").replace(" ", "_")
        kind = fc.CANON.get(kind, kind)
        if kind not in PROMPTS:
            print(f"  skipped kind '{kind}': no prompt for it (write a gen cue for it by hand)")
            continue
        route = (fc.ROUTES.get(kind) or {}).get("specs") or [{}]
        spec = route[0]
        tone = "dark" if spec.get("dark") else "bright" if spec.get("bright") else None
        dur = e.get("len") if kind not in ("key", "typing") else None
        if dur is None and spec.get("target_dur"):
            dur = spec["target_dur"]
        out.setdefault((kind, tone), []).append(dur)
    return out


def kit_jobs(needs, per_kind, flavor, bed):
    jobs = []
    for (name, tone), lens in sorted(needs.items(), key=lambda x: x[0][0]):
        n = len(lens)
        if name in ("key", "typing"):
            takes = 2                                    # long takes: from_cues cuts the single keystrokes
        elif name in ("click", "tick", "tock"):
            takes = max(3, per_kind)                     # from_cues wants 3 single clicks before it leaves uisfx
        else:
            takes = 2 if n == 1 else per_kind
        known = [x for x in lens if x]
        d0 = PROMPTS[name][2]
        dur = d0
        if known and name not in ("key", "typing", "ambience"):
            dur = max(d0 * 0.6, min(d0 * 1.6, statistics.median(known) * 1.2))   # headroom: mix.py trims, not stretches
        for t in range(1, takes + 1):
            jobs.append(from_template(name, t, dur, tone, flavor))
    if bed:
        for t in (1, 2):
            jobs.append(from_template("ambience", t, 20.0, None, "", loop=True))
    return jobs


# ------------------------------------------------------------------ commands
def cmd_check(cfg, a):
    d = gen_dir(cfg)
    rows = load_catalog(cfg)
    lib = next((l for l in cfg.get("libraries", []) if l.get("name") == LIB_NAME), None)
    print(f"generated library  {d}  ({'exists' if d.exists() else 'not yet created'})")
    print(f"config             {'registered' if lib else 'not registered yet (the first generation does it)'}")
    print(f"takes              {len(rows)}" + (f" in roles {', '.join(sorted({r['role'] for r in rows}))}" if rows else ""))
    spent = sum(r.get("cost") or 0 for r in rows)
    if spent:
        print(f"credits recorded   {spent:.0f}")
    var = gen_cfg(cfg).get("api_key_env") or "ELEVENLABS_API_KEY"
    print(f"api key            {'set ($' + var + ')' if api_key(cfg) else 'not set ($' + var + ') -> connector route'}")
    print(f"backend            {backend_for(cfg, a.backend)}")
    flav = gen_cfg(cfg).get("flavor")
    print(f"flavor             {flav or '(none: config elevenlabs.flavor adds words to every prompt)'}")


def cmd_kit(cfg, a):
    needs = needs_from(load_any(a.src))
    if not needs and not a.bed:
        sys.exit("nothing to generate: no event kind or cue role this script has a prompt for")
    flavor = a.flavor if a.flavor is not None else gen_cfg(cfg).get("flavor", "")
    print("needs: " + ", ".join(f"{k}{'/' + t if t else ''} x{len(v)}" for (k, t), v in sorted(needs.items(),
                                                                                              key=lambda x: x[0][0])))
    have, pending = run_jobs(cfg, kit_jobs(needs, a.per_kind, flavor, a.bed), a, a.jobs_out)
    if not pending and have and not a.dry:
        print("kit ready: run from_cues.py with --ui library (generated sounds instead of uisfx), then mix.py")


def norm_gen(c):
    g = c["gen"]
    g = {"prompt": g} if isinstance(g, str) else dict(g)
    if not g.get("prompt"):
        sys.exit(f"cue at {c.get('t')}s: gen needs a prompt")
    return g


def gen_cues(sheet):
    out = []
    for c in sheet.get("cues", []):
        if "gen" in c:
            out.append(c)
        for l in c.get("layers") or []:
            if "gen" in l:
                l.setdefault("role", c.get("role"))
                out.append(l)
    return out


def cmd_sheet(cfg, a):
    path = Path(a.sheet)
    sheet = json.loads(read_text_any(path))
    flavor = a.flavor if a.flavor is not None else gen_cfg(cfg).get("flavor", "")
    plan = []
    for c in gen_cues(sheet):
        g = norm_gen(c)
        role = c.get("role") or (c.get("pick") or {}).get("role") or next(iter(tag_roles(g["prompt"])), "foley")
        c["role"] = role
        jobs = [make_job(role, g["prompt"], g.get("dur", g.get("duration")), g.get("influence", 0.6),
                         g.get("loop", False), t, flavor) for t in range(1, int(g.get("takes", 1)) + 1)]
        plan.append((c, jobs))
    if not plan:
        sys.exit("no cue in the sheet has a \"gen\" field")
    have, pending = run_jobs(cfg, [j for _, js in plan for j in js], a, a.jobs_out)
    changed = 0
    for c, jobs in plan:
        files = [str(gen_dir(cfg) / have[j["key"]]["file"]) for j in jobs if j["key"] in have]
        if not files:
            continue
        if c.get("file") not in files:
            c["file"] = files[0]
        c["gen_takes"] = files
        c.pop("pick", None)
        c.pop("id", None)
        changed += 1
    if changed and not a.dry:
        path.write_text(json.dumps(sheet, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{changed} cue(s) got their generated file -> {path}  (other takes in gen_takes: "
              f"sheet.py set --at T file=...)")
    if pending:
        print(f"{len(pending)} take(s) still to generate")


def cmd_prompt(cfg, a):
    role = a.role or next(iter(tag_roles(a.text)), "foley")
    flavor = a.flavor if a.flavor is not None else gen_cfg(cfg).get("flavor", "")
    jobs = [make_job(role, a.text, a.dur, a.influence, a.loop, t, flavor) for t in range(1, a.takes + 1)]
    have, _ = run_jobs(cfg, jobs, a, a.jobs_out)
    for j in jobs:
        if j["key"] in have:
            print(gen_dir(cfg) / have[j["key"]]["file"])


def cmd_import(cfg, a):
    data = json.loads(read_text_any(Path(a.jobs)))
    cfg = ensure_library(cfg)
    rows = load_catalog(cfg)
    got, missing = 0, []
    for j in data.get("jobs", []):
        src = Path(j["save_as"])
        if not src.exists():
            missing.append(j)
            continue
        to_wav(src, gen_dir(cfg) / j["file"])
        record(cfg, rows, j, j.get("cost"), "connector")
        src.unlink()
        got += 1
    save_catalog(cfg, rows)
    print(f"imported {got} take(s)" + (f", {len(missing)} not downloaded yet" if missing else ""))
    if got:
        reindex()
    if missing:
        data["jobs"] = missing
        Path(a.jobs).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_list(cfg, a):
    rows = load_catalog(cfg)
    ids = {}
    ip = index_path(cfg)
    if ip.exists():
        for s in json.loads(ip.read_text(encoding="utf-8"))["sounds"]:
            ids[os.path.normcase(s["path"])] = s["id"]
    for r in sorted(rows, key=lambda r: (r["role"], r["created"])):
        if a.role and r["role"] != a.role:
            continue
        i = ids.get(os.path.normcase(str(gen_dir(cfg) / r["file"])), "-")
        print(f"{str(i):>5s}  {r['role']:9s} {r.get('duration_s') or 'auto'!s:>5s}  {r['prompt']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common_args(p):
        p.add_argument("--backend", choices=["api", "connector"])
        p.add_argument("--yes", action="store_true", help="spend credits (without it only the plan is printed)")
        p.add_argument("--dry", action="store_true")
        p.add_argument("--max", type=int, default=24, help="most calls in one run")
        p.add_argument("--flavor", help="words added to every prompt (default: config elevenlabs.flavor)")
        p.add_argument("--jobs-out", default="work/gen_jobs.json", help="connector route: where the jobs go")

    p = sub.add_parser("check")
    p.add_argument("--backend", choices=["api", "connector"])
    p = sub.add_parser("kit")
    p.add_argument("src", help="event list or cue sheet (- reads stdin)")
    p.add_argument("--per-kind", type=int, default=3)
    p.add_argument("--bed", action="store_true", help="also two long steady loops for bed.py")
    common_args(p)
    p = sub.add_parser("sheet")
    p.add_argument("sheet")
    common_args(p)
    p = sub.add_parser("prompt")
    p.add_argument("text")
    p.add_argument("--role")
    p.add_argument("--dur", type=float)
    p.add_argument("--takes", type=int, default=1)
    p.add_argument("--influence", type=float, default=0.6)
    p.add_argument("--loop", action="store_true")
    common_args(p)
    p = sub.add_parser("import")
    p.add_argument("jobs")
    p = sub.add_parser("list")
    p.add_argument("--role")
    a = ap.parse_args()
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is missing (python scripts/setup.py check)")
    cfg = load_config()
    {"check": cmd_check, "kit": cmd_kit, "sheet": cmd_sheet, "prompt": cmd_prompt, "import": cmd_import,
     "list": cmd_list}[a.cmd](cfg, a)


if __name__ == "__main__":
    main()
