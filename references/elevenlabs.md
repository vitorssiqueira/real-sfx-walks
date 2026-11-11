# Generating sound effects with ElevenLabs

`scripts/generate.py` talks to ElevenLabs text to sound (`eleven_text_to_sound_v2`) and files every take as a WAV in the
generated library (`~/.real-sfx/elevenlabs` by default, `config.json` `elevenlabs.dir`, or `REAL_SFX_GEN_DIR`). The
library is registered as `elevenlabs`, tier main, and indexed, so everything downstream treats a take like a recorded
sound: `search.py --library elevenlabs`, the picker, `from_cues.py`, `mix.py`, `bed.py`, `taste.py ban/love`.

## Order of work

1. **Event list first** (project list, `ui_events.py`, or your own list from the frames).
2. **`generate.py kit EVENTS.json`**: prints the plan (kinds, takes, lengths, prompts, credits per call so far). Read
   it, then run again with `--yes`. Kinds already in the generated library cost nothing.
   - takes per kind: `--per-kind` (default 3; 2 for a kind that occurs once; clicks, ticks and tocks get 3 because
     `from_cues` wants three single clicks before it leaves uisfx; typing gets 2 long takes that `from_cues` cuts
     into keystrokes);
   - `--bed`: two 20 s steady loops for `bed.py` (dark room tone; `bed.py --list` still measures and may refuse them);
   - `--flavor "restrained, premium, editorial"` or `config.json` `elevenlabs.flavor`: words added to every prompt.
3. **`from_cues.py ... --ui library`**: now every kind has candidates; without `--ui library` the interface kinds still
   go to uisfx.
4. **Named moments** (the hook, the logo, a physical object the film shows): a `gen` cue, written by hand or with
   `sheet.py set --at T 'gen={"prompt":"...","dur":1.2,"takes":2}'`, then `generate.py sheet SHEET --yes`. Take 1
   becomes `file`; the others sit in `gen_takes` (`sheet.py set --at T file=PATH` swaps). `mix.py` refuses a `gen` cue
   that has no file yet.
5. The usual loop (`mix.py`, critique, listen list). A take the user dislikes: `taste.py ban ID`; a kind that keeps
   sounding wrong: change the prompt, do not ask the same prompt for more takes.

## Prompts

The model follows concrete words. Every prompt says:
- **the physical source**: "leather glove on a wooden table", "paper page turn", not "cinematic impact";
- **attack and tail**: "tight attack, short tail", "builds steadily and ends at its peak";
- **distance and room**: "close microphone, dry";
- **one event**: "one click only" for anything `from_cues` places as a single sound;
- `generate.py` appends "isolated sound effect, no music, no voices" itself.

Avoid: "epic", "trailer", "cinematic whoosh" (generic results), brand or song names, and words the picker's name guards
filter (mouth pops, drum fills, censor beeps), since `generate.py list` shows the prompt but the index only sees the
role.

`prompt_influence`: 0.5–0.7 for short single sounds (follow the words), 0.3–0.4 for ambiences (more natural variation).

## Lengths

- API: `duration_seconds` 0.5–30 s; the kit asks a little longer than the cue needs (`mix.py` trims tails and places by
  the audible start, it does not stretch well past 2×).
- The connector has no length field: the length goes into the prompt ("about 0.8 seconds long"), and the model often
  ignores it and returns 1–2 s. Measure with `search.py --library elevenlabs` (the length column is the audible one).
- Anything under 0.5 s comes back padded with silence: harmless, `mix.py` drops it.
- Long beds: `loop: true` (`--bed` sets it) or two takes laid by `bed.py` with its overlap. Never splice a take with
  its own reverse.

## Credits

- Nothing is spent without `--yes`; `--max` (default 24) caps the calls of one run.
- The API returns the cost of each call (`character-cost`); it is recorded in the catalog, and the plan shows the
  median of what was recorded so far. `generate.py check` sums it.
- A call that failed with 401 (key), 402 (no credits) or no network stops the run; a 429 is retried twice after a wait
  (not charged). Anything else: read the error, do not call again blindly. Every finished take is saved at once, so a
  crash keeps what was paid for.

## Two routes

**API** (Claude Code on the user's machine): `ELEVENLABS_API_KEY` in the environment, or the variable named in
`config.json` `elevenlabs.api_key_env`. The key is never written into config or the catalog.

**Connector** (a cloud session where the shell cannot reach api.elevenlabs.io, or no key): `generate.py` writes
`work/gen_jobs.json`, one job per take with `connector_prompt` and `save_as`. For each job:
1. `creative_generate_in_flow` with `node_type: "sfx"`, `model_id: "eleven_text_to_sound_v2"`,
   `prompt: connector_prompt`, `generations_count: 1`; one `flow_id` for the whole film (create it with the first call
   or `creative_create_flow`). `estimate_only: true` on the first job prices the run: tell the user before going on.
2. Poll `creative_get_flow_run_status` until done; big answers go to a file (`jq '.media[]|[.generation_id,.url]'`).
3. Download the url to `save_as` at once (the URLs expire in about 2 h). If the shell cannot download it either, say
   so: the takes stay on the user's ElevenLabs canvas and can be attached to the chat.
4. `generate.py import work/gen_jobs.json` files what was downloaded (the rest stays in the jobs file), then run the
   same `kit` / `sheet` command again: it finds the takes in the cache.

Do not call `creative_generate_in_flow` again to retry a job: each call is a new, charged generation.
