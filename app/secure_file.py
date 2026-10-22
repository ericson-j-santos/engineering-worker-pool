"""Race-resistant file opens shared by POSIX and Windows workers.

The Windows implementation opens the reparse point itself, rather than its
target, and validates the resulting handle before exposing it as a Python file
descriptor.  This is the Win32 analogue of ``O_NOFOLLOW`` for the narrow file
operations used by the worker.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class SecureFileError(RuntimeError):
    """The requested file could not be opened with the required guarantees."""


def is_link_or_reparse(path: Path | str) -> bool:
    """Return true for POSIX links and Windows reparse points/junctions."""
    target = Path(path)
    try:
        info = target.lstat()
    except FileNotFoundError:
        return False
    return target.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0) & 0x00000400
    )


def _validate_fd(fd: int, *, private: bool) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SecureFileError("file_not_private")
    if os.name == "posix" and private:
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SecureFileError("file_not_private")
    if os.name == "nt" and private:
        _validate_windows_private_fd(fd)


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x00000001
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _SE_DACL_PROTECTED = 0x1000
    _ACL_SIZE_INFORMATION = 2
    _ACCESS_ALLOWED_ACE_TYPE = 0
    _ACCESS_DENIED_ACE_TYPES = {1, 6, 10, 12}

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class _AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", wintypes.BYTE),
            ("AceFlags", wintypes.BYTE),
            ("AceSize", wintypes.WORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _create_file.restype = wintypes.HANDLE
    _file_information = _kernel32.GetFileInformationByHandle
    _file_information.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation),
    ]
    _file_information.restype = wintypes.BOOL
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = [wintypes.HANDLE]
    _close_handle.restype = wintypes.BOOL
    _get_current_process = _kernel32.GetCurrentProcess
    _get_current_process.restype = wintypes.HANDLE
    _local_free = _kernel32.LocalFree
    _local_free.argtypes = [wintypes.HLOCAL]
    _local_free.restype = wintypes.HLOCAL

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _open_process_token = _advapi32.OpenProcessToken
    _open_process_token.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    _open_process_token.restype = wintypes.BOOL
    _get_token_information = _advapi32.GetTokenInformation
    _get_token_information.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _get_token_information.restype = wintypes.BOOL
    _convert_sid_to_string = _advapi32.ConvertSidToStringSidW
    _convert_sid_to_string.argtypes = [wintypes.LPVOID,
                                       ctypes.POINTER(wintypes.LPWSTR)]
    _convert_sid_to_string.restype = wintypes.BOOL
    _convert_sddl = _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    _convert_sddl.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _convert_sddl.restype = wintypes.BOOL
    _get_security_info = _advapi32.GetSecurityInfo
    _get_security_info.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    _get_security_info.restype = wintypes.DWORD
    _get_acl_information = _advapi32.GetAclInformation
    _get_acl_information.argtypes = [
        wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.c_int,
    ]
    _get_acl_information.restype = wintypes.BOOL
    _get_ace = _advapi32.GetAce
    _get_ace.argtypes = [
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
    ]
    _get_ace.restype = wintypes.BOOL
    _get_sd_control = _advapi32.GetSecurityDescriptorControl
    _get_sd_control.argtypes = [
        wintypes.LPVOID, ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _get_sd_control.restype = wintypes.BOOL


    def _sid_string(sid: int) -> str:
        rendered = wintypes.LPWSTR()
        if not _convert_sid_to_string(sid, ctypes.byref(rendered)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
        try:
            return rendered.value
        finally:
            _local_free(rendered)


    def _current_user_sid() -> str:
        token = wintypes.HANDLE()
        if not _open_process_token(_get_current_process(), _TOKEN_QUERY,
                                   ctypes.byref(token)):
            raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
        try:
            required = wintypes.DWORD()
            _get_token_information(token, _TOKEN_USER, None, 0,
                                   ctypes.byref(required))
            if not required.value:
                raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
            buffer = ctypes.create_string_buffer(required.value)
            if not _get_token_information(token, _TOKEN_USER, buffer,
                                          required, ctypes.byref(required)):
                raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
            sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
            return _sid_string(sid)
        finally:
            _close_handle(token)


    def _private_security_attributes() -> tuple[_SecurityAttributes, int]:
        user_sid = _current_user_sid()
        descriptor = wintypes.LPVOID()
        sddl = f"O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FA;;;SY)(A;;FA;;;BA)"
        if not _convert_sddl(sddl, 1, ctypes.byref(descriptor), None):
            raise OSError(ctypes.get_last_error(),
                          "ConvertStringSecurityDescriptorToSecurityDescriptorW failed")
        attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes), descriptor, False,
        )
        return attributes, descriptor.value


    def _validate_windows_private_fd(fd: int) -> None:
        owner = wintypes.LPVOID()
        dacl = wintypes.LPVOID()
        descriptor = wintypes.LPVOID()
        result = _get_security_info(
            msvcrt.get_osfhandle(fd), _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner), None, ctypes.byref(dacl), None,
            ctypes.byref(descriptor),
        )
        if result:
            raise OSError(result, "GetSecurityInfo failed")
        try:
            if not owner.value or _sid_string(owner.value) != _current_user_sid():
                raise SecureFileError("file_not_private")
            if not dacl.value:
                raise SecureFileError("file_not_private")
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if (not _get_sd_control(descriptor, ctypes.byref(control),
                                    ctypes.byref(revision))
                    or not control.value & _SE_DACL_PROTECTED):
                raise SecureFileError("file_not_private")
            size = _AclSizeInformation()
            if not _get_acl_information(dacl, ctypes.byref(size),
                                        ctypes.sizeof(size), _ACL_SIZE_INFORMATION):
                raise OSError(ctypes.get_last_error(), "GetAclInformation failed")
            allowed = {_current_user_sid(), "S-1-5-18", "S-1-5-32-544"}
            for index in range(size.AceCount):
                ace = wintypes.LPVOID()
                if not _get_ace(dacl, index, ctypes.byref(ace)):
                    raise OSError(ctypes.get_last_error(), "GetAce failed")
                header = ctypes.cast(ace, ctypes.POINTER(_AceHeader)).contents
                if header.AceType == _ACCESS_ALLOWED_ACE_TYPE:
                    sid = ace.value + 8
                    if _sid_string(sid) not in allowed:
                        raise SecureFileError("file_not_private")
                elif header.AceType not in _ACCESS_DENIED_ACE_TYPES:
                    raise SecureFileError("file_not_private")
        finally:
            _local_free(descriptor)


    def _windows_fd(path: Path, *, write: bool, create: bool) -> int:
        attributes = None
        descriptor = None
        if create:
            attributes, descriptor = _private_security_attributes()
        handle = _create_file(
            str(path), _GENERIC_WRITE if write else _GENERIC_READ,
            0 if write else _FILE_SHARE_READ,
            ctypes.byref(attributes) if attributes is not None else None,
            _CREATE_NEW if create else _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT, None,
        )
        if descriptor:
            _local_free(descriptor)
        if handle == _INVALID_HANDLE_VALUE:
            error = ctypes.get_last_error()
            if error in {80, 183}:
                raise FileExistsError(error, "CreateFileW failed", str(path))
            if error in {2, 3}:
                raise FileNotFoundError(error, "CreateFileW failed", str(path))
            raise OSError(error, "CreateFileW failed", str(path))
        owned = True
        try:
            info = _ByHandleFileInformation()
            if not _file_information(handle, ctypes.byref(info)):
                raise OSError(ctypes.get_last_error(),
                              "GetFileInformationByHandle failed", str(path))
            if info.dwFileAttributes & (_FILE_ATTRIBUTE_DIRECTORY
                                        | _FILE_ATTRIBUTE_REPARSE_POINT):
                raise SecureFileError("file_not_regular")
            if info.nNumberOfLinks != 1:
                raise SecureFileError("file_not_private")
            flags = (os.O_WRONLY if write else os.O_RDONLY) | os.O_BINARY
            fd = msvcrt.open_osfhandle(handle, flags)
            owned = False
            return fd
        finally:
            if owned:
                _close_handle(handle)


def open_exclusive(path: Path | str, mode: int = 0o600) -> int:
    """Create a new regular, single-link file without following links."""
    target = Path(path)
    if os.name == "nt":
        return _windows_fd(target, write=True, create=True)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise SecureFileError("nofollow_unavailable")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, mode)
    try:
        _validate_fd(fd, private=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_regular_read(path: Path | str, *, private: bool = False) -> int:
    """Open an existing regular file without following links."""
    target = Path(path)
    if os.name == "nt":
        fd = _windows_fd(target, write=False, create=False)
    else:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise SecureFileError("nofollow_unavailable")
        fd = os.open(target, os.O_RDONLY | nofollow)
    try:
        _validate_fd(fd, private=private)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_private_read(path: Path | str) -> int:
    """Open an existing private regular file without following links."""
    return open_regular_read(path, private=True)


def validate_private_fd(fd: int) -> None:
    """Revalidate the open handle after the caller has consumed its content."""
    _validate_fd(fd, private=True)


def validate_regular_fd(fd: int) -> None:
    """Revalidate an ordinary, single-link regular file descriptor."""
    _validate_fd(fd, private=False)


def path_matches_fd(fd: int, path: Path | str, *, private: bool = False) -> bool:
    """Verify that a pathname still names the object held by ``fd``."""
    if os.name != "nt":
        return os.path.samestat(os.fstat(fd), os.stat(path, follow_symlinks=False))

    def identity(handle: int) -> tuple[int, int, int]:
        info = _ByHandleFileInformation()
        if not _file_information(handle, ctypes.byref(info)):
            raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
        return (info.dwVolumeSerialNumber, info.nFileIndexHigh, info.nFileIndexLow)

    other = open_regular_read(path, private=private)
    try:
        return identity(msvcrt.get_osfhandle(fd)) == identity(msvcrt.get_osfhandle(other))
    finally:
        os.close(other)
