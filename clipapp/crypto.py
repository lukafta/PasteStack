"""Encrypts everything that gets saved, with a key only this Windows account can open.

A random 256-bit key does the work (AES-256-GCM, from Windows' own crypto library). That
key is stored wrapped with DPAPI, so the database is useless if copied to another account
or machine, read from a stolen disk, or swept up by something that just collects files.

What this can't stop: a program already running under the same Windows account can ask
DPAPI to unwrap the key just as this app does, read this app's memory, or simply read the
clipboard at the moment of a copy. Windows has no way to refuse those.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
import threading
from ctypes import POINTER, Structure, byref, c_char, sizeof
from ctypes import wintypes as w

_ENTROPY = b"ClipApp/1"
_NONCE_SIZE, _TAG_SIZE = 12, 16
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_bcrypt = ctypes.WinDLL("bcrypt")


class LockedError(Exception):
    """The saved key doesn't open here: another Windows account, or the password was reset."""


class DecryptError(Exception):
    """A saved value was damaged or changed: GCM refuses it rather than return garbage."""


class _Blob(Structure):
    _fields_ = [("cbData", w.DWORD), ("pbData", POINTER(c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, POINTER(c_char))), buffer


_crypt32.CryptProtectData.restype = w.BOOL
_crypt32.CryptProtectData.argtypes = [POINTER(_Blob), w.LPCWSTR, POINTER(_Blob), w.LPVOID, w.LPVOID, w.DWORD, POINTER(_Blob)]
_crypt32.CryptUnprotectData.restype = w.BOOL
_crypt32.CryptUnprotectData.argtypes = [POINTER(_Blob), w.LPVOID, POINTER(_Blob), w.LPVOID, w.LPVOID, w.DWORD, POINTER(_Blob)]
_kernel32.LocalFree.restype = w.HLOCAL
_kernel32.LocalFree.argtypes = [w.HLOCAL]


def wrap_key(key: bytes) -> bytes:
    data, _keep = _blob(key)
    entropy, _keep2 = _blob(_ENTROPY)
    out = _Blob()
    if not _crypt32.CryptProtectData(byref(data), "ClipApp", byref(entropy), None, None,
                                     _CRYPTPROTECT_UI_FORBIDDEN, byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, w.HLOCAL))


def unwrap_key(wrapped: bytes) -> bytes:
    data, _keep = _blob(wrapped)
    entropy, _keep2 = _blob(_ENTROPY)
    out = _Blob()
    if not _crypt32.CryptUnprotectData(byref(data), None, byref(entropy), None, None,
                                       _CRYPTPROTECT_UI_FORBIDDEN, byref(out)):
        raise LockedError(f"DPAPI refused the key (error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, w.HLOCAL))


class _AuthInfo(Structure):  # BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO
    _fields_ = [("cbSize", w.ULONG), ("dwInfoVersion", w.ULONG),
                ("pbNonce", ctypes.c_void_p), ("cbNonce", w.ULONG),
                ("pbAuthData", ctypes.c_void_p), ("cbAuthData", w.ULONG),
                ("pbTag", ctypes.c_void_p), ("cbTag", w.ULONG),
                ("pbMacContext", ctypes.c_void_p), ("cbMacContext", w.ULONG),
                ("cbAAD", w.ULONG), ("cbData", ctypes.c_ulonglong), ("dwFlags", w.ULONG)]


_NTSTATUS = ctypes.c_long
_STATUS_AUTH_TAG_MISMATCH = -1073700862  # 0xC000A002
_bcrypt.BCryptOpenAlgorithmProvider.restype = _NTSTATUS
_bcrypt.BCryptOpenAlgorithmProvider.argtypes = [POINTER(w.HANDLE), w.LPCWSTR, w.LPCWSTR, w.ULONG]
_bcrypt.BCryptSetProperty.restype = _NTSTATUS
_bcrypt.BCryptSetProperty.argtypes = [w.HANDLE, w.LPCWSTR, ctypes.c_void_p, w.ULONG, w.ULONG]
_bcrypt.BCryptGenerateSymmetricKey.restype = _NTSTATUS
_bcrypt.BCryptGenerateSymmetricKey.argtypes = [w.HANDLE, POINTER(w.HANDLE), ctypes.c_void_p, w.ULONG,
                                               ctypes.c_void_p, w.ULONG, w.ULONG]
for _fn in (_bcrypt.BCryptEncrypt, _bcrypt.BCryptDecrypt):
    _fn.restype = _NTSTATUS
    _fn.argtypes = [w.HANDLE, ctypes.c_void_p, w.ULONG, ctypes.c_void_p, ctypes.c_void_p, w.ULONG,
                    ctypes.c_void_p, w.ULONG, POINTER(w.ULONG), w.ULONG]


def _check(status: int, action: str) -> None:
    if status != 0:
        raise OSError(f"{action} failed (NTSTATUS {status & 0xFFFFFFFF:#010x})")


class _AesGcm:
    """AES-256-GCM through bcrypt.dll. The window and the engine both use it, so one key
    handle serves one call at a time: ctypes lets go of the GIL during the call."""

    def __init__(self, key: bytes):
        self._lock = threading.Lock()
        self._alg = w.HANDLE()
        self._key = w.HANDLE()
        _check(_bcrypt.BCryptOpenAlgorithmProvider(byref(self._alg), "AES", None, 0), "opening AES")
        mode = ctypes.create_unicode_buffer("ChainingModeGCM")
        _check(_bcrypt.BCryptSetProperty(self._alg, "ChainingMode", mode, sizeof(mode), 0), "selecting GCM")
        secret = ctypes.create_string_buffer(key, len(key))
        _check(_bcrypt.BCryptGenerateSymmetricKey(self._alg, byref(self._key), None, 0, secret, len(key), 0),
               "loading the key")

    @staticmethod
    def _info(nonce, aad, tag) -> _AuthInfo:
        return _AuthInfo(cbSize=sizeof(_AuthInfo), dwInfoVersion=1, pbNonce=ctypes.addressof(nonce), cbNonce=_NONCE_SIZE,
                         pbAuthData=ctypes.addressof(aad), cbAuthData=len(aad) - 1,
                         pbTag=ctypes.addressof(tag), cbTag=_TAG_SIZE)

    def encrypt(self, nonce: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
        nonce_buf = ctypes.create_string_buffer(nonce, _NONCE_SIZE)
        aad_buf = ctypes.create_string_buffer(aad)
        tag = ctypes.create_string_buffer(_TAG_SIZE)
        source = ctypes.create_string_buffer(plaintext, max(1, len(plaintext)))
        target = ctypes.create_string_buffer(max(1, len(plaintext)))
        info = self._info(nonce_buf, aad_buf, tag)
        written = w.ULONG()
        with self._lock:
            status = _bcrypt.BCryptEncrypt(self._key, source, len(plaintext), byref(info), None, 0,
                                           target, len(plaintext), byref(written), 0)
        _check(status, "encrypting")
        return target.raw[:len(plaintext)], tag.raw

    def decrypt(self, nonce: bytes, ciphertext: bytes, tag: bytes, aad: bytes) -> bytes:
        nonce_buf = ctypes.create_string_buffer(nonce, _NONCE_SIZE)
        aad_buf = ctypes.create_string_buffer(aad)
        tag_buf = ctypes.create_string_buffer(tag, _TAG_SIZE)
        source = ctypes.create_string_buffer(ciphertext, max(1, len(ciphertext)))
        target = ctypes.create_string_buffer(max(1, len(ciphertext)))
        info = self._info(nonce_buf, aad_buf, tag_buf)
        written = w.ULONG()
        with self._lock:
            status = _bcrypt.BCryptDecrypt(self._key, source, len(ciphertext), byref(info), None, 0,
                                           target, len(ciphertext), byref(written), 0)
        if status == _STATUS_AUTH_TAG_MISMATCH:
            raise DecryptError("authentication failed")
        _check(status, "decrypting")
        return target.raw[:len(ciphertext)]


class Vault:
    def __init__(self, master_key: bytes):
        self._aes = _AesGcm(hmac.new(master_key, b"clipapp aes", hashlib.sha256).digest())
        self._fingerprint_key = hmac.new(master_key, b"clipapp fingerprint", hashlib.sha256).digest()

    def seal_bytes(self, data: bytes, context: bytes) -> bytes:
        """`context` ties the value to its column, so it can't be moved to another one."""
        nonce = os.urandom(_NONCE_SIZE)
        ciphertext, tag = self._aes.encrypt(nonce, data, context)
        return nonce + tag + ciphertext

    def open_bytes(self, blob: bytes, context: bytes) -> bytes:
        if len(blob) < _NONCE_SIZE + _TAG_SIZE:
            raise DecryptError("too short")
        nonce, tag = blob[:_NONCE_SIZE], blob[_NONCE_SIZE:_NONCE_SIZE + _TAG_SIZE]
        return self._aes.decrypt(nonce, blob[_NONCE_SIZE + _TAG_SIZE:], tag, context)

    def seal(self, text: str, context: bytes) -> bytes:
        return self.seal_bytes(text.encode("utf-8", "surrogatepass"), context)

    def open(self, blob: bytes, context: bytes) -> str:
        return self.open_bytes(blob, context).decode("utf-8", "surrogatepass")

    def fingerprint(self, text: str) -> bytes:
        """Finds repeated text without storing it. Keyed, so it can't be checked against a word list."""
        return hmac.new(self._fingerprint_key, text.encode("utf-8", "surrogatepass"), hashlib.sha256).digest()


def new_master_key() -> bytes:
    return os.urandom(32)


# ---- the optional password -------------------------------------------------------
#
# With a password, the key is sealed twice: by a key made from the password (scrypt, slow on
# purpose, so guessing is slow), then by DPAPI as before. Opening it needs this Windows
# account and the password. A program running in the account can still read the app's
# memory while it is unlocked, but not what is saved while it is locked.

MIN_PASSWORD = 6
_PASSWORD_TAG = b"PW1"
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}


class WrongPassword(Exception):
    pass


def _password_key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)


def wrap_with_password(key: bytes, password: str) -> bytes:
    salt, nonce = os.urandom(16), os.urandom(_NONCE_SIZE)
    ciphertext, tag = _AesGcm(_password_key(password, salt)).encrypt(nonce, key, _PASSWORD_TAG)
    return wrap_key(_PASSWORD_TAG + salt + nonce + tag + ciphertext)


def unwrap_with_password(wrapped: bytes, password: str) -> bytes:
    """Raises LockedError if the account can't open it, WrongPassword for a wrong password."""
    inner = unwrap_key(wrapped)
    if not inner.startswith(_PASSWORD_TAG) or len(inner) < 3 + 16 + _NONCE_SIZE + _TAG_SIZE:
        raise LockedError("not a password-sealed key")
    salt, rest = inner[3:19], inner[19:]
    nonce, tag, ciphertext = rest[:_NONCE_SIZE], rest[_NONCE_SIZE:_NONCE_SIZE + _TAG_SIZE], rest[_NONCE_SIZE + _TAG_SIZE:]
    try:
        return _AesGcm(_password_key(password, salt)).decrypt(nonce, ciphertext, tag, _PASSWORD_TAG)
    except DecryptError:
        raise WrongPassword() from None
