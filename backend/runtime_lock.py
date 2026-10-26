"""Single process per data directory, also used by offline backup."""
import fcntl
from pathlib import Path


def acquire(data_dir):
    handle = (Path(data_dir) / '.runtime.lock').open('a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('此数据目录正在运行或备份；仅允许单实例') from None
    return handle
