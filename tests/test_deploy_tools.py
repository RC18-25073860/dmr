import importlib.util, sqlite3, tempfile
from pathlib import Path
import pytest
from backend.runtime_lock import acquire
spec=importlib.util.spec_from_file_location('backup',Path(__file__).resolve().parents[1]/'deploy/backup.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)

def test_backup_restore_and_tamper():
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);src=root/'data';src.mkdir()
        with sqlite3.connect(src/'dmr.sqlite3') as db:db.execute('CREATE TABLE proof(value)')
        (src/'files').mkdir();(src/'files/a').write_bytes(b'original');(src/'nas-credentials.key').write_text('test-only')
        b.transfer(src,root/'snapshot',stopped=True)
        b.transfer(root/'snapshot',root/'restored',restore=True,stopped=True)
        assert (root/'restored/files/a').read_bytes()==b'original'
        assert (root/'restored/nas-credentials.key').exists()
        (root/'snapshot/files/a').write_bytes(b'changed')
        with pytest.raises(ValueError):b.transfer(root/'snapshot',root/'bad',True,True)
        with pytest.raises(ValueError):b.transfer(src,src/'nested',stopped=True)
        with pytest.raises(ValueError):b.transfer(src,root/'restored',stopped=True)
        with pytest.raises(ValueError):b.transfer(src,root/'without-stop')
        lock=acquire(src)
        try:
            with pytest.raises(RuntimeError):b.transfer(src,root/'active',stopped=True)
        finally:lock.close()
