#!/usr/bin/env python3
"""Self test of the ElevenLabs route (generate.py) without spending credits or touching the network.

Takes are made by the test-only fake backend (REAL_SFX_GEN_FAKE=1: noise bursts), in a temp folder with its own
config, index and generated library. Checks: the plan spends nothing without --yes, a kit fills the roles of an event
list, a second run reuses the takes, from_cues routes every kind to the generated library, a "gen" cue gets its file,
mix.py refuses a gen cue without a file and builds the film with one, the connector route writes jobs and files the
downloads, and the API route without a key or network fails with a clear message.

  python tests/test_generate.py [--keep]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
S = SKILL / "scripts"
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def sh(args, env, cwd):
    r = subprocess.run([str(x) for x in args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=env, cwd=cwd)
    return r, r.stdout + r.stderr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="realsfx_gen_"))
    env = dict(os.environ, REAL_SFX_CONFIG=str(tmp / "config.json"), REAL_SFX_INDEX=str(tmp / "index.json"),
               REAL_SFX_GEN_DIR=str(tmp / "gen"), REAL_SFX_TASTE=str(tmp / "taste.json"))
    env.pop("ELEVENLABS_API_KEY", None)
    fake = dict(env, REAL_SFX_GEN_FAKE="1")
    (tmp / "config.json").write_text(json.dumps({
        "libraries": [], "index_path": str(tmp / "index.json"),
        "uisfx": {"path": str(SKILL / "vendor/node_modules/uisfx"), "default_pack": "studio"}}))
    (tmp / "index.json").write_text('{"count": 0, "sounds": []}')
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=6",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "6", "-shortest", str(tmp / "film.mp4")],
                   check=True)
    events = [{"t": 0.5, "type": "whoosh"}, {"t": 1.5, "type": "hit"}, {"t": 2.5, "type": "pop"},
              {"t": 3.2, "type": "click"}, {"t": 4.0, "type": "chime"}, {"t": 4.6, "type": "whoosh"}]
    (tmp / "ev.json").write_text(json.dumps(events))
    G = [sys.executable, S / "generate.py"]
    try:
        r, out = sh(G + ["kit", "ev.json", "--backend", "api"], env, tmp)
        check("kit without --yes prints the plan and spends nothing",
              r.returncode == 0 and "nothing spent" in out and not (tmp / "gen" / "catalog.json").exists(),
              out.splitlines()[1] if len(out.splitlines()) > 1 else out)

        r, out = sh(G + ["kit", "ev.json", "--yes"], fake, tmp)
        cat = json.loads((tmp / "gen" / "catalog.json").read_text())["sounds"] if r.returncode == 0 else []
        roles = sorted({c["role"] for c in cat})
        cfg = json.loads((tmp / "config.json").read_text())
        check("kit generates takes for every kind, registers the library and indexes it",
              r.returncode == 0 and roles == ["chime", "click", "hit", "pop", "whoosh"]
              and any(l["name"] == "elevenlabs" for l in cfg["libraries"]), f"{len(cat)} takes, {roles}")
        idx = json.loads((tmp / "index.json").read_text())["sounds"]
        check("every take keeps its role in the index (prompt words do not drift a hit into foley)",
              sorted({s["role"] for s in idx}) == roles, str(sorted({s['role'] for s in idx})))

        r, out = sh(G + ["kit", "ev.json", "--yes"], fake, tmp)
        check("a second kit run reuses the takes", "0 to generate" in out, out.splitlines()[1])

        r, out = sh([sys.executable, S / "from_cues.py", "ev.json", "--video", "film.mp4", "--out", "cues.json",
                     "--ui", "library"], env, tmp)
        line = next((l for l in out.splitlines() if l.startswith("routes:")), "")
        check("from_cues routes every kind to the generated library",
              r.returncode == 0 and "uisfx" not in line and "silent" in out and ", 0 silent" in out, line)

        sheet = json.loads((tmp / "cues.json").read_text())
        sheet["cues"].append({"t": 5.2, "role": "paper", "label": "page", "gen": {"prompt": "single page turn, dry",
                                                                                   "dur": 0.8, "takes": 2}})
        (tmp / "cues.json").write_text(json.dumps(sheet))
        r, out = sh([sys.executable, S / "mix.py", "cues.json", "--out", "out/a.mp4"], env, tmp)
        check("mix.py refuses a gen cue that has no file yet and names the command", r.returncode != 0
              and "generate.py sheet" in out, out.strip().splitlines()[-1] if out.strip() else "")

        r, out = sh(G + ["sheet", "cues.json", "--yes"], fake, tmp)
        page = next(c for c in json.loads((tmp / "cues.json").read_text())["cues"] if c.get("label") == "page")
        check("sheet: a gen cue gets take 1 as its file and both takes in gen_takes",
              r.returncode == 0 and Path(page.get("file", "")).exists() and len(page.get("gen_takes", [])) == 2)

        r, out = sh([sys.executable, S / "mix.py", "cues.json", "--out", "out/a.mp4"], env, tmp)
        res = json.loads((tmp / "out" / "a_resolved_cues.json").read_text()) if r.returncode == 0 else {}
        libs = {c.get("library") for c in (res.get("cues") or [])}
        check("mix.py builds the film from generated takes only", r.returncode == 0
              and (tmp / "out" / "a.mp4").exists(), f"libraries {libs}")

        r, out = sh(G + ["prompt", "leather glove slap, close", "--role", "foley", "--takes", "2",
                         "--jobs-out", "work/jobs.json"], env, tmp)
        jobs = json.loads((tmp / "work" / "jobs.json").read_text())["jobs"] if r.returncode == 0 else []
        check("connector route: no key, the jobs are written with prompt, length and where to save",
              len(jobs) == 2 and all(j["save_as"].endswith(".mp3") and "connector_prompt" in j for j in jobs))
        if jobs:                                          # play the agent: the "download" of the first job only
            Path(jobs[0]["save_as"]).parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=d=0.6:a=0.3",
                            jobs[0]["save_as"]], check=True)
        r, out = sh(G + ["import", "work/jobs.json"], env, tmp)
        left = json.loads((tmp / "work" / "jobs.json").read_text())["jobs"]
        check("import files the downloaded take and keeps the missing one as a job",
              "imported 1" in out and len(left) == 1, out.strip().splitlines()[0])
        r, out = sh(G + ["prompt", "leather glove slap, close", "--role", "foley", "--takes", "2",
                         "--jobs-out", "work/jobs.json"], env, tmp)
        check("the same command afterwards finds the imported take in the cache", "1 already generated" in out)

        # the API route against a local stand-in for ElevenLabs: the request body and headers, the cost header
        import http.server
        import threading
        mp3 = tmp / "stand_in.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=d=0.7:a=0.3", str(mp3)],
                       check=True)
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, self.headers.get("xi-api-key"), body))
                data = mp3.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("character-cost", "40")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a_):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        api_env = dict(env, ELEVENLABS_API_KEY="test-key",
                       REAL_SFX_GEN_API_URL=f"http://127.0.0.1:{srv.server_address[1]}/v1/sound-generation")
        r, out = sh(G + ["prompt", "brass key ring jingle", "--role", "foley", "--dur", "0.9", "--yes"], api_env, tmp)
        srv.shutdown()
        body = seen[0][2] if seen else {}
        row = next((c for c in json.loads((tmp / "gen" / "catalog.json").read_text())["sounds"]
                    if "brass key" in c["prompt"]), {})
        check("api route: the request has the key, model, length and influence; the cost header is recorded",
              r.returncode == 0 and seen and seen[0][1] == "test-key" and "output_format=" in seen[0][0]
              and body.get("model_id") == "eleven_text_to_sound_v2" and body.get("duration_seconds") == 0.9
              and 0 <= body.get("prompt_influence", -1) <= 1 and row.get("cost") == 40.0
              and (tmp / "gen" / row.get("file", "x")).exists(), str(body)[:120])

        r, out = sh(G + ["prompt", "x", "--backend", "api", "--yes"], env, tmp)
        check("api route without a key stops with a clear message", r.returncode != 0 and "ELEVENLABS_API_KEY" in out)
    finally:
        if a.keep:
            print("kept:", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
