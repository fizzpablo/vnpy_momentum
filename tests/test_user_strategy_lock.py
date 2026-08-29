import pytest

from user_strategy.instance_lock import InstanceLock


def test_second_local_instance_is_refused(tmp_path):
    first = InstanceLock(tmp_path / "strategy.lock")
    second = InstanceLock(tmp_path / "strategy.lock")
    first.acquire()
    try:
        with pytest.raises(RuntimeError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
