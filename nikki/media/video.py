"""Vídeo vertical barato: frames estáticos + zoom suave (ffmpeg embutido via imageio-ffmpeg)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg

FPS = 30


def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args: list[str]) -> None:
    proc = subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou: {proc.stderr[-800:]}")


def clip_from_image(image: Path, out: Path, seconds: float, zoom_in: bool = True) -> Path:
    frames = int(seconds * FPS)
    z = "min(zoom+0.0007,1.07)" if zoom_in else "if(eq(on,0),1.07,max(zoom-0.0007,1.0))"
    vf = (
        "scale=2160:3840,"
        f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1080x1920:fps={FPS},"
        f"fade=t=in:st=0:d=0.3,fade=t=out:st={seconds - 0.3:.2f}:d=0.3,format=yuv420p"
    )
    _run(["-loop", "1", "-i", str(image), "-vf", vf, "-t", f"{seconds}", "-r", str(FPS),
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-an", str(out)])
    return out


def concat_with_audio(clips: list[Path], out: Path, audio: Path | None = None) -> Path:
    """Concatena clipes e adiciona trilha (ou silêncio — várias redes rejeitam vídeo sem áudio)."""
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips), encoding="utf-8")
    audio_in = ["-i", str(audio)] if audio else ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    _run(["-f", "concat", "-safe", "0", "-i", str(lst), *audio_in, "-map", "0:v", "-map", "1:a",
          "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(out)])
    lst.unlink(missing_ok=True)
    return out


def single_offer_video(story_png: Path, out: Path, seconds: float = 8.0, audio: Path | None = None) -> Path:
    tmp = out.with_name(out.stem + "_v.mp4")
    clip_from_image(story_png, tmp, seconds)
    concat_with_audio([tmp], out, audio)
    tmp.unlink(missing_ok=True)
    return out


def slideshow_video(frames: list[tuple[Path, float]], out: Path, audio: Path | None = None) -> Path:
    clips = []
    for i, (img, secs) in enumerate(frames):
        clips.append(clip_from_image(img, out.with_name(f"{out.stem}_c{i}.mp4"), secs, zoom_in=i % 2 == 0))
    concat_with_audio(clips, out, audio)
    for c in clips:
        c.unlink(missing_ok=True)
    return out
