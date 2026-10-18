"""Shared test-only own-child tracer; raw waits and cleanup have one owner."""
import ctypes
import json
import os
from pathlib import Path
import select
import signal
import stat as stat_module
import fcntl
import struct
import subprocess
import sys
import tempfile
import time

libc = ctypes.CDLL(None, use_errno=True)
libc.ptrace.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
libc.ptrace.restype = ctypes.c_long


def ptrace(request, pid, address=0, data=0):
    ctypes.set_errno(0)
    result = libc.ptrace(request, pid, address, data)
    if result == -1:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def emit(value):
    payload = (json.dumps(value) + '\n').encode()
    assert len(payload) < 4000
    assert os.write(1, payload) == len(payload)


def worker(path, variant):
    if variant == 'early_exit':
        raise SystemExit(17)
    if variant == 'symlink':
        real = Path(str(path)+'.real')
        real.touch()
        path.symlink_to(real)
        fd = os.open(path, os.O_RDWR)
    else:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    if variant == 'preexisting_thread':
        import threading
        threading.Thread(target=lambda: time.sleep(15), daemon=True).start()
    try:
        os.write(fd, b'owned generic syscall capability probe\n')
        if variant=='kernel_sync_error':
            os.close(fd)
            fd=os.open(path,os.O_PATH)
        stat = os.fstat(fd)
        ptrace(0, 0)
        os.kill(os.getpid(), signal.SIGSTOP)
        emit({'phase': 'BOOT', 'pid': os.getpid() + (variant == 'wrong_pid'),
              'fd': fd + (variant == 'wrong_fd'), 'path': str(path),
              'device': stat.st_dev, 'inode': stat.st_ino + (variant == 'wrong_inode')})
        if variant == 'fd_reopen_attempt':
            os.close(fd)
            fd = os.open(path, os.O_RDWR)
        if variant == 'dup2_attempt':
            os.dup2(1, fd)
        marker = {'phase': 'ARM', 'pid': os.getpid()}
        if variant == 'done_before_arm': marker['phase']='DONE'
        if variant == 'malformed_marker':
            os.write(1,b'not JSON\n')
        elif variant == 'coalesced_marker':
            os.write(1,((json.dumps(marker)+'\n')*2).encode())
        elif variant == 'unterminated_marker':
            os.write(1,json.dumps(marker).encode())
        else:
            emit(marker)
        if variant == 'duplicate_arm': emit(marker)
        if variant == 'unexpected_signal':
            os.kill(os.getpid(), signal.SIGUSR1)
        if variant == 'unexpected_fork':
            raise AssertionError('fork syscall was allowed') if os.fork() else SystemExit(91)
        if variant == 'timeout':
            time.sleep(5)
        if variant == 'missing_sync':
            emit({'phase':'DONE','pid':os.getpid()})
            return
        os.fsync(fd)
        os.fdatasync(fd)
        emit({'phase': 'DONE', 'pid': os.getpid()})
    finally:
        os.close(fd)


def run_traced(argv, path, policy, *, mode='observe', variant='normal', target=74, cleanup_records=None,
               deadline_seconds=10):
    assert mode in ('observe','before','after')
    proc = subprocess.Popen(argv,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + (.3 if variant == "timeout" else deadline_seconds)
    terminal = None
    events = []
    raw_sync_entries = []
    stops = 0
    pending = None
    arm = False
    done = False
    killed = False
    bootstrap_exit = False
    boot = None
    bound_fd = None
    cleanup_fault_injected = False

    def wait():
        nonlocal terminal
        while time.monotonic() < deadline:
            try:
                pid, status = os.waitpid(proc.pid, os.WNOHANG | os.WUNTRACED)
            except InterruptedError:
                continue
            if pid:
                assert pid == proc.pid
                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    terminal = status
                    proc.returncode = os.waitstatus_to_exitcode(status)
                return status
            time.sleep(.001)
        raise TimeoutError('bounded own-child wait expired')

    def message():
        ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline-time.monotonic()))
        assert ready, 'missing complete child marker'
        data = os.read(proc.stdout.fileno(), 4000)
        assert data.endswith(b'\n') and data.count(b'\n') == 1, 'ambiguous marker frame'
        value = json.loads(data)
        assert type(value) is dict and type(value.get('pid')) is int and value['pid'] == proc.pid, 'wrong marker PID'
        assert type(value.get('phase')) is str, 'invalid marker phase type'
        return value

    def topology():
        assert sorted(int(x.name) for x in Path(f'/proc/{proc.pid}/task').iterdir()) == [proc.pid], 'unsupported preexisting/concurrent thread'

    def bind(value):
        nonlocal bound_fd
        assert set(value)=={'phase','pid','fd','path','device','inode'} | policy.boot_extra, 'wrong BOOT schema'
        assert type(value['fd']) is int and value['fd']>=3, 'invalid BOOT fd'
        assert value['path']==str(path), 'wrong independently expected path'
        assert stat_module.S_ISREG(path.lstat().st_mode), 'expected path is not regular file'
        bound_fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        owned=os.fstat(bound_fd)
        assert type(value['device']) is int and type(value['inode']) is int, 'invalid inode/device types'
        assert (owned.st_dev,owned.st_ino)==(value['device'],value['inode']), 'child inode differs from parent binding'
        identity(value,value['fd'])
        policy.on_boot(value)

    def identity(boot, fd):
        topology()
        assert fd == boot['fd'], 'unexpected sync FD'
        link = Path(f'/proc/{proc.pid}/fd/{fd}')
        stat = link.stat()
        observed = {'path': os.readlink(link), 'device': stat.st_dev, 'inode': stat.st_ino}
        assert observed == {k: boot[k] for k in observed}, 'FD path/device/inode changed'
        owned=os.fstat(bound_fd)
        assert (stat.st_dev,stat.st_ino)==(owned.st_dev,owned.st_ino), 'parent binding differs'
        return observed

    def read_bound(maximum=2*1024*1024):
        before=os.fstat(bound_fd)
        assert stat_module.S_ISREG(before.st_mode) and 0<before.st_size<=maximum, 'bound file byte budget'
        data=os.pread(bound_fd,before.st_size,0)
        after=os.fstat(bound_fd)
        assert len(data)==before.st_size and (before.st_dev,before.st_ino,before.st_size)==(after.st_dev,after.st_ino,after.st_size), 'unstable bound file'
        return data

    def remaining():
        value=deadline-time.monotonic()
        if value<=0: raise TimeoutError('bounded hook deadline')
        return value

    try:
        status = wait()
        assert os.WIFSTOPPED(status) and os.WSTOPSIG(status) == signal.SIGSTOP, 'missing initial SIGSTOP'
        topology()
        # TRACESYSGOOD and EXITKILL are set before any qualifying syscall stop.
        ptrace(0x4200, proc.pid, 0, 1 | (1 << 20))
        while terminal is None:
            assert stops < 10000, 'syscall bound exceeded'
            ptrace(24, proc.pid)
            status = wait()
            if terminal is not None:
                break
            assert os.WIFSTOPPED(status)
            assert os.WSTOPSIG(status) == (signal.SIGTRAP | 0x80), 'unexpected signal/event stop'
            assert status >> 16 == 0, 'unexpected topology/exec event'
            info = ctypes.create_string_buffer(128)
            count = ptrace(0x420e, proc.pid, len(info), ctypes.addressof(info))
            assert 24 <= count <= len(info), 'unsupported syscall info length'
            kind, reserved, flags, arch = struct.unpack_from('=BBHI', info.raw)
            # Explicitly synthetic observation controls, never reported as kernel errors.
            if variant == 'wrong_length' and arm: count=23
            if variant == 'large_length' and arm: count=129
            if variant == 'unknown_op' and arm: kind=3
            if variant == 'wrong_arch' and arm:
                arch = 0
            if variant == 'unmatched_exit' and arm and kind == 1:
                kind = 2
                struct.pack_into('=qB', info, 24, 0, 0)
            if variant == 'duplicate_entry' and arm and kind == 2 and pending and pending['number'] == 74:
                kind = 1
                count = 80
            assert 24<=count<=len(info), 'unsupported syscall info length'
            assert not reserved and not flags and arch == 0xc000003e, 'unsupported syscall ABI'
            stops += 1
            if kind == 1:
                assert count >= 80 and pending is None, 'unmatched syscall entry'
                number, *args = struct.unpack_from('=7Q', info.raw, 24)
                if variant=='x32' and arm: number|=0x40000000
                assert number < 0x40000000, 'x32/unsupported syscall ABI'
                assert number not in (56, 57, 58, 59, 322, 435), 'fork/clone/exec is unsupported'
                if boot is not None and not done:
                    watched=boot['fd']
                    bad_close=number==3 and args[0]==watched
                    bad_replace=number in (33,292) and args[1]==watched
                    bad_dup=number==32 and args[0]==watched
                    bad_fcntl=number==72 and args[0]==watched and args[1] in (fcntl.F_DUPFD,fcntl.F_DUPFD_CLOEXEC)
                    bad_range=number==436 and args[0]<=watched<=args[1]
                    assert not (bad_close or bad_replace or bad_dup or bad_fcntl or bad_range), 'watched descriptor lifetime change'
                if variant=='controller_entry' and arm and number==target:
                    raise RuntimeError('injected controller exception at entry')
                pending = {'number': number, 'args': args}
                if number in (74, 75):
                    if done:
                        assert policy.allow_post_done_sync, 'sync after DONE'
                        pending['excluded']=True
                        events.append({'phase':'excluded-post-DONE-entry','syscall':number,'fd':args[0]})
                        continue
                    assert arm, 'sync before ARM'
                    link=Path(f'/proc/{proc.pid}/fd/{args[0]}')
                    observed_stat=link.stat()
                    raw_sync_entries.append({'ordinal':len(raw_sync_entries)+1,'syscall':number,'fd':args[0],
                        'path':os.readlink(link),'device':observed_stat.st_dev,'inode':observed_stat.st_ino,
                        'regular':stat_module.S_ISREG(observed_stat.st_mode)})
                    observed = identity(boot, args[0])
                    qualification=policy.on_entry(number,read_bound,remaining)
                    assert type(qualification) is dict and type(qualification['eligible']) is bool
                    pending['qualification']=qualification
                    events.append({'phase': 'entry', 'syscall': number, 'fd': args[0],
                                   'ordinal':len(raw_sync_entries),**observed,**qualification})
                    if mode == 'before' and qualification['eligible']:
                        os.kill(proc.pid, signal.SIGKILL)
                        killed = True
                        break
            elif kind == 2:
                assert count >= 33
                value, error = struct.unpack_from('=qB', info.raw, 24)
                if variant in ('failed_sync_result','restarted_sync') and pending and pending['number']==74:
                    value,error=(-512 if variant=='restarted_sync' else -5),1
                assert pending is not None, 'unmatched syscall exit; bootstrap allowance removed'
                number, args = pending['number'], pending['args']
                if number in (74, 75):
                    if pending.get('excluded'):
                        events.append({'phase':'excluded-post-DONE-exit','syscall':number,'fd':args[0],
                                       'return':value,'is_error':error})
                        pending=None
                        continue
                    observed = identity(boot, args[0])
                    events.append({'phase': 'exit', 'syscall': number, 'fd': args[0],
                                   'return': value, 'is_error': error,
                                   'observation_source':'synthetic' if variant in ('failed_sync_result','restarted_sync') else 'kernel', **observed})
                    assert value == 0 and error == 0, 'sync did not return successfully'
                    if variant=='controller_exit' and number==target:
                        raise RuntimeError('injected controller exception at exit')
                    policy.on_exit(pending['qualification'],read_bound,remaining)
                    if mode == 'after' and pending['qualification']['eligible']:
                        os.kill(proc.pid, signal.SIGKILL)
                        killed = True
                        break
                elif number == 1 and args[0] == 1:
                    assert value > 0 and not error, 'marker write failed'
                    marker = message()
                    if marker['phase'] == 'BOOT':
                        assert boot is None and not arm and not done, 'duplicate BOOT'
                        bind(marker)
                        boot=marker
                    elif marker['phase'] == 'ARM':
                        assert set(marker)=={'phase','pid'}, 'wrong ARM schema'
                        assert boot is not None and not arm and not done, 'duplicate or out-of-phase ARM'
                        topology()
                        policy.on_arm(read_bound,remaining)
                        arm = True
                        if variant in ('controller_arm','cleanup_esrch','cleanup_eintr'):
                            raise RuntimeError('injected controller exception at ARM')
                    else:
                        assert set(marker)=={'phase','pid'}, 'wrong DONE schema'
                        assert marker['phase'] == 'DONE' and arm and not done, 'duplicate or out-of-phase DONE'
                        done = True
                pending = None
            else:
                raise AssertionError('unknown/seccomp syscall stop')
        if killed:
            while terminal is None:
                wait()
            assert os.WIFSIGNALED(terminal) and os.WTERMSIG(terminal) == signal.SIGKILL
        else:
            assert mode == 'observe' and done and proc.returncode == 0
        policy.finish(events,mode)
        eligible_entries = [e for e in events if e.get('eligible')]
        actual_target = eligible_entries[0]['syscall'] if eligible_entries else None
        result = {'mode': mode, 'variant': variant, 'target_syscall':actual_target, 'actual_pid': proc.pid, 'returncode': proc.returncode,
                  'raw_wait_status': terminal, 'bootstrap_exit_observed': bootstrap_exit,
                  'typed_syscall_stops': stops, 'events': events,
                  'raw_sync_entries':raw_sync_entries}
        return result
    finally:
        original_error=sys.exception()
        cleanup_errors=[]
        stderr=b''
        try:
            cleanup_deadline=time.monotonic()+3
            while terminal is None and time.monotonic()<cleanup_deadline:
                try:
                    if variant=='cleanup_esrch' and not cleanup_fault_injected:
                        cleanup_fault_injected=True
                        raise ProcessLookupError('synthetic one-shot ESRCH')
                    os.kill(proc.pid,signal.SIGKILL)
                except (ProcessLookupError,InterruptedError):
                    pass  # Not proof of death: an actual terminal wait is still required.
                try:
                    if variant=='cleanup_eintr' and not cleanup_fault_injected:
                        cleanup_fault_injected=True
                        raise InterruptedError('synthetic one-shot EINTR')
                    pid,status=os.waitpid(proc.pid,os.WNOHANG|os.WUNTRACED)
                except InterruptedError:
                    continue
                except ChildProcessError as error:
                    cleanup_errors.append(error)
                    break  # No invented returncode when terminal status is unavailable.
                if pid and (os.WIFEXITED(status) or os.WIFSIGNALED(status)):
                    terminal=status
                    proc.returncode=os.waitstatus_to_exitcode(status)
                else:
                    time.sleep(.001)
            if terminal is None:
                cleanup_errors.append(RuntimeError('own-child cleanup has no terminal wait status'))
        except BaseException as error:
            cleanup_errors.append(error)
        finally:
            try:
                os.set_blocking(proc.stderr.fileno(),False)
                try: stderr=os.read(proc.stderr.fileno(),8192)
                except BlockingIOError: pass
            except BaseException as error:
                cleanup_errors.append(error)
            for pipe in (proc.stdout,proc.stderr):
                try: pipe.close()
                except BaseException as error: cleanup_errors.append(error)
            if bound_fd is not None:
                try: os.close(bound_fd)
                except BaseException as error: cleanup_errors.append(error)
            if cleanup_records is not None:
                cleanup_records.append({'pid':proc.pid,'raw_wait_status':terminal,
                    'returncode':proc.returncode,'actual_sigkill':terminal is not None and os.WIFSIGNALED(terminal) and os.WTERMSIG(terminal)==signal.SIGKILL,
                    'cleanup_fault_injected':cleanup_fault_injected,
                    'stderr_prefix':stderr.decode(errors='replace'),
                    'cleanup_errors':[repr(e) for e in cleanup_errors], 'observed_sync_events':list(events), 'raw_sync_entries':list(raw_sync_entries)})
        if cleanup_errors:
            raise BaseExceptionGroup('controller and cleanup failures',
                ([original_error] if original_error is not None else [])+cleanup_errors)

class GenericPolicy:
    boot_extra = set()
    allow_post_done_sync = False

    def __init__(self,target): self.target=target
    def on_boot(self,marker): pass
    def on_arm(self,read_bound,remaining): pass
    def on_entry(self,number,read_bound,remaining): return {'eligible':number==self.target}
    def on_exit(self,qualification,read_bound,remaining): pass
    def finish(self,events,mode):
        if mode=='observe':
            assert [(e['phase'],e['syscall']) for e in events]==[('entry',74),('exit',74),('entry',75),('exit',75)], 'expected sync sequence missing'


def run(mode, variant='normal', cleanup_records=None, target=74):
    with tempfile.TemporaryDirectory(prefix='sprintctl-ptrace-owned-') as directory:
        path=Path(directory)/'owned-file'
        argv=[sys.executable,'-B',__file__,'--worker',str(path),variant]
        return run_traced(argv,path,GenericPolicy(target),mode=mode,variant=variant,
                          target=target,cleanup_records=cleanup_records)


if __name__=='__main__':
    assert sys.argv[1]=='--worker'
    worker(Path(sys.argv[2]),sys.argv[3])
