"""Windows Hello behind the access key's eye (ui/hello.py): the parameterised WinRT interface IDs derived as WinRT
does (checked against the value Windows' headers give), and nothing tried where Windows Hello cannot exist."""
import sys
import uuid

import pytest

from groundtruth.ui import hello


def test_the_async_operation_iids_are_winrts():
    # IAsyncOperation<UserConsentVerificationResult>, as windows.security.credentials.ui.h declares it
    assert hello.IID_OP_RESULT == uuid.UUID("fd596ffd-2318-558f-9dbe-d21df43764a5")
    assert hello.IID_OP_AVAILABILITY.version == 5 and hello.IID_OP_AVAILABILITY != hello.IID_OP_RESULT


def test_no_windows_hello_off_windows(monkeypatch):
    monkeypatch.setattr(hello, "supported", lambda: False)
    assert hello.availability() == hello.DEVICE_NOT_PRESENT
    assert hello.verify(0, "x") == hello.DEVICE_NOT_PRESENT


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_the_availability_query_answers_on_windows():
    # the real WinRT call (no dialog): one of its five answers, never an exception
    assert hello.availability() in range(5)
