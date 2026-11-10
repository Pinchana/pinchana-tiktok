import asyncio
import hashlib
import shutil
import subprocess

import pytest

from pinchana_tiktok.audio import measure_loudness, normalize_video_audio

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires ffmpeg')


def make_video(path, gain):
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=64x64:r=10:d=3',
                    '-f','lavfi','-i',f'sine=frequency=1000:duration=3,volume={gain}',
                    '-c:v','libx264','-c:a','aac','-shortest',str(path)],check=True)


def video_hash(path):
    return hashlib.sha256(subprocess.check_output(['ffmpeg','-v','error','-i',str(path),'-map','0:v:0','-c:v','copy','-f','h264','-'])).hexdigest()


def test_quiet_audio_is_raised_and_video_stream_is_identical(tmp_path):
    path=tmp_path/'quiet.mp4';make_video(path,0.2)
    async def run():
        before=await measure_loudness(path); original=video_hash(path)
        assert await normalize_video_audio(path)==path
        after=await measure_loudness(path)
        assert float(after['input_i'])>float(before['input_i'])+5
        assert abs(float(after['input_i'])+14)<1
        assert float(after['input_tp'])<=-0.5  # Allow AAC encode measurement tolerance.
        assert video_hash(path)==original
        assert not list(tmp_path.glob('.*audio-normalized*'))
    asyncio.run(run())


def test_already_loud_audio_and_silent_video_are_unchanged(tmp_path):
    loud=tmp_path/'loud.mp4';make_video(loud,3)
    original=loud.read_bytes()
    asyncio.run(normalize_video_audio(loud))
    assert loud.read_bytes()==original
    silent=tmp_path/'silent.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=64x64:r=10:d=1','-c:v','libx264',str(silent)],check=True)
    original=silent.read_bytes()
    asyncio.run(normalize_video_audio(silent))
    assert silent.read_bytes()==original
