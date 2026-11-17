from dataclasses import replace
from datetime import datetime, UTC, timedelta
import json
from pathlib import Path
import shutil

import pytest

from ai_factory import checkpoints
from ai_factory.agents import Agents
from ai_factory.intake import task_from_text
from ai_factory.models import RunStatus, Spec
from ai_factory.pipeline import Factory
from ai_factory.quota import QuotaExhausted
from ai_factory.review_policy import select_reviewers, active_reviewers
from .conftest import FakeClient, GOOD_FILES, git


def fail_quota():
    raise QuotaExhausted('usage limit', datetime.now(UTC) + timedelta(hours=1))


def package(cfg, state):
    root = cfg.runs_dir / state.run_id / 'checkpoints'
    return root / json.loads((root / 'latest.json').read_text())['generation']


def test_partial_builder_survives_fresh_clone(cfg, repo, tmp_path, monkeypatch):
    original = Agents.build
    def partial(self, spec, ws, brain, feedback):
        ws.write_file('partial.py', 'VALUE = 123\n')
        fail_quota()
    monkeypatch.setattr(Agents, 'build', partial)
    first = Factory(cfg, FakeClient(), log=lambda *_: None).run(task_from_text('add'))
    assert first.status == RunStatus.PAUSED
    assert first.iterations == 1
    saved = tmp_path / 'saved'
    shutil.copytree(package(cfg, first), saved)
    clone = tmp_path / 'clone'
    git(tmp_path, 'clone', str(repo), str(clone))
    git(clone, 'config', 'user.name', 'test')
    git(clone, 'config', 'user.email', 'test@example.com')
    cfg2 = replace(cfg, repo_path=clone)
    def complete(self, spec, ws, brain, feedback):
        assert ws.read_file('partial.py') == 'VALUE = 123\n'
        assert 'Continuar trabajo parcial' in feedback
        return original(self, spec, ws, brain, feedback)
    monkeypatch.setattr(Agents, 'build', complete)
    client = FakeClient(files=GOOD_FILES)
    resumed = Factory(cfg2, client, log=lambda *_: None).resume(first.run_id, package=saved)
    assert resumed.status == RunStatus.SHIPPED, resumed.error
    assert resumed.iterations == 1 and resumed.attempt == 2
    assert 'spec_writer' not in client.calls
    assert (clone / 'partial.py').read_text() == 'VALUE = 123\n'


def test_individual_review_results_survive_quota(cfg, monkeypatch):
    from .conftest import _role
    class FailSecurity(FakeClient):
        def create(self, **p):
            if _role(p['system']) == 'reviewer_security':
                fail_quota()
            return super().create(**p)
    f = Factory(cfg, FailSecurity(files=GOOD_FILES), log=lambda *_: None)
    first = f.run(task_from_text('add'))
    assert first.status == RunStatus.PAUSED
    assert {r.reviewer for r in first.reviews} == {'code', 'ui', 'ux'}
    client = FakeClient(files=GOOD_FILES)
    resumed = Factory(cfg, client, log=lambda *_: None).resume(first.run_id)
    assert resumed.status == RunStatus.SHIPPED, resumed.error
    assert not {'spec_writer', 'builder', 'reviewer_ui', 'reviewer_ux', 'reviewer_code'} & set(client.calls)
    assert 'reviewer_security' in client.calls
    assert len(resumed.reviews) == 4
    assert 'resume_tests' in (cfg.runs_dir / first.run_id / 'events.jsonl').read_text()


def test_changed_requirements_invalidate_reviews(cfg, monkeypatch):
    original = Agents.gate
    monkeypatch.setattr(Agents, 'gate', lambda *a, **kw: fail_quota())
    task = task_from_text('add')
    first = Factory(cfg, FakeClient(files=GOOD_FILES), log=lambda *_: None).run(task)
    monkeypatch.setattr(Agents, 'gate', original)
    client = FakeClient(files=GOOD_FILES)
    task = task.model_copy(update={'body': 'new acceptance criterion'})
    resumed = Factory(cfg, client, log=lambda *_: None).resume(first.run_id, task=task)
    assert resumed.status == RunStatus.SHIPPED, resumed.error
    assert 'spec_writer' in client.calls and 'reviewer_code' in client.calls


def test_changed_base_invalidates_reviews(cfg, repo, monkeypatch):
    original = Agents.gate
    monkeypatch.setattr(Agents, 'gate', lambda *a, **kw: fail_quota())
    first = Factory(cfg, FakeClient(files=GOOD_FILES), log=lambda *_: None).run(task_from_text('add'))
    (repo / 'other.txt').write_text('another change')
    git(repo, 'add', 'other.txt'); git(repo, 'commit', '-m', 'other')
    monkeypatch.setattr(Agents, 'gate', original)
    client = FakeClient(files=GOOD_FILES)
    resumed = Factory(cfg, client, log=lambda *_: None).resume(first.run_id)
    assert resumed.status == RunStatus.SHIPPED, resumed.error
    assert 'reviewer_security' in client.calls and 'spec_writer' in client.calls


def test_corrupt_checkpoint_is_rejected(cfg, monkeypatch):
    monkeypatch.setattr(Agents, 'build', lambda *a, **kw: fail_quota())
    first = Factory(cfg, FakeClient(), log=lambda *_: None).run(task_from_text('add'))
    p = package(cfg, first)
    (p / 'state.json').write_text('{}')
    with pytest.raises(ValueError, match='corrupto'):
        Factory(cfg, FakeClient()).resume(first.run_id)


def test_failure_to_save_keeps_worktree(cfg, monkeypatch):
    def broken(*a, **kw):
        raise OSError('disk full')
    monkeypatch.setattr(checkpoints, 'save', broken)
    result = Factory(cfg, FakeClient(), log=lambda *_: None).run(task_from_text('add'))
    assert 'worktree conservado' in result.error
    assert (cfg.worktrees_dir / result.branch.replace('/', '-')).exists()


def test_lock_prevents_duplicate_execution(cfg):
    with checkpoints.task_lock(cfg, 'task'):
        with pytest.raises(RuntimeError, match='activa'):
            with checkpoints.task_lock(cfg, 'task'):
                pass


def test_no_automatic_repeat_of_uncertain_publication(cfg, monkeypatch):
    def uncertain(*args, **kwargs):
        raise RuntimeError('lost response after push')
    monkeypatch.setattr(Factory, '_ship', uncertain)
    state = Factory(cfg, FakeClient(files=GOOD_FILES), log=lambda *_: None).run(task_from_text('add'))
    assert state.status == RunStatus.RECOVERABLE
    with pytest.raises(ValueError, match='Publicación iniciada'):
        Factory(cfg, FakeClient()).resume(state.run_id)


def spec(**kw):
    return Spec(summary='x', problem='x', acceptance_criteria=[], technical_approach='x', test_plan=[], **kw)


@pytest.mark.parametrize('files', [['src/app/api/notify/route.ts'], ['package-lock.json'], ['src/components/a.tsx'], ['unknown.xyz'], ['docs/help.md']])
def test_uncertain_and_indirect_changes_keep_all_reviewers(files):
    selection = select_reviewers(files, spec())
    assert len(active_reviewers(selection, 'selective')) == 4


def test_internal_notes_selection_observation_and_escalation():
    selection = select_reviewers(['.factory/brain/learnings/note.md'], spec())
    assert active_reviewers(selection, 'selective') == ['security', 'code']
    assert len(active_reviewers(selection, 'observe')) == 4
    assert 'ui' in active_reviewers(selection, 'selective', ['ui'])
    assert len(active_reviewers(select_reviewers(['.factory/brain/learnings/note.md'], spec(touches_ui=True)), 'selective')) == 4


def test_selective_pipeline_asks_for_missing_reviewer(cfg):
    from .conftest import _role
    class AskUI(FakeClient):
        def create(self, **p):
            if _role(p['system']) == 'approver' and self.approver_calls == 0:
                self.approver_calls += 1
                return self._msg(self._use('submit_result', approved=False, reasoning='need UI', additional_reviewers=['ui']))
            return super().create(**p)
    cfg.review_mode = 'selective'
    client = AskUI(files={'.factory/brain/learnings/test.md': '# Internal note\n'})
    result = Factory(cfg, client, log=lambda *_: None).run(task_from_text('note'))
    assert result.status == RunStatus.SHIPPED, result.error
    assert {r.reviewer for r in result.reviews} == {'security', 'code', 'ui'}
    assert client.calls.count('reviewer_code') == 1
    assert 'reviewer_ux' not in client.calls


def test_paused_issue_does_not_start_new_run(cfg):
    task = task_from_text('paused').model_copy(update={'labels': [cfg.quota_label]})
    client = FakeClient()
    with pytest.raises(ValueError, match='espera'):
        Factory(cfg, client).run(task)
    assert not client.calls


def test_terminal_run_rejects_older_checkpoint(cfg):
    state = Factory(cfg, FakeClient(files=GOOD_FILES), log=lambda *_: None).run(task_from_text('done'))
    assert state.status == RunStatus.SHIPPED
    with pytest.raises(ValueError, match='terminó'):
        Factory(cfg, FakeClient()).resume(state.run_id)
