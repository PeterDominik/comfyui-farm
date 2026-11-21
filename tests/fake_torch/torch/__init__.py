"""A stand-in for torch, backed by numpy: just what workflows/gpu_test/run.py uses, so the
tests run the farm's test process without torch or a GPU (tests/test_gpu_test.py).

FAKE_TORCH_GPU=0 makes it see no GPU; FAKE_TORCH_FREE_GB and FAKE_TORCH_TOTAL_GB set the card."""

import os

import numpy as np

__version__ = "0.0+fake"
uint8, float32 = np.uint8, np.float32
_peak = [0]


class OutOfMemoryError(RuntimeError):
    pass


class Tensor(np.ndarray):
    def clamp(self, lo, hi):
        return np.clip(np.asarray(self), lo, hi).view(Tensor)

    def to(self, device=None, dtype=None):
        if isinstance(device, type) or (device is not None and not isinstance(device, _Device)):
            dtype, device = device, None
        out = self.astype(dtype).view(Tensor) if dtype is not None else self
        _peak[0] = max(_peak[0], out.nbytes)
        return out

    def cpu(self):
        return self

    def numpy(self):
        return np.asarray(self)

    def permute(self, *dims):
        return np.asarray(self).transpose(dims).view(Tensor)

    def unsqueeze(self, i):
        return np.expand_dims(np.asarray(self), i).view(Tensor)

    def squeeze(self, i=None):
        return np.asarray(self).squeeze(i).view(Tensor)

    def view(self, *shape):
        if len(shape) == 1 and isinstance(shape[0], type):
            return np.ndarray.view(self, shape[0])
        return np.reshape(np.asarray(self), shape).view(Tensor)

    def repeat(self, *reps):
        return np.tile(np.asarray(self), reps).view(Tensor)

    def fill_(self, v):
        self.fill(v)
        return self


class _Device:
    def __init__(self, kind):
        self.kind = kind


def device(kind):
    return _Device(kind)


def from_numpy(a):
    return np.asarray(a).view(Tensor)


def arange(a, b, device=None, dtype=None):
    return np.arange(a, b, dtype=dtype).view(Tensor)


def exp(x):
    return np.exp(np.asarray(x)).view(Tensor)


def empty(n, dtype=None, device=None):
    if n > cuda.mem_get_info()[0]:
        raise OutOfMemoryError(f"CUDA out of memory. Tried to allocate {n / 2**30:.2f} GiB")
    _peak[0] = max(_peak[0], n)
    return np.zeros(1, dtype=dtype).view(Tensor)    # the size is what counts, not the bytes


class cuda:
    OutOfMemoryError = OutOfMemoryError

    @staticmethod
    def is_available():
        return os.environ.get("FAKE_TORCH_GPU", "1") != "0"

    @staticmethod
    def get_device_name(i=0):
        return "Fake GPU"

    @staticmethod
    def mem_get_info():
        gb = 2**30
        return (int(float(os.environ.get("FAKE_TORCH_FREE_GB", "23")) * gb),
                int(float(os.environ.get("FAKE_TORCH_TOTAL_GB", "24")) * gb))

    @staticmethod
    def reset_peak_memory_stats():
        _peak[0] = 0

    @staticmethod
    def max_memory_allocated():
        return _peak[0]

    @staticmethod
    def synchronize():
        pass


class nn:
    class functional:
        @staticmethod
        def pad(x, pads, mode="replicate"):
            """torch's order: the last dimension first, (before, after) for each."""
            a = np.asarray(x)
            width = [(0, 0)] * a.ndim
            for i in range(len(pads) // 2):
                width[a.ndim - 1 - i] = (pads[2 * i], pads[2 * i + 1])
            return np.pad(a, width, mode="edge").view(Tensor)

        @staticmethod
        def conv2d(x, w, groups=1):
            """Depthwise only (groups == channels), as the blur uses it."""
            x, w = np.asarray(x), np.asarray(w)
            n, c, h, wd = x.shape
            kh, kw = w.shape[2], w.shape[3]
            out = np.zeros((n, c, h - kh + 1, wd - kw + 1), dtype=x.dtype)
            for ch in range(c):
                for i in range(kh):
                    for j in range(kw):
                        out[:, ch] += w[ch, 0, i, j] * x[:, ch, i:i + h - kh + 1, j:j + wd - kw + 1]
            return out.view(Tensor)
