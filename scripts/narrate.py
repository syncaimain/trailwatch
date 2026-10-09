"""Narrate the kill-test recording with Piper (open-source, offline TTS).

  python scripts/narrate.py --voice path/to/en_US-ryan-high.onnx

The long worker-dead stretch plays at 4x (labelled on screen), and narration
lines are placed back to back: each starts at its cue or right after the
previous line, whichever is later, so there are no dead gaps.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (start, end, speed) in seconds of the source recording.
SEGMENTS = [(0, 24, 1), (24, 74, 4), (74, 108, 1)]
END_CARD = 6

# (cue in OUTPUT seconds, text). Cues match events after the speed-up:
# kill 17, alert due 35, worker back 39.5, alert 48.5, I'm OK 55.5, home 61.5, end 70.5.
LINES = [
    (0.3, "This is TrailWatch: a hiking check-in timer that can't forget about you. "
          "You type one sentence, where you're going and when you'll be back, "
          "and Gemma, running locally on this laptop, turns it into a check-in plan, using tonight's sunset and forecast."),
    (14.8, "The trip is armed. Now, I hard-kill the worker."),
    (18.0, "Nothing is running TrailWatch code anymore. But the check-in timer was never inside a process. "
           "It's a durable timer on the Temporal server, and the hiker's phone says so."),
    (27.3, "I'm playing this part at four times speed. The check-in deadline passes. Then the grace period. "
           "Still no worker. The alert is due, and right now, nothing could send it."),
    (36.6, "So, let's bring the worker back."),
    (39.5, "Temporal replays the history, sees that both timers already fired, "
           "nudges the hiker, and alerts the emergency contact straight away."),
    (47.4, "First, an instant template alert. Then local Gemma writes a situation brief from the trip log."),
    (53.0, "The hiker gets signal back and taps I'm OK. The contact gets an all clear."),
    (59.5, "Back home, one tap cancels the last timer. One hard crash. Zero lost alerts."),
    (65.5, "TrailWatch. Local Gemma, Temporal, and n t f y, all open source. Now put your phone away, and go outside."),
]
GAP = 0.3  # minimum breath between lines


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def duration(path: Path) -> float:
    return float(run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", required=True, help="Piper .onnx voice model")
    ap.add_argument("--video", default=str(ROOT / "media" / "trailwatch_kill_test.mp4"))
    ap.add_argument("--out", default=str(ROOT / "media" / "trailwatch_kill_test_narrated.mp4"))
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="tw-narration-"))

    # 1) Synthesize each line and lay them out back to back.
    placed, prev_end = [], 0.0
    for i, (cue, text) in enumerate(LINES):
        wav = tmp / f"line{i}.wav"
        subprocess.run([sys.executable, "-m", "piper", "-m", args.voice, "-f", str(wav)],
                       input=text, text=True, check=True, capture_output=True)
        start = max(cue, prev_end + GAP)
        d = duration(wav)
        drift = f"  (+{start - cue:.1f}s late)" if start - cue > 0.05 else ""
        print(f"{start:6.1f}s - {start + d:6.1f}s  {text[:60]}{drift}")
        placed.append((wav, start))
        prev_end = start + d

    # 2) Video: speed up the dead stretch, label it, hold the last frame.
    parts, labels = [], []
    for j, (a, b, speed) in enumerate(SEGMENTS):
        v = f"[0:v]trim={a}:{b},setpts=(PTS-STARTPTS)/{speed}"
        if speed != 1:
            v += (",drawtext=fontfile='C\\:/Windows/Fonts/segoeuib.ttf':text='{0}x speed':"
                  "x=w-tw-28:y=h-th-24:fontsize=30:fontcolor=white:box=1:boxcolor=0xd04a3a@0.9:boxborderw=12"
                  ).format(speed)
        parts.append(v + f"[s{j}]")
        labels.append(f"[s{j}]")
    video_len = sum((b - a) / s for a, b, s in SEGMENTS)
    total = max(video_len + END_CARD, prev_end + 0.8)
    vf = ";".join(parts) + f";{''.join(labels)}concat=n={len(SEGMENTS)}:v=1:a=0," \
         f"tpad=stop_mode=clone:stop_duration={total - video_len:.2f}[v]"

    # 3) Audio: each line delayed to its slot, mixed, padded to the video length.
    inputs, af = [], []
    for k, (wav, start) in enumerate(placed):
        inputs += ["-i", str(wav)]
        ms = int(start * 1000)
        af.append(f"[{k + 1}:a]adelay={ms}|{ms}[a{k}]")
    af_s = ";".join(af) + ";" + "".join(f"[a{k}]" for k in range(len(placed))) + \
        f"amix=inputs={len(placed)}:normalize=0,apad,atrim=0:{total:.2f}[a]"

    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", args.video, *inputs,
                    "-filter_complex", vf + ";" + af_s, "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "22", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", args.out], check=True)
    print(f"Saved {args.out} ({total:.1f}s)")


if __name__ == "__main__":
    main()
