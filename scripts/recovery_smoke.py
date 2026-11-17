"""Live two-runner smoke test. Deterministic agents, disposable repo, no model credentials."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_factory.agents import Agents
from ai_factory.brain import Brain
from ai_factory.config import FactoryConfig
from ai_factory.intake import task_from_text
from ai_factory.models import RunStatus
from ai_factory.pipeline import Factory
from tests.conftest import FakeClient, GOOD_FILES, _role


def git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo, text=True).strip()


def fixture(root: Path) -> FactoryConfig:
    root.mkdir(parents=True, exist_ok=True)
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.email', 'recovery-test@example.invalid')
    git(root, 'config', 'user.name', 'Recovery integration test')
    (root / '.gitignore').write_text('.factory/runs/\n__pycache__/\n.pytest_cache/\n')
    (root / 'README.md').write_text('Disposable recovery fixture\n')
    Brain(root / '.factory/brain').init()
    git(root, 'add', '-A')
    # Same base commit in both runners, independent of wall clock.
    env = {**os.environ, 'GIT_AUTHOR_DATE':'2026-01-01T00:00:00Z', 'GIT_COMMITTER_DATE':'2026-01-01T00:00:00Z'}
    subprocess.run(['git','commit','-q','-m','fixture base'], cwd=root, env=env, check=True)
    return FactoryConfig(repo_path=root, github_repo=os.environ['GITHUB_REPOSITORY'],
                         test_command='python -m pytest -q -p no:cacheprovider',
                         allowed_commands=['ls','python','pytest'], push=False,
                         auto_ship='none', issue_feedback=False, retro=False,
                         review_mode='all')


def quota():
    from ai_factory.quota import QuotaExhausted
    raise QuotaExhausted('SIMULATED quota interruption, no external model call',
                         datetime.now(UTC) + timedelta(minutes=5))


class InterruptSecurity(FakeClient):
    def create(self, **kwargs):
        if _role(kwargs['system']) == 'reviewer_security':
            quota()
        return super().create(**kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['prepare','resume'])
    parser.add_argument('scenario', choices=['builder','review'])
    parser.add_argument('--package', type=Path)
    parser.add_argument('--run-id')
    args = parser.parse_args()
    root = Path(os.environ['RUNNER_TEMP']) / ('recovery-fixture-' + args.scenario)
    cfg = fixture(root)
    if args.phase == 'prepare':
        if args.scenario == 'builder':
            def partial(self, spec, ws, brain, feedback):
                ws.write_file('partial_marker.py', 'PRESERVED = "from-first-runner"\n')
                quota()
            Agents.build = partial
        client = InterruptSecurity(files=GOOD_FILES) if args.scenario == 'review' else FakeClient()
        state = Factory(cfg, client).run(task_from_text('Live recovery ' + args.scenario))
        assert state.status == RunStatus.PAUSED, state.error
        assert state.checkpoint_artifact_id, 'The real artifact upload did not return an ID'
        assert state.next_stage == ('build' if args.scenario == 'builder' else 'review')
        if args.scenario == 'review':
            assert {r.reviewer for r in state.reviews} == {'code','ux','ui'}
        metadata = {'run_id':state.run_id,'artifact_id':state.checkpoint_artifact_id,
                    'scenario':args.scenario,'source_runner':os.environ['RUNNER_NAME'],
                    'source_hostname':__import__('socket').gethostname(), 'stage':state.next_stage,
                    'cached_reviewers':[r.reviewer for r in state.reviews]}
        Path('smoke-source.json').write_text(json.dumps(metadata,indent=2))
        with open(os.environ['GITHUB_OUTPUT'],'a') as f:
            f.write(f'run_id={state.run_id}\nartifact_id={state.checkpoint_artifact_id}\n')
        print('SOURCE VERIFIED',json.dumps(metadata))
        return
    assert args.package and args.run_id
    assert not cfg.runs_dir.exists(), 'Resume runner unexpectedly has local execution state'
    original = Agents.build
    if args.scenario == 'builder':
        def complete(self, spec, ws, brain, feedback):
            assert ws.read_file('partial_marker.py') == 'PRESERVED = "from-first-runner"\n'
            assert feedback[0] == 'Continuar trabajo parcial'
            return original(self,spec,ws,brain,feedback)
        Agents.build = complete
    client = FakeClient(files=GOOD_FILES)
    state = Factory(cfg, client).resume(args.run_id, package=args.package)
    assert state.status == RunStatus.SHIPPED, state.error
    assert state.attempt == 2 and state.iterations == 1
    assert 'spec_writer' not in client.calls, client.calls
    if args.scenario == 'review':
        assert not {'builder','reviewer_code','reviewer_ui','reviewer_ux'} & set(client.calls), client.calls
        assert client.calls.count('reviewer_security') == 1
    else:
        assert git(root,'show',state.branch+':partial_marker.py') == 'PRESERVED = "from-first-runner"'
    assert git(root,'log','--oneline','main').count('\n') == 0, 'Fixture base should not be published to'
    assert not git(root,'remote'), 'No remote writes permitted'
    report = {'result':'PASS','scenario':args.scenario,'run_id':state.run_id,
              'attempt':state.attempt,'iterations':state.iterations,
              'resume_runner':os.environ['RUNNER_NAME'],'resume_hostname':__import__('socket').gethostname(),
              'agent_calls':client.calls,'reused_stages':state.reused_stages,
              'real_model_calls':0,'remote_writes':0}
    Path('smoke-result.json').write_text(json.dumps(report,indent=2))
    print('RECOVERY VERIFIED',json.dumps(report))


if __name__ == '__main__':
    main()
