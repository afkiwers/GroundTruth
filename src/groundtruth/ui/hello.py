"""Windows Hello: ask the signed-in user to prove it is them (PIN, fingerprint, face or the security key Windows has
set up for them) before Preferences shows the compute server's access key in clear text (the user, 2026-11-14:
"add an eye icon to display the server key ... guard it behind windows hello ... only if it's correct it will be
shown").

The check is Windows' own UserConsentVerifier (WinRT, Windows 10 and later), called through ctypes so no extra
package is needed in KiCad's Python: RoGetActivationFactory gives the class's IUserConsentVerifierInterop, whose
RequestVerificationForWindowAsync(hwnd, ...) shows the Windows Security dialog owned by our window (the plain
RequestVerificationAsync of a desktop app can open behind it; Microsoft Learn, "UserConsentVerifier Class" and
"IUserConsentVerifierInterop"). Its asynchronous operation is polled through IAsyncInfo until it ends; nothing here
touches wx, and a caller runs verify() off the GUI thread (it blocks until the user answers).

The parameterised interface IDs (IAsyncOperation<UserConsentVerificationResult>, <...Availability>) are derived as
WinRT does: a version-5 UUID (SHA-1) of the type signature in the WinRT pinterface namespace (the WinRT type
system's "GUID generation for parameterized types"); the result's, fd596ffd-2318-558f-9dbe-d21df43764a5, is the one
Windows' headers give."""
from __future__ import annotations

import sys
import time
import uuid
from typing import Optional

CLASS = "Windows.Security.Credentials.UI.UserConsentVerifier"
_PINTERFACE_NS = uuid.UUID("11f47ad5-7b73-42c0-abae-878b1e16adee")   # WinRT's namespace for parameterised IIDs
_IASYNC_OPERATION = "{9fc2b0bb-e446-44e2-aa61-9cab8f636af2}"           # IAsyncOperation`1
IID_STATICS = uuid.UUID("af4f3f91-564c-4ddc-b8b5-973447627c65")       # IUserConsentVerifierStatics
IID_INTEROP = uuid.UUID("39e050c3-4e74-441a-8dc0-b81104df949c")       # IUserConsentVerifierInterop
IID_ASYNC_INFO = uuid.UUID("00000036-0000-0000-c000-000000000046")    # IAsyncInfo

# UserConsentVerificationResult
VERIFIED, DEVICE_NOT_PRESENT, NOT_CONFIGURED, DISABLED_BY_POLICY, DEVICE_BUSY, RETRIES_EXHAUSTED, CANCELED = range(7)
# UserConsentVerifierAvailability: AVAILABLE 0, then the same meanings 1..4 as above
AVAILABLE = 0

RESULT_TEXT = {
    VERIFIED: "verified",
    DEVICE_NOT_PRESENT: "no Windows Hello device (PIN, fingerprint or camera) is present",
    NOT_CONFIGURED: "Windows Hello is not set up for this account (Settings › Accounts › Sign-in options)",
    DISABLED_BY_POLICY: "Windows Hello is turned off by a policy on this computer",
    DEVICE_BUSY: "the Windows Hello device is busy: try again",
    RETRIES_EXHAUSTED: "too many failed attempts",
    CANCELED: "cancelled",
}


def operation_iid(enum_name: str) -> uuid.UUID:
    """The IID of IAsyncOperation<enum_name> for an Int32 WinRT enum (a version-5 UUID of its signature)."""
    sig = f"pinterface({_IASYNC_OPERATION};enum({enum_name};i4))"
    return uuid.uuid5(_PINTERFACE_NS, sig)


IID_OP_RESULT = operation_iid("Windows.Security.Credentials.UI.UserConsentVerificationResult")
IID_OP_AVAILABILITY = operation_iid("Windows.Security.Credentials.UI.UserConsentVerifierAvailability")


def supported() -> bool:
    """Whether this platform can have Windows Hello at all (Windows 10 or later)."""
    return sys.platform == "win32" and sys.getwindowsversion().major >= 10


class _WinRT:
    """The few combase calls and COM vtable calls needed, through ctypes (Windows only)."""

    def __init__(self):
        """Load combase and declare its functions; COM methods are called by vtable index (call)."""
        import ctypes
        from ctypes import wintypes
        self.ct = ctypes
        self.wt = wintypes
        cb = ctypes.WinDLL("combase")
        self.RoInitialize = cb.RoInitialize
        self.RoInitialize.argtypes = [ctypes.c_int]
        self.RoInitialize.restype = ctypes.c_long            # S_FALSE / RPC_E_CHANGED_MODE are not errors here
        self.RoUninitialize = cb.RoUninitialize
        self.RoUninitialize.restype = None
        self.WindowsCreateString = cb.WindowsCreateString
        self.WindowsCreateString.argtypes = [wintypes.LPCWSTR, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        self.WindowsCreateString.restype = ctypes.HRESULT
        self.WindowsDeleteString = cb.WindowsDeleteString
        self.WindowsDeleteString.argtypes = [ctypes.c_void_p]
        self.WindowsDeleteString.restype = ctypes.HRESULT
        self.RoGetActivationFactory = cb.RoGetActivationFactory
        self.RoGetActivationFactory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.RoGetActivationFactory.restype = ctypes.HRESULT

    def guid(self, u: uuid.UUID):
        """A GUID structure (16 bytes, little-endian fields as Windows keeps them) for `u`."""
        return (self.ct.c_ubyte * 16).from_buffer_copy(u.bytes_le)

    def hstring(self, text: str):
        """A new HSTRING of `text` (free it with WindowsDeleteString)."""
        h = self.ct.c_void_p()
        self.WindowsCreateString(text, len(text), self.ct.byref(h))
        return h

    def call(self, obj, index: int, *args, argtypes=()):
        """Call COM method number `index` of the interface pointer `obj` (HRESULT result: a failure raises
        OSError)."""
        ct = self.ct
        vtbl = ct.cast(obj, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
        proto = ct.WINFUNCTYPE(ct.HRESULT, ct.c_void_p, *argtypes)
        return proto(vtbl[index])(obj, *args)

    def release(self, obj) -> None:
        """IUnknown::Release (vtable 2) of a non-null pointer."""
        if obj:
            ct = self.ct
            vtbl = ct.cast(obj, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
            ct.WINFUNCTYPE(ct.c_ulong, ct.c_void_p)(vtbl[2])(obj)

    def query(self, obj, iid: uuid.UUID):
        """IUnknown::QueryInterface (vtable 0): the interface `iid` of `obj` (release it)."""
        out = self.ct.c_void_p()
        self.call(obj, 0, self.ct.byref(self.guid(iid)), self.ct.byref(out),
                  argtypes=(self.ct.c_void_p, self.ct.POINTER(self.ct.c_void_p)))
        return out

    def factory(self, iid: uuid.UUID):
        """The UserConsentVerifier activation factory as interface `iid` (release it)."""
        name = self.hstring(CLASS)
        try:
            out = self.ct.c_void_p()
            self.RoGetActivationFactory(name, self.ct.byref(self.guid(iid)), self.ct.byref(out))
            return out
        finally:
            self.WindowsDeleteString(name)

    def wait_enum(self, op, timeout_s: float, cancelled=None) -> Optional[int]:
        """Poll the IAsyncOperation<enum> `op` until it ends (IAsyncInfo::get_Status, vtable 7: 0 started,
        1 completed, 2 canceled, 3 error), then its GetResults (vtable 8). None when it was cancelled, failed or
        ran out of time (it is then cancelled: IAsyncInfo::Cancel, vtable 9)."""
        ct = self.ct
        info = self.query(op, IID_ASYNC_INFO)
        try:
            end = time.monotonic() + timeout_s
            status = ct.c_int(0)
            while True:
                self.call(info, 7, ct.byref(status), argtypes=(ct.POINTER(ct.c_int),))
                if status.value != 0:
                    break
                if time.monotonic() > end or (cancelled is not None and cancelled()):
                    self.call(info, 9)
                    return None
                time.sleep(0.05)
            if status.value != 1:
                return None
            value = ct.c_int(-1)
            self.call(op, 8, ct.byref(value), argtypes=(ct.POINTER(ct.c_int),))
            return value.value
        finally:
            self.release(info)


def _apartment(rt: _WinRT) -> bool:
    """Join this thread to the multithreaded apartment; True when RoUninitialize is owed (S_OK or S_FALSE)."""
    hr = rt.RoInitialize(1)                         # RO_INIT_MULTITHREADED
    return hr in (0, 1)


def availability(timeout_s: float = 10.0) -> int:
    """UserConsentVerifier.CheckAvailabilityAsync: AVAILABLE (0) or why not (DEVICE_NOT_PRESENT, NOT_CONFIGURED,
    DISABLED_BY_POLICY, DEVICE_BUSY); DEVICE_NOT_PRESENT on a platform without Windows Hello or when the call fails.
    Blocks briefly: call it off the GUI thread."""
    if not supported():
        return DEVICE_NOT_PRESENT
    rt = _WinRT()
    owed = _apartment(rt)
    statics = op = None
    try:
        statics = rt.factory(IID_STATICS)
        op = rt.ct.c_void_p()
        rt.call(statics, 6, rt.ct.byref(op), argtypes=(rt.ct.POINTER(rt.ct.c_void_p),))   # CheckAvailabilityAsync
        got = rt.wait_enum(op, timeout_s)
        return DEVICE_NOT_PRESENT if got is None else got
    except OSError:
        return DEVICE_NOT_PRESENT
    finally:
        rt.release(op)
        rt.release(statics)
        if owed:
            rt.RoUninitialize()


def verify(hwnd: int, message: str, timeout_s: float = 300.0, cancelled=None) -> int:
    """Ask the user to verify with Windows Hello in a Windows Security dialog owned by window `hwnd`, showing
    `message`. VERIFIED (0) only when they did; otherwise one of the other results (CANCELED when they closed it
    or `timeout_s` passed, DEVICE_NOT_PRESENT when Windows has no such call). Blocks until they answer: call it off the GUI thread."""
    if not supported():
        return DEVICE_NOT_PRESENT
    rt = _WinRT()
    ct = rt.ct
    owed = _apartment(rt)
    interop = op = None
    text = None
    try:
        interop = rt.factory(IID_INTEROP)
        text = rt.hstring(message)
        op = ct.c_void_p()
        # RequestVerificationForWindowAsync(HWND, HSTRING, REFIID, void**): vtable 6 (after IInspectable)
        rt.call(interop, 6, rt.wt.HWND(hwnd), text, ct.byref(rt.guid(IID_OP_RESULT)), ct.byref(op),
                argtypes=(rt.wt.HWND, ct.c_void_p, ct.c_void_p, ct.POINTER(ct.c_void_p)))
        got = rt.wait_enum(op, timeout_s, cancelled)
        return CANCELED if got is None else got
    except OSError:                                 # no such class or interface here (older Windows): unavailable
        return DEVICE_NOT_PRESENT
    finally:
        rt.release(op)
        rt.release(interop)
        if text is not None:
            rt.WindowsDeleteString(text)
        if owed:
            rt.RoUninitialize()
