"""Geometry validity checks and a subprocess guard for OCCT operations that can crash.

Some OCCT operations (`offset_3d`, Shell on the pointed-nose loft) segfault instead of
raising. `run_guarded` runs such an operation in a forked child process, so a crash becomes
a `GuardedCrash` exception and the parent keeps running.
"""

import multiprocessing as mp
import signal
import traceback
from collections.abc import Callable
from typing import Any


class GeometryError(ValueError):
    """A shape failed a validity or topology check."""


class GuardedCrash(RuntimeError):
    """The guarded child process died (segfault, abort, kill) or timed out."""


def void_count(shape: Any) -> int:
    """Number of internal voids: shells beyond the outer one, summed over solids."""
    return sum(len(s.shells()) - 1 for s in shape.solids())


def assert_valid(
    shape: Any, solids: int | None = None, shells: int | None = None, what: str = "shape"
) -> None:
    """Raise GeometryError unless `shape` is valid and has the expected solid/shell counts.

    `shells` counts every shell of every solid, so a sealed print section with one cavity
    is `solids=1, shells=2`.
    """
    if shape is None:
        raise GeometryError(f"{what}: no shape")
    if not shape.is_valid:
        raise GeometryError(f"{what}: OCCT reports the shape as invalid")
    n_solids = len(shape.solids())
    n_shells = sum(len(s.shells()) for s in shape.solids())
    if solids is not None and n_solids != solids:
        raise GeometryError(f"{what}: expected {solids} solid(s), found {n_solids}")
    if shells is not None and n_shells != shells:
        raise GeometryError(f"{what}: expected {shells} shell(s), found {n_shells}")
    for solid in shape.solids():
        if solid.volume <= 0:
            raise GeometryError(f"{what}: solid with non-positive volume {solid.volume:.6g}")


def _child(conn: Any, fn: Callable[..., Any], args: tuple, kwargs: dict) -> None:
    try:
        result = fn(*args, **kwargs)
        conn.send(("ok", result))
    except BaseException as exc:  # report everything; the parent re-raises
        conn.send(("err", (type(exc).__name__, str(exc), traceback.format_exc())))
    finally:
        conn.close()


def _context() -> mp.context.BaseContext:
    """`forkserver` with build123d preloaded (fast, and the server is single-threaded, so
    no fork-with-threads deadlock risk); `spawn` where forkserver is unavailable."""
    if "forkserver" in mp.get_all_start_methods():
        ctx = mp.get_context("forkserver")
        ctx.set_forkserver_preload(["build123d"])
        return ctx
    return mp.get_context("spawn")


def run_guarded(fn: Callable[..., Any], *args: Any, timeout: float = 120.0, **kwargs: Any) -> Any:
    """Call `fn(*args, **kwargs)` in a child process and return its (picklable) result.

    `fn` must be picklable (a module-level function) and so must its arguments and result;
    build123d shapes pickle. A Python exception in the child is re-raised as
    `GeometryError`; a crash (segfault, abort) or timeout raises `GuardedCrash`.
    Like any forkserver/spawn child, it re-imports `__main__`: scripts need an
    `if __name__ == "__main__":` guard, and code piped to `python -` cannot use it.
    """
    ctx = _context()
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(child_conn, fn, args, kwargs), daemon=True)
    proc.start()
    child_conn.close()
    name = getattr(fn, "__qualname__", repr(fn))
    message = None
    try:
        if parent_conn.poll(timeout):
            message = parent_conn.recv()
    except EOFError:
        message = None  # child died before sending
    proc.join(5.0 if message is not None else 0.5)
    if proc.is_alive():
        proc.kill()
        proc.join()
        if message is None:
            raise GuardedCrash(f"{name}: timed out after {timeout:g} s")
    if message is None:
        code = proc.exitcode
        if code is not None and code < 0:
            try:
                sig = signal.Signals(-code).name
            except ValueError:
                sig = f"signal {-code}"
            raise GuardedCrash(f"{name}: child process crashed ({sig})")
        raise GuardedCrash(f"{name}: child process exited with code {code} and no result")
    status, payload = message
    if status == "ok":
        return payload
    exc_type, exc_msg, tb = payload
    raise GeometryError(f"{name}: {exc_type}: {exc_msg}\n--- child traceback ---\n{tb}")
