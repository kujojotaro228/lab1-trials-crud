#!/usr/bin/env python3
"""torchrun for Windows builds of PyTorch (no libuv, no POSIX signals).

Same as `python -m torch.distributed.run ...` (same arguments, same elastic agent), with one
change: the TCPStore that the static rendezvous creates in the agent is created with
use_libuv=False. On Windows, PyTorch is built without libuv, and the static rendezvous
(--rdzv-backend=static, required for the restart run of step 5) ignores USE_LIBUV=0, so plain
torchrun fails with "use_libuv was requested but PyTorch was built without libuv support".
Worker processes create their store themselves with use_libuv=False (train.py, init_group).
"""
from __future__ import annotations

import importlib
import os

import torch.distributed as dist

_Orig = dist.TCPStore


def _make_patched():
    try:
        class TCPStoreNoUV(_Orig):          # subclass keeps isinstance(store, TCPStore) working
            def __init__(self, *args, **kwargs):
                kwargs["use_libuv"] = False
                super().__init__(*args, **kwargs)
        return TCPStoreNoUV
    except TypeError:                       # class not subclassable: plain factory function
        def tcp_store_no_uv(*args, **kwargs):
            kwargs["use_libuv"] = False
            return _Orig(*args, **kwargs)
        return tcp_store_no_uv


_Patched = _make_patched()
_patched = []
for name in ("torch.distributed.elastic.rendezvous.static_tcp_rendezvous",
             "torch.distributed.elastic.rendezvous.c10d_rendezvous_backend"):
    try:
        mod = importlib.import_module(name)
    except ImportError:
        continue
    if getattr(mod, "TCPStore", None) is _Orig:
        mod.TCPStore = _Patched
        _patched.append(name)
if "torch.distributed.elastic.rendezvous.static_tcp_rendezvous" not in _patched:
    dist.TCPStore = _Patched                # the module looks TCPStore up through torch.distributed
    _patched.append("torch.distributed")
# 2) Stopping workers. On Windows the agent stops the surviving workers with CTRL_C_EVENT.
#    GenerateConsoleCtrlEvent delivers it to every process of the console (the agent itself
#    and the script that started torchrun get SIGINT), and a worker blocked in a gloo
#    collective does not react to it, so the restart hangs. Here a worker is stopped with
#    TerminateProcess (Popen.kill), the same as SIGKILL on Linux.
try:
    from torch.distributed.elastic.multiprocessing.subprocess_handler import subprocess_handler as _sh
    _orig_close = _sh.SubprocessHandler.close

    def _close_kill(self, death_sig=None):
        if os.name == "nt":
            if self.proc.poll() is None:
                self.proc.kill()
            return
        return _orig_close(self, death_sig) if death_sig is not None else _orig_close(self)

    _sh.SubprocessHandler.close = _close_kill
    _patched.append("SubprocessHandler.close -> kill on Windows")
except (ImportError, AttributeError) as exc:
    print(f"[torchrun_win] could not patch worker shutdown: {exc!r}", flush=True)

# the same for code paths that send a signal themselves: SIGTERM on Windows = TerminateProcess
if os.name == "nt":
    import signal as _signal
    try:
        from torch.distributed.elastic.multiprocessing import api as _api
        for _fn in ("_get_default_signal", "_get_kill_signal"):
            if hasattr(_api, _fn):
                setattr(_api, _fn, lambda: _signal.SIGTERM)
                _patched.append(f"api.{_fn} -> SIGTERM")
    except ImportError:
        pass

print(f"[torchrun_win] patched: {', '.join(_patched)}", flush=True)

if __name__ == "__main__":
    from torch.distributed.run import main
    main()
