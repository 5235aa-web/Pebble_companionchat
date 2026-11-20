# -*- coding: utf-8 -*-
"""Windows job object that ties every audio.cpp child to this process.

`atexit` only runs when the interpreter is allowed to shut down. A hard kill
(taskkill /F, Task Manager, a crashed parent) skips it entirely and leaves an
audiocpp_server.exe holding its port and its VRAM. A job object created with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE has no such gap: the kernel terminates every
member as soon as the last handle to the job closes, which happens when this
process dies for any reason at all.

The job handle therefore stays open for the whole process lifetime and is never
closed explicitly - leaking it is the mechanism, not an oversight.
"""
import ctypes
import logging
import os

logger = logging.getLogger('audio_cpp_job')

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x0000_2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ('ReadOperationCount', ctypes.c_uint64),
        ('WriteOperationCount', ctypes.c_uint64),
        ('OtherOperationCount', ctypes.c_uint64),
        ('ReadTransferCount', ctypes.c_uint64),
        ('WriteTransferCount', ctypes.c_uint64),
        ('OtherTransferCount', ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_int64),
        ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', ctypes.c_uint32),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', ctypes.c_uint32),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', ctypes.c_uint32),
        ('SchedulingClass', ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ('IoInfo', _IO_COUNTERS),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


_kernel32 = None
# Handle values are pointer-sized; leaving restype at the default c_int would
# truncate them on 64-bit and make every later call fail with a bogus handle.
_job = 0
# True once this process itself is a member of the job. Windows then puts every
# child it spawns into the same job automatically, which is what makes a hard
# kill of the backend take the engines with it even when adopt() never ran.
_self_bound = False


def _lib():
    global _kernel32
    if _kernel32 is not None:
        return _kernel32
    lib = ctypes.WinDLL('kernel32', use_last_error=True)
    lib.CreateJobObjectW.restype = ctypes.c_void_p
    lib.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    lib.SetInformationJobObject.restype = ctypes.c_int
    lib.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ]
    lib.AssignProcessToJobObject.restype = ctypes.c_int
    lib.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.OpenProcess.restype = ctypes.c_void_p
    lib.GetCurrentProcess.restype = ctypes.c_void_p
    lib.GetCurrentProcess.argtypes = []
    lib.IsProcessInJob.restype = ctypes.c_int
    lib.IsProcessInJob.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
    ]
    lib.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    lib.CloseHandle.restype = ctypes.c_int
    lib.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32 = lib
    return lib


def _bind_self(lib, job) -> bool:
    """Put this process in the job so its children are born inside it.

    Assigning the children one by one (adopt) is the fallback, not the plan: it
    can fail with access-denied on a child that already started, and that
    failure used to be a logged warning followed by an orphan. Inheriting is not
    a second chance, it is the mechanism - Windows joins every child of a job
    member to the same job at CreateProcess time.
    """
    try:
        if not lib.AssignProcessToJobObject(job, lib.GetCurrentProcess()):
            logger.info(
                'self-assign to job refused (err=%s); children will need adopt()',
                ctypes.get_last_error(),
            )
            return False
        return True
    except Exception:
        logger.warning('self-assign failed', exc_info=True)
        return False


def _job_handle(lib) -> int:
    global _job, _self_bound
    if _job:
        return _job
    job = lib.CreateJobObjectW(None, None)
    if not job:
        logger.warning('CreateJobObjectW failed (err=%s)', ctypes.get_last_error())
        return 0
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = lib.SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        logger.warning('SetInformationJobObject failed (err=%s)', ctypes.get_last_error())
        lib.CloseHandle(job)
        return 0
    _job = int(job)
    _self_bound = _bind_self(lib, job)
    logger.info(
        'kill-on-exit job created (%s)',
        'this process is a member, children inherit it'
        if _self_bound else 'children will be adopted one by one',
    )
    return _job


def _in_job(lib, handle, job) -> bool:
    inside = ctypes.c_int(0)
    if not lib.IsProcessInJob(handle, job, ctypes.byref(inside)):
        return False
    return inside.value != 0


def is_member(pid: int) -> bool:
    """True when *pid* is a member of our job."""
    if os.name != 'nt':
        return False
    lib = _lib()
    job = _job_handle(lib)
    if not job:
        return False
    handle = lib.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return False
    try:
        return _in_job(lib, handle, job)
    finally:
        lib.CloseHandle(handle)


def ensure() -> bool:
    """Create the job and join it, before anything is spawned.

    Inheritance only happens when the job already exists at CreateProcess time,
    so this must run at startup. A child started before the job existed is
    outside it and only an explicit assignment can pull it in.
    """
    if os.name != 'nt':
        return False
    try:
        return bool(_job_handle(_lib()))
    except Exception:
        logger.warning('job ensure failed', exc_info=True)
        return False


def adopt_pid(pid: int) -> bool:
    """Put an already-running process into the job, addressed by pid.

    Needed for a llama-server this backend did not spawn but chose to reuse: it
    is nobody's child, so nothing binds it automatically, and a hard kill of the
    backend would leave it holding port 8080 and its VRAM.
    """
    if os.name != 'nt' or not pid:
        return False
    try:
        lib = _lib()
        job = _job_handle(lib)
        if not job:
            return False
        handle = lib.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, int(pid))
        if not handle:
            logger.warning('OpenProcess(pid=%s) failed (err=%s)', pid, ctypes.get_last_error())
            return False
        try:
            if _in_job(lib, handle, job):
                return True
            if not lib.AssignProcessToJobObject(job, handle):
                logger.warning(
                    'AssignProcessToJobObject(pid=%s) failed (err=%s)',
                    pid, ctypes.get_last_error(),
                )
                return False
        finally:
            lib.CloseHandle(handle)
        logger.info('pid %s bound to the kill-on-exit job', pid)
        return True
    except Exception:
        logger.warning('job adopt(pid) failed', exc_info=True)
        return False


def adopt(proc) -> bool:
    """Put *proc* into the job so it dies with this process. False on failure.

    Only needed when the self-assignment in `_job_handle` was refused: a child
    of a job member is already inside the job and re-assigning it would fail
    with access-denied for no reason. Failure is never fatal - the port sweep in
    each engine's stop() and the atexit hook cover a graceful exit - but it is
    the difference between an orphan and a clean exit after a hard kill.
    """
    if os.name != 'nt' or proc is None:
        return False
    try:
        if proc.poll() is not None:
            return False
        lib = _lib()
        job = _job_handle(lib)
        if not job:
            return False
        handle = lib.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, proc.pid)
        if not handle:
            logger.warning('OpenProcess(pid=%s) failed (err=%s)', proc.pid, ctypes.get_last_error())
            return False
        try:
            # Membership is verified, never assumed. A child spawned after this
            # process joined the job inherits it, but one started before the job
            # existed - the first engine after a cold start - does not, and
            # skipping the assignment there is exactly how the orphan survived.
            if _in_job(lib, handle, job):
                return True
            if not lib.AssignProcessToJobObject(job, handle):
                logger.warning(
                    'AssignProcessToJobObject(pid=%s) failed (err=%s)',
                    proc.pid, ctypes.get_last_error(),
                )
                return False
        finally:
            lib.CloseHandle(handle)
        logger.info('audio.cpp pid %s bound to the kill-on-exit job', proc.pid)
        return True
    except Exception:
        logger.warning('job adopt failed', exc_info=True)
        return False
