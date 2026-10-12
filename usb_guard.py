"""在 ADB 进程上叠加 seccomp，拒绝 USBDEVFS_RESET；保留 Docker 原过滤器。"""
import ctypes
import errno
import os
import platform

USBDEVFS_RESET = 0x5514  # Linux UAPI: _IO('U', 20)


def server_args(port=5037):
    # -a 控制全接口监听；server 的 -L 不接受指定 hostname。
    return ['adb', '-a', '-L', 'tcp:%d' % port, 'server', 'nodaemon']


class Instruction(ctypes.Structure):
    _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint32)]


class Program(ctypes.Structure):
    _fields_ = [('length', ctypes.c_ushort), ('instructions', ctypes.POINTER(Instruction))]


def install():
    if platform.system() != 'Linux' or platform.machine() != 'x86_64' or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError('USB reset guard requires Linux x86_64; refusing unguarded ADB')
    # seccomp_data: nr@0, arch@4, args[1]@24。x86_64 为小端；ioctl request 按低 32 位解释。
    # 错误 ABI 直接拒绝；同时覆盖 x86_64 ioctl(16) 和 x32 ioctl（__X32_SYSCALL_BIT + 514）。
    instructions = (Instruction * 11)(
        Instruction(0x20, 0, 0, 4),                  # load arch
        Instruction(0x15, 1, 0, 0xc000003e),        # AUDIT_ARCH_X86_64
        Instruction(0x06, 0, 0, 0x80000000),        # KILL_PROCESS
        Instruction(0x20, 0, 0, 0),                # load syscall nr
        Instruction(0x15, 2, 0, 16),
        Instruction(0x15, 1, 0, 0x40000202),
        Instruction(0x06, 0, 0, 0x7fff0000),        # ALLOW other syscalls
        Instruction(0x20, 0, 0, 24),               # load ioctl request
        Instruction(0x15, 0, 1, USBDEVFS_RESET),
        Instruction(0x06, 0, 0, 0x00050000 | errno.EPERM),
        Instruction(0x06, 0, 0, 0x7fff0000),
    )
    program = Program(len(instructions), instructions)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    libc.prctl.restype = ctypes.c_int
    for operation, argument, pointer in ((38, 1, 0), (22, 2, ctypes.addressof(program))):
        if libc.prctl(operation, argument, pointer, 0, 0):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))


if __name__ == '__main__':
    install()
    os.execvp('adb', server_args())
