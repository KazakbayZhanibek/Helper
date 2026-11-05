"""No network or real subscription credentials: controlled local CLI boundary."""
import hashlib
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_drafts import codex_provider, providers
from media_pipeline.exceptions import Cancelled, ProcessingFailure
from media_pipeline.worker import JobContext, claim_job, process_one
from visual_drafts import provider as vision
from visual_drafts.models import VisualProposal
from .test_media_pipeline import queued_job
from .test_visual_drafts import add_frames, create_session, job_for

pytestmark = pytest.mark.django_db


@pytest.fixture
def codex_config(team, settings, tmp_path):
    binary = tmp_path / 'codex-test'
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o700)
    settings.DEBUG = True
    settings.DEMO_MODE = True
    settings.CODEX_TEST_MODE = True
    settings.CODEX_CLI_BINARY = str(binary)
    settings.STEP_DRAFT_BACKEND = 'codex'
    settings.STEP_DRAFT_ENDPOINT = ''
    settings.STEP_DRAFT_MODEL = 'gpt-6-luna'
    settings.STEP_DRAFT_API_KEY = ''
    settings.EXTERNAL_AI_ENABLED = True
    team.org.external_ai_enabled = True
    team.org.save(update_fields=['external_ai_enabled'])
    return binary


def frames():
    result = []
    for i, color in enumerate(('red', 'blue')):
        output = io.BytesIO()
        Image.new('RGB', (80, 60), color).save(output, format='JPEG')
        content = output.getvalue()
        result.append({'id': str(uuid.uuid4()), 'sequence': i + 1, 'timestamp_ms': i * 1000,
                       'bytes': content, 'sha256': hashlib.sha256(content).hexdigest()})
    return result


def candidate(version='2'):
    return {'schema_version': version, 'language': 'ru', 'steps': [],
            'warnings': [{'code': 'insufficient_evidence', 'message': 'Действий недостаточно.'}]}


def cli_result(argv, value, *, event_item='agent_message', output=None):
    encoded = json.dumps(value, ensure_ascii=False)
    Path(argv[argv.index('--output-last-message') + 1]).write_text(encoded if output is None else output)
    stdout = '\n'.join(json.dumps(event, ensure_ascii=False) for event in [
        {'type': 'thread.started'}, {'type': 'turn.started'},
        {'type': 'item.completed', 'item': {'type': event_item, 'text': encoded}},
        {'type': 'turn.completed'},
    ])
    return subprocess.CompletedProcess(argv, 0, stdout, '')


def test_codex_is_explicitly_external_without_requiring_an_api_key(team, codex_config):
    config = providers.capabilities(team.org, probe=False)['step_drafts']
    assert config['available'] and config['external'] and config['configured']
    assert config['provider'] == 'codex'
    assert str(codex_config) not in json.dumps(config)
    assert vision.capabilities(team.org)['available']


@pytest.mark.parametrize('disabled', ['DEBUG', 'DEMO_MODE', 'CODEX_TEST_MODE', 'EXTERNAL_AI_ENABLED', 'organization', 'binary'])
def test_codex_cannot_run_in_production_or_without_cloud_permission(team, settings, codex_config, disabled):
    if disabled == 'organization':
        team.org.external_ai_enabled = False
    elif disabled == 'binary':
        settings.CODEX_CLI_BINARY = '/does/not/exist'
    else:
        setattr(settings, disabled, False)
    assert not providers.endpoint_configuration(team.org)['available']
    assert not vision.capabilities(team.org)['available']
    with pytest.raises(providers.ProviderFailure):
        vision.VisualProvider(team.org)


def test_cli_receives_only_bounded_frames_schema_and_clean_environment(team, codex_config, monkeypatch):
    for name in ('OPENAI_API_KEY', 'STEP_DRAFT_API_KEY', 'NVIDIA_API_KEY', 'CODEX_API_KEY', 'CODEX_THREAD_ID', 'HTTP_PROXY'):
        monkeypatch.setenv(name, 'private-test-value')
    source = frames()
    paths = []
    def run(argv, timeout, *, input_bytes, cwd, env):
        assert timeout == 180 and argv[-1] == '-'
        assert '--ignore-user-config' in argv and '--ephemeral' in argv and '--strict-config' in argv
        assert argv[argv.index('--sandbox') + 1] == 'read-only'
        for feature in codex_provider.DISABLED_FEATURES:
            assert any(argv[i:i + 2] == ['--disable', feature] for i in range(len(argv) - 1))
        assert 'forced_login_method="chatgpt"' in argv and 'mcp_servers={}' in argv
        assert 'private-test-value' not in json.dumps(env)
        assert set(env) <= codex_provider.ENV_ALLOWLIST
        assert stat.S_IMODE(Path(cwd).stat().st_mode) == 0o700
        assert 'ru' in input_bytes.decode() and not any('Действий' in arg for arg in argv)
        images = [Path(argv[i + 1]) for i, arg in enumerate(argv[:-1]) if arg == '--image']
        assert len(images) == len(source)
        for image, frame in zip(images, source):
            assert image.read_bytes() == frame['bytes']
            assert stat.S_IMODE(image.stat().st_mode) == 0o600
        paths.append(Path(cwd))
        return cli_result(argv, candidate())
    value = vision.VisualProvider(team.org).generate(language='ru', mode='live', frames=source, context=SimpleNamespace(run=run))
    assert value == candidate()
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize('mutation', ['hash', 'bytes', 'uuid', 'order', 'count'])
def test_corrupt_or_unbounded_frames_never_launch_cli(team, codex_config, mutation):
    source = frames()
    if mutation == 'hash':
        source[0]['sha256'] = '0' * 64
    elif mutation == 'bytes':
        source[0]['bytes'] = b'not jpeg'
        source[0]['sha256'] = hashlib.sha256(source[0]['bytes']).hexdigest()
    elif mutation == 'uuid':
        source[0]['id'] = '../../source'
    elif mutation == 'order':
        source.reverse()
    else:
        source *= 7
    calls = []
    with pytest.raises(providers.ProviderFailure):
        vision.VisualProvider(team.org).generate(language='ru', mode='live', frames=source, context=SimpleNamespace(run=lambda *a, **k: calls.append(1)))
    assert calls == []


@pytest.mark.parametrize('failure', ['tool', 'malformed', 'schema', 'oversized', 'missing_turn'])
def test_tool_events_and_bad_output_are_rejected_and_files_removed(team, codex_config, failure):
    paths = []
    def run(argv, **kwargs):
        paths.append(Path(kwargs['cwd']))
        value = candidate() if failure != 'schema' else {'wrong': True}
        result = cli_result(argv, value, event_item='command_execution' if failure == 'tool' else 'agent_message',
                            output='not json' if failure == 'malformed' else 'x' * 262145 if failure == 'oversized' else None)
        if failure == 'missing_turn':
            result.stdout = ''
        return result
    with pytest.raises(providers.ProviderFailure) as caught:
        vision.VisualProvider(team.org).generate(language='ru', mode='live', frames=frames(), context=SimpleNamespace(run=run))
    assert caught.value.code == ('CODEX_TOOL_USE_REJECTED' if failure == 'tool' else 'MODEL_OUTPUT_INVALID')
    assert all(not path.exists() for path in paths)


def test_transcript_uses_same_controlled_runner_and_schema(team, codex_config):
    def run(argv, **kwargs):
        schema = json.loads(Path(argv[argv.index('--output-schema') + 1]).read_text())
        assert schema['properties']['schema_version'] == {'type': 'string', 'enum': ['1']}
        assert '--image' not in argv
        return cli_result(argv, candidate('1'))
    assert providers.StepDraftProvider(team.org).generate({'segments': []}, context=SimpleNamespace(run=run)) == candidate('1')


def install_stub(binary, source):
    script = binary.with_suffix('.py')
    script.write_text(source)
    binary.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(script)) + ' "$@"\n')
    binary.chmod(0o700)


def test_live_worker_runs_subprocess_and_saves_only_a_proposal(team, draft, codex_config):
    install_stub(codex_config, '''import json, pathlib, sys
args = sys.argv[1:]
request = json.loads(sys.stdin.read().rsplit('\\n\\n', 1)[1])
value = {'schema_version':'2','language':request['language'],'steps':[], 'warnings':[{'code':'insufficient_evidence','message':'Действий недостаточно.'}]}
text = json.dumps(value)
pathlib.Path(args[args.index('--output-last-message') + 1]).write_text(text)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}))
print(json.dumps({'type':'turn.completed'}))
''')
    session = create_session(team, draft, 'live')
    evidence = add_frames(session)
    job = job_for(team, session, evidence)
    revision = session.instruction.working_copy.revision
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done', job.error_code
    proposal = VisualProposal.objects.get(source_job_id=job.id)
    assert proposal.provider == 'codex' and proposal.applied_at is None
    assert proposal.content['steps'] == []
    session.instruction.working_copy.refresh_from_db()
    assert session.instruction.working_copy.revision == revision


@pytest.mark.parametrize('stop', ['timeout', 'cancel'])
def test_managed_cli_process_is_terminated_and_temp_files_removed(team, codex_config, monkeypatch, stop):
    install_stub(codex_config, 'import time\ntime.sleep(10)\n')
    queued_job(team)
    context = JobContext(claim_job())
    actual_popen = subprocess.Popen
    processes, directories = [], []
    def start_process(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        processes.append(process)
        directories.append(Path(kwargs['cwd']))
        return process
    monkeypatch.setattr('media_pipeline.worker.subprocess.Popen', start_process)
    if stop == 'timeout':
        monkeypatch.setattr(codex_provider, 'TIMEOUT_SECONDS', 0.1)
    else:
        original_check = context.check
        calls = 0
        def cancel_after_start():
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise Cancelled()
            original_check()
        monkeypatch.setattr(context, 'check', cancel_after_start)
    with pytest.raises(ProcessingFailure if stop == 'timeout' else Cancelled) as caught:
        providers.StepDraftProvider(team.org).generate({'segments': []}, context=context)
    if stop == 'timeout':
        assert caught.value.code == 'PROCESS_TIMEOUT'
    assert processes and all(process.poll() is not None for process in processes)
    assert all(not directory.exists() for directory in directories)
