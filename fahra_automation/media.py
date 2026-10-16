from __future__ import annotations

import json
import math
import re
import subprocess
import textwrap
from fractions import Fraction
from pathlib import Path


class Media:
    def __init__(self, config):
        self.config = config
        self.ffmpeg = config.section("video")["ffmpeg"]
        self.ffprobe = config.section("video")["ffprobe"]
        self.docker = config.section("downloader")["docker"]
        self.container = config.section("downloader")["container"]

    @staticmethod
    def run(command, timeout=600):
        r = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            raise RuntimeError("Media tool gagal: " + r.stderr[-1500:])
        return r.stdout

    def probe(self, path):
        data = json.loads(self.run([self.ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]))
        videos = [s for s in data["streams"] if s["codec_type"] == "video"]
        if len(videos) != 1:
            raise ValueError("File harus berisi satu stream video.")
        duration = float(data["format"]["duration"])
        if not math.isfinite(duration) or not 3 <= duration <= 600:
            raise ValueError("Durasi video harus antara 3 dan 600 detik.")
        if min(videos[0]["width"], videos[0]["height"]) < 360:
            raise ValueError("Resolusi sumber terlalu kecil.")
        audio = [s for s in data['streams'] if s['codec_type'] == 'audio']
        try:
            fps = float(Fraction(videos[0].get('avg_frame_rate') or videos[0].get('r_frame_rate', '0')))
        except (ValueError, ZeroDivisionError):
            fps = 0
        return {"duration": duration, "width": videos[0]["width"], "height": videos[0]["height"],
                "codec": videos[0]["codec_name"], "audio": any(s["codec_type"] == "audio" for s in data["streams"]),
                "audio_codec": audio[0].get('codec_name') if audio else None,
                "pixel_format": videos[0].get('pix_fmt'), "fps": fps,
                "size": int(data["format"]["size"])}

    @staticmethod
    def validate_shared(info):
        duration = info.get('duration', 0)
        if isinstance(duration, bool) or not isinstance(duration, (float, int)) or not math.isfinite(duration) or not 3 <= duration <= 90:
            raise ValueError('Profil bersama memerlukan durasi 3–90 detik; adaptasi perlu keputusan terpisah.')
        w, h = info.get('width', 0), info.get('height', 0)
        if w < 540 or h < 960 or abs(w / h - 9 / 16) > .005:
            raise ValueError('Profil bersama memerlukan video 9:16 minimal 540 × 960; tidak dipotong otomatis.')
        return info

    @staticmethod
    def validate_delivery(info, platform):
        if platform not in {'tiktok', 'facebook', 'youtube'}:
            raise ValueError('Platform media tidak didukung.')
        Media.validate_shared(info)
        if info.get('codec') != 'h264' or info.get('pixel_format') != 'yuv420p':
            raise ValueError('Hasil harus H.264/yuv420p.')
        if info.get('audio') and info.get('audio_codec') != 'aac':
            raise ValueError('Audio hasil harus AAC.')
        if not 24 <= info.get('fps', 0) <= 60 or not 0 < info.get('size', 0) <= 1_000_000_000:
            raise ValueError('Frame rate/ukuran hasil di luar kontrak bersama.')
        return info

    def analysis_assets(self, source, folder, count=6, audio=True):
        info = self.probe(source)
        folder.mkdir(parents=True, exist_ok=True)
        frames = []
        for i in range(count):
            second = min(info["duration"] - .1, info["duration"] * (i + .5) / count)
            target = folder / f"frame-{i}.jpg"
            self.run([self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(second), "-i", str(source),
                      "-frames:v", "1", "-vf", "scale=512:512:force_original_aspect_ratio=decrease", "-q:v", "4", str(target)])
            frames.append(target)
        sound = None
        if audio and info["audio"]:
            sound = folder / "speech.wav"
            self.run([self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
                      "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(sound)])
        return frames, sound

    def render(self, source, target, generated, job_id):
        info = self.probe(source)
        self.validate_shared(info)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', job_id):
            raise ValueError('Identitas render tidak valid.')
        w, h = info["width"] // 2 * 2, info["height"] // 2 * 2
        sw, sh = round(w * 1.1 / 2) * 2, round(h * 1.1 / 2) * 2
        target.parent.mkdir(parents=True, exist_ok=True)
        hook = target.parent / "hook.txt"
        hook.write_text("\n".join(textwrap.wrap(generated["hook"], width=25)), encoding="utf-8")
        scratch = "/tmp/fahra_automation-render-" + job_id
        docker = [self.docker, "exec", self.container]
        self.run([*docker, "mkdir", "-p", scratch])
        try:
            self.run([self.docker, "cp", str(source), self.container + ":" + scratch + "/source.mp4"])
            self.run([self.docker, "cp", str(hook), self.container + ":" + scratch + "/hook.txt"])
            size = max(20, round(w / 18))
            vf = (f"hflip,scale={sw}:{sh},crop={w}:{h}:(iw-ow)/2:(ih-oh)/2,setsar=1,"
                  "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
                  f"textfile={scratch}/hook.txt:expansion=none:fontsize={size}:fontcolor=white:"
                  f"borderw=2:bordercolor=black:box=1:boxcolor=black@0.45:boxborderw={size//3}:"
                  f"line_spacing=6:x=(w-tw)/2:y=h*0.68:enable='lt(t,{self.config.section('video')['hook_seconds']})'")
            self.run([*docker, "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", scratch + "/source.mp4",
                      "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                      "-crf", str(self.config.section("video")["crf"]), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                      "-r", "30",
                      "-map_metadata", "-1", "-metadata", "title=" + generated["hook"],
                      "-metadata", "comment=" + generated["caption"], "-movflags", "+faststart", scratch + "/final.mp4"], timeout=1200)
            partial = target.with_suffix(".partial.mp4")
            self.run([self.docker, "cp", self.container + ":" + scratch + "/final.mp4", str(partial)])
            result = self.probe(partial)
            if result["codec"] != "h264" or result["audio"] != info["audio"] or abs(result["duration"] - info["duration"]) > .5:
                raise ValueError("Pemeriksaan render gagal.")
            if result["size"] > 1_000_000_000:
                raise ValueError("Hasil video melampaui 1 GB.")
            self.validate_delivery(result, 'facebook')
            partial.replace(target)
            return result
        finally:
            self.run([*docker, "python", "-c", "import shutil,sys; shutil.rmtree(sys.argv[1],ignore_errors=True)", scratch])
