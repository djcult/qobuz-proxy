"""Small libasound PCM interface used by the ALSA backend."""

from __future__ import annotations

import ctypes
import ctypes.util
from dataclasses import dataclass
from typing import Protocol


class AlsaError(RuntimeError):
    """An ALSA operation failed."""


class ExactFormatError(AlsaError):
    """The hardware cannot represent the source format exactly."""


@dataclass(frozen=True)
class PcmFormat:
    sample_rate: int
    channels: int
    bits_per_sample: int
    total_samples: int | None = None


class PcmDevice(Protocol):
    def open(self, audio_format: PcmFormat) -> None: ...
    def write(self, data: bytes) -> int: ...
    def delay_frames(self) -> int | None: ...
    def drain(self) -> None: ...
    def drop(self) -> None: ...
    def close(self) -> None: ...


class AlsaPcm:
    """Blocking, exact-format playback through a single ALSA ``hw:`` PCM."""

    _PLAYBACK = 0
    _NORMAL = 0
    _RW_INTERLEAVED = 3

    def __init__(self, device: str, latency_us: int = 500_000) -> None:
        if not device.startswith("hw:") or device.startswith("plughw:"):
            raise ValueError("ALSA device must be an explicit hw: device (never default/plughw:)")
        self.device = device
        self.latency_us = latency_us
        self._lib: ctypes.CDLL | None = None
        self._handle = ctypes.c_void_p()
        self._format: PcmFormat | None = None
        self._alsa_format = -1
        self._container_bytes = 0

    def _load(self) -> ctypes.CDLL:
        if self._lib is not None:
            return self._lib
        name = ctypes.util.find_library("asound") or "libasound.so.2"
        lib = ctypes.CDLL(name)
        lib.snd_strerror.argtypes = [ctypes.c_int]
        lib.snd_strerror.restype = ctypes.c_char_p
        lib.snd_pcm_format_value.argtypes = [ctypes.c_char_p]
        lib.snd_pcm_format_value.restype = ctypes.c_int
        lib.snd_pcm_open.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_int,
        ]
        lib.snd_pcm_open.restype = ctypes.c_int
        lib.snd_pcm_set_params.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        lib.snd_pcm_set_params.restype = ctypes.c_int
        lib.snd_pcm_hw_params_malloc.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.snd_pcm_hw_params_malloc.restype = ctypes.c_int
        lib.snd_pcm_hw_params_free.argtypes = [ctypes.c_void_p]
        lib.snd_pcm_hw_params_current.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.snd_pcm_hw_params_current.restype = ctypes.c_int
        lib.snd_pcm_hw_params_get_rate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.snd_pcm_hw_params_get_rate.restype = ctypes.c_int
        lib.snd_pcm_hw_params_get_channels.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.snd_pcm_hw_params_get_channels.restype = ctypes.c_int
        lib.snd_pcm_hw_params_get_format.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.snd_pcm_hw_params_get_format.restype = ctypes.c_int
        lib.snd_pcm_writei.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        lib.snd_pcm_writei.restype = ctypes.c_long
        lib.snd_pcm_delay.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_long)]
        lib.snd_pcm_delay.restype = ctypes.c_int
        lib.snd_pcm_recover.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.snd_pcm_recover.restype = ctypes.c_int
        for fn in ("snd_pcm_drain", "snd_pcm_drop", "snd_pcm_close"):
            method = getattr(lib, fn)
            method.argtypes = [ctypes.c_void_p]
            method.restype = ctypes.c_int
        self._lib = lib
        return lib

    def _parameters_are_exact(
        self, handle: ctypes.c_void_p, audio_format: PcmFormat, alsa_format: int
    ) -> bool:
        """Read back negotiated hw parameters; a hw device must not substitute them."""
        lib = self._load()
        params = ctypes.c_void_p()
        if lib.snd_pcm_hw_params_malloc(ctypes.byref(params)) < 0:
            return False
        try:
            if lib.snd_pcm_hw_params_current(handle, params) < 0:
                return False
            rate = ctypes.c_uint()
            direction = ctypes.c_int()
            channels = ctypes.c_uint()
            actual_format = ctypes.c_int()
            if (
                lib.snd_pcm_hw_params_get_rate(params, ctypes.byref(rate), ctypes.byref(direction))
                < 0
            ):
                return False
            if lib.snd_pcm_hw_params_get_channels(params, ctypes.byref(channels)) < 0:
                return False
            if lib.snd_pcm_hw_params_get_format(params, ctypes.byref(actual_format)) < 0:
                return False
            return (
                rate.value == audio_format.sample_rate
                and channels.value == audio_format.channels
                and actual_format.value == alsa_format
            )
        finally:
            lib.snd_pcm_hw_params_free(params)

    def _message(self, code: int) -> str:
        lib = self._load()
        raw = lib.snd_strerror(code)
        return raw.decode(errors="replace") if raw else f"ALSA error {code}"

    def _check(self, code: int, operation: str) -> None:
        if code < 0:
            raise AlsaError(f"{operation} failed for {self.device}: {self._message(code)}")

    def open(self, audio_format: PcmFormat) -> None:
        self.close()
        if audio_format.channels <= 0 or audio_format.channels > 2:
            raise ExactFormatError(f"Unsupported channel count: {audio_format.channels}")
        if audio_format.bits_per_sample not in (16, 24, 32):
            raise ExactFormatError(f"Unsupported source bit depth: {audio_format.bits_per_sample}")

        names = {
            16: [("S16_LE", 2)],
            24: [("S24_3LE", 3), ("S24_LE", 4)],
            32: [("S32_LE", 4)],
        }[audio_format.bits_per_sample]
        failures: list[str] = []
        lib = self._load()
        for format_name, container_bytes in names:
            handle = ctypes.c_void_p()
            rc = lib.snd_pcm_open(
                ctypes.byref(handle), self.device.encode(), self._PLAYBACK, self._NORMAL
            )
            if rc < 0:
                raise AlsaError(f"Cannot open ALSA device {self.device}: {self._message(rc)}")
            alsa_format = lib.snd_pcm_format_value(format_name.encode())
            rc = lib.snd_pcm_set_params(
                handle,
                alsa_format,
                self._RW_INTERLEAVED,
                audio_format.channels,
                audio_format.sample_rate,
                0,  # Never allow alsa-lib software resampling.
                self.latency_us,
            )
            if rc >= 0 and self._parameters_are_exact(handle, audio_format, alsa_format):
                self._handle = handle
                self._format = audio_format
                self._alsa_format = alsa_format
                self._container_bytes = container_bytes
                return
            failures.append(
                f"{format_name}: {self._message(rc)}"
                if rc < 0
                else f"{format_name}: hardware negotiated different parameters"
            )
            lib.snd_pcm_close(handle)
        raise ExactFormatError(
            f"{self.device} cannot represent {audio_format.sample_rate} Hz, "
            f"{audio_format.bits_per_sample}-bit, {audio_format.channels} channel PCM exactly "
            f"({'; '.join(failures)})"
        )

    def _packed_for_device(self, data: bytes) -> bytes:
        if self._format is None:
            raise AlsaError("PCM is not open")
        if self._format.bits_per_sample != 24 or self._container_bytes != 4:
            return data
        if len(data) % 3:
            raise AlsaError("24-bit PCM chunk is not sample-aligned")
        output = bytearray((len(data) // 3) * 4)
        out = 0
        for offset in range(0, len(data), 3):
            output[out : out + 3] = data[offset : offset + 3]
            output[out + 3] = 0xFF if data[offset + 2] & 0x80 else 0
            out += 4
        return bytes(output)

    def write(self, data: bytes) -> int:
        if not self._handle or self._format is None:
            raise AlsaError("PCM is not open")
        source_frame_bytes = self._format.channels * ((self._format.bits_per_sample + 7) // 8)
        if not data or len(data) % source_frame_bytes:
            raise AlsaError("PCM write is not aligned to complete source frames")
        packed = self._packed_for_device(data)
        device_frame_bytes = self._format.channels * self._container_bytes
        frames_total = len(packed) // device_frame_bytes
        buffer = ctypes.create_string_buffer(packed)
        frames_written = 0
        lib = self._load()
        while frames_written < frames_total:
            address = ctypes.addressof(buffer) + frames_written * device_frame_bytes
            rc = lib.snd_pcm_writei(
                self._handle, ctypes.c_void_p(address), frames_total - frames_written
            )
            if rc < 0:
                recovered = lib.snd_pcm_recover(self._handle, int(rc), 1)
                if recovered < 0:
                    raise AlsaError(
                        f"ALSA write/recovery failed for {self.device}: {self._message(recovered)}"
                    )
                continue
            if rc == 0:
                raise AlsaError(f"ALSA write made no progress on {self.device}")
            frames_written += int(rc)
        return frames_written

    def delay_frames(self) -> int | None:
        if not self._handle:
            return None
        delay = ctypes.c_long()
        rc = self._load().snd_pcm_delay(self._handle, ctypes.byref(delay))
        if rc < 0:
            recovered = self._load().snd_pcm_recover(self._handle, rc, 1)
            if recovered < 0:
                return None
            return None
        return max(0, int(delay.value))

    def drain(self) -> None:
        if self._handle:
            self._check(self._load().snd_pcm_drain(self._handle), "snd_pcm_drain")

    def drop(self) -> None:
        if self._handle:
            self._load().snd_pcm_drop(self._handle)

    def close(self) -> None:
        if self._handle:
            self._load().snd_pcm_close(self._handle)
            self._handle = ctypes.c_void_p()
        self._format = None
