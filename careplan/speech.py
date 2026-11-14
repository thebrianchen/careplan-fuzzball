"""Speech path (H6): visit audio -> transcript -> the same planner.

Two commands, no GPU needed (faster-whisper on CPU; on the box this is the Grace-core fallback if the
GPU is exclusive to vLLM):

    python -m careplan.speech render cases/dev/D001-dialysis.yaml --out data/audio/D001 [--noise-db -28]
        Synthesizes the visit with macOS `say` (two voices), one 16 kHz mono wav per turn plus the whole
        visit in one file. Dev-only stand-in for a recorded hero visit.

    python -m careplan.speech transcribe data/audio/D001 --case cases/dev/D001-dialysis.yaml \
        --model small.en --out data/asr_cases
        Transcribes every turn, reports WER per speaker and overall plus the real-time factor on the
        whole-visit file, and writes a copy of the case with each turn's text replaced by the ASR text.
        Refs, speakers and topic tags are kept, so `careplan run --cases data/asr_cases` scores with the
        standard scorer: the E6 check is whether the planner still finds the planted conflicts.

Per-turn transcription stands in for a push-to-talk or labelled-mic demo; whole-file WER is the
continuous-audio number. Speaker diarization is out of scope.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

from .cases import load_case

VOICES = {"doctor": "Samantha", "patient": "Fred", "caregiver": "Flo (English (US))", "nurse": "Karen", "interpreter": "Eddy (English (US))"}


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def render(case_path: Path, out: Path, noise_db: float | None = None) -> Path:
    case = load_case(case_path)
    out.mkdir(parents=True, exist_ok=True)
    wavs = []
    for turn in case.visit["transcript"]:
        stem = out / turn["ref"].replace(":", "_")
        _run(["say", "-v", VOICES.get(turn["speaker"], "Samantha"), "-o", f"{stem}.aiff", turn["text"]])
        filt = ["-ar", "16000", "-ac", "1"]
        if noise_db is not None:   # add pink room noise (the spec records the hero clip in a noisy room on purpose)
            _run(["ffmpeg", "-y", "-loglevel", "error", "-i", f"{stem}.aiff", "-f", "lavfi",
                  "-i", "anoisesrc=color=pink:amplitude=0.5", "-filter_complex",
                  f"[1:a]volume={noise_db}dB[n];[0:a][n]amix=inputs=2:duration=first:normalize=0[o]",
                  "-map", "[o]", *filt, f"{stem}.wav"])
        else:
            _run(["ffmpeg", "-y", "-loglevel", "error", "-i", f"{stem}.aiff", *filt, f"{stem}.wav"])
        Path(f"{stem}.aiff").unlink()
        wavs.append(f"{stem}.wav")
    lst = out / "turns.txt"
    lst.write_text("".join(f"file '{Path(w).name}'\n" for w in wavs))
    _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out / "visit.wav")])
    (out / "manifest.json").write_text(json.dumps({"case_id": case.case_id, "source": str(case_path), "noise_db": noise_db,
                                                   "turns": [t["ref"] for t in case.visit["transcript"]]}, indent=2))
    return out


def _norm(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower().replace("-", " ")).split()


def wer(ref: str, hyp: str) -> tuple[int, int]:
    """(edit distance, reference words) at word level; sum over turns before dividing."""
    r, h = _norm(ref), _norm(hyp)
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw)))
        prev = cur
    return prev[-1], len(r)


def _pcm16k(wav: Path):
    """Read a 16 kHz mono 16-bit wav as float32 (what render writes), bypassing faster-whisper's PyAV decoder."""
    import wave
    import numpy as np
    with wave.open(str(wav), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2), f"{wav}: need 16 kHz mono s16"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def _duration_s(wav: Path) -> float:
    import wave
    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / w.getframerate()


class Transcriber:
    """One lazily loaded faster-whisper model for the card server (live visit capture). CPU int8 by default:
    measured RTF 0.09 on the Spark's Grace cores with Super loaded, +42 MB."""

    def __init__(self, model_size: str = "small.en", device: str = "cpu", compute_type: str = "int8"):
        self.args = (model_size, device, compute_type)
        self._model = None

    def model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.args[0], device=self.args[1], compute_type=self.args[2])
        return self._model

    def pcm16(self, data: bytes, initial_prompt: str | None = None) -> str:
        """16 kHz mono signed-16-bit little-endian PCM -> text. initial_prompt biases the decoder toward the words in
        it (drug names, facilities): see chart_vocabulary()."""
        import numpy as np
        audio = np.frombuffer(data[: len(data) - len(data) % 2], dtype="<i2").astype(np.float32) / 32768.0
        if len(audio) < 1600:          # under 0.1 s: nothing to transcribe
            return ""
        segs, _ = self.model().transcribe(audio, language="en", beam_size=5, vad_filter=False, initial_prompt=initial_prompt)
        return " ".join(s.text.strip() for s in segs).strip()


def chart_vocabulary(case) -> str:
    """Words the visit is likely to contain, taken from the chart on box 2: medication names, facilities, departments,
    problems. Whisper reads it as a prior; the chart itself never leaves the box. No identifiers (name, MRN, DOB)."""
    p = case.patient
    words: list[str] = []
    words += [m["name"] for m in p.get("meds", []) if m.get("name")]
    for a in p.get("appointments", []):
        loc = (a.get("location") or "").split(",")[0].strip()
        words += [w for w in (a.get("dept"), loc) if w]
    words += [pr.get("name") or pr.get("description") or "" for pr in p.get("problems", [])]
    seen, out = set(), []
    for w in words:
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return "Clinic visit. " + ", ".join(out[:40]) + "." if out else "Clinic visit."


def transcribe(audio_dir: Path, case_path: Path, model_size: str, out_dir: Path, device: str = "cpu",
               compute_type: str = "int8", threads: int = 0) -> dict:
    from faster_whisper import WhisperModel
    case = load_case(case_path)
    model = WhisperModel(model_size, device=device, compute_type=compute_type, cpu_threads=threads)

    def run(wav: Path) -> str:
        segs, _ = model.transcribe(_pcm16k(wav), language="en", beam_size=5, vad_filter=False)
        return " ".join(s.text.strip() for s in segs)

    data = json.loads(json.dumps(case.data))
    errs: dict[str, list[int]] = {}
    for turn in data["visit"]["transcript"]:
        hyp = run(audio_dir / (turn["ref"].replace(":", "_") + ".wav"))
        e, n = wer(turn["text"], hyp)
        errs.setdefault(turn["speaker"], [0, 0])
        errs[turn["speaker"]][0] += e
        errs[turn["speaker"]][1] += n
        turn["text"] = hyp or "[inaudible]"
    full = audio_dir / "visit.wav"
    t0 = time.time()
    full_hyp = run(full)
    elapsed = time.time() - t0
    dur = _duration_s(full)
    fe, fn = wer(" ".join(t["text"] for t in case.visit["transcript"]), full_hyp)
    out_dir.mkdir(parents=True, exist_ok=True)
    data["title"] = data.get("title", case.case_id) + " (ASR transcript)"
    data["asr"] = {"model": f"faster-whisper {model_size} {compute_type} {device}", "audio": str(audio_dir),
                   "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    out_path = out_dir / case_path.name
    out_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    tot_e, tot_n = sum(v[0] for v in errs.values()), sum(v[1] for v in errs.values())
    report = {"case_id": case.case_id, "model": data["asr"]["model"],
              "per_turn_wer": round(tot_e / max(tot_n, 1), 3),
              "per_speaker_wer": {s: round(v[0] / max(v[1], 1), 3) for s, v in errs.items()},
              "whole_visit_wer": round(fe / max(fn, 1), 3), "audio_s": round(dur, 1),
              "transcribe_s": round(elapsed, 1), "rtf": round(elapsed / dur, 3), "asr_case": str(out_path)}
    (out_dir / f"{case.case_id}_asr_report.json").write_text(json.dumps(report, indent=2))
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="careplan.speech")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="synthesize a case's visit with macOS say (dev only)")
    r.add_argument("case", type=Path)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--noise-db", type=float, default=None, help="mix pink noise at this level, e.g. -28")
    t = sub.add_parser("transcribe", help="transcribe rendered or recorded turns; write the ASR copy of the case")
    t.add_argument("audio_dir", type=Path)
    t.add_argument("--case", type=Path, required=True)
    t.add_argument("--model", default="small.en")
    t.add_argument("--out", type=Path, default=Path("data/asr_cases"))
    t.add_argument("--device", default="cpu")
    t.add_argument("--compute-type", default="int8")
    t.add_argument("--threads", type=int, default=0)
    a = ap.parse_args(argv)
    if a.cmd == "render":
        out = render(a.case, a.out, a.noise_db)
        print(f"rendered {len(list(out.glob('turn_*.wav')))} turns + visit.wav in {out}")
        return 0
    rep = transcribe(a.audio_dir, a.case, a.model, a.out, a.device, a.compute_type, a.threads)
    print(json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
