import io
import json
import subprocess
import zipfile
import pytest
from ai_factory.recovery_transport import download


def fake(metadata=None, run=None, archive=None):
    meta = {'workflow_run':{'id':10}, 'size_in_bytes':10, 'expired':False, **(metadata or {})}
    source = {'status':'completed', 'conclusion':'failure', 'head_branch':'main', 'path':'.github/workflows/ai-factory.yml', 'event':'issues',
              'repository':{'full_name':'o/r'}, 'head_repository':{'full_name':'o/r'}, **(run or {})}
    def runner(args, **kw):
        endpoint = args[-1]
        body = archive if endpoint.endswith('/zip') else json.dumps(source if '/runs/' in endpoint else meta).encode()
        return subprocess.CompletedProcess(args, 0, body, b'')
    return runner


@pytest.mark.parametrize('change', [{'conclusion':'success'}, {'status':'in_progress'}, {'head_branch':'attacker'}, {'event':'pull_request'}, {'path':'.github/workflows/other.yml'}, {'head_repository':{'full_name':'attacker/fork'}}])
def test_rejects_wrong_workflow_origin(cfg, tmp_path, change):
    cfg.github_repo = 'o/r'
    with pytest.raises(ValueError, match='Origen'):
        download(cfg, 1, tmp_path/'package', runner=fake(run=change))


def test_zip_traversal_rejected(cfg, tmp_path):
    cfg.github_repo = 'o/r'
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('../escaped', 'bad')
    with pytest.raises(ValueError, match='Ruta'):
        download(cfg, 1, tmp_path/'package', runner=fake(archive=out.getvalue()))
    assert not (tmp_path/'escaped').exists()
