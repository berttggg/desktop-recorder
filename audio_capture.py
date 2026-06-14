"""System (desktop) audio capture via WASAPI loopback.

Captures whatever is playing on the default output device. A silent
keep-alive output stream is run alongside the loopback input so that the
device keeps delivering frames even during silence (otherwise WASAPI
loopback stalls when nothing is playing, desyncing the recording).
"""

import wave
import threading

import pyaudiowpatch as pyaudio


class SystemAudioRecorder:
    def __init__(self, out_wav):
        self.out_wav = out_wav
        self._pa = None
        self._in = None
        self._keepalive = None
        self._wf = None
        self._sampwidth = 2
        self._lock = threading.Lock()  # guards _wf swaps vs. the write callback
        self.channels = 2
        self.rate = 48000
        self.error = None

    def _find_loopback(self):
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        spk = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        loop = None
        for d in self._pa.get_loopback_device_info_generator():
            if spk["name"] in d["name"]:
                loop = d
                break
        if loop is None:
            loop = next(self._pa.get_loopback_device_info_generator(), None)
        return spk, loop

    def start(self):
        self._pa = pyaudio.PyAudio()
        spk, loop = self._find_loopback()
        if loop is None:
            self._pa.terminate()
            raise RuntimeError("No WASAPI loopback device found (no active speaker?)")

        self.channels = int(loop["maxInputChannels"])
        self.rate = int(loop["defaultSampleRate"])

        self._sampwidth = self._pa.get_sample_size(pyaudio.paInt16)
        self._wf = wave.open(self.out_wav, "wb")
        self._wf.setnchannels(self.channels)
        self._wf.setsampwidth(self._sampwidth)
        self._wf.setframerate(self.rate)

        def in_cb(data, frames, time_info, status):
            try:
                with self._lock:
                    if self._wf is not None:
                        self._wf.writeframes(data)
            except Exception as e:  # file closed during shutdown
                self.error = e
                return (None, pyaudio.paComplete)
            return (None, pyaudio.paContinue)

        # Silent keep-alive output keeps the endpoint active.
        out_ch = int(spk["maxOutputChannels"]) or 2
        silence = b"\x00" * (1024 * out_ch * 2)

        def out_cb(in_data, frames, time_info, status):
            return (silence[: frames * out_ch * 2], pyaudio.paContinue)

        self._keepalive = self._pa.open(
            format=pyaudio.paInt16, channels=out_ch,
            rate=int(spk["defaultSampleRate"]), output=True,
            output_device_index=spk["index"], frames_per_buffer=1024,
            stream_callback=out_cb,
        )
        self._in = self._pa.open(
            format=pyaudio.paInt16, channels=self.channels, rate=self.rate,
            input=True, input_device_index=loop["index"],
            frames_per_buffer=1024, stream_callback=in_cb,
        )
        self._keepalive.start_stream()
        self._in.start_stream()

    def rotate(self, new_wav_path):
        """Finalize the current WAV and start writing a new one (live chunking).

        The next file is opened and configured *before* the swap, so the audio
        callback is blocked only for the reference swap (microseconds), never for
        disk I/O. Returns the path of the WAV just finalized, or None if capture
        isn't running yet."""
        if self._wf is None:
            self.out_wav = new_wav_path
            return None
        nf = wave.open(new_wav_path, "wb")
        nf.setnchannels(self.channels)
        nf.setsampwidth(self._sampwidth)
        nf.setframerate(self.rate)
        with self._lock:
            old = self._wf
            old_path = self.out_wav
            self._wf = nf
            self.out_wav = new_wav_path
        try:
            old.close()
        except Exception:
            pass
        return old_path

    def stop(self):
        for s in (self._in, self._keepalive):
            try:
                if s and s.is_active():
                    s.stop_stream()
                if s:
                    s.close()
            except Exception:
                pass
        with self._lock:
            wf, self._wf = self._wf, None
        try:
            if wf:
                wf.close()
        except Exception:
            pass
        try:
            if self._pa:
                self._pa.terminate()
        except Exception:
            pass
        return self.out_wav
