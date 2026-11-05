"""Enforce source ownership and immutable AI artifacts even for bulk SQL writes."""
from django.db import migrations


def specifications():
    checks = {
        'ai_drafts_transcript': [('source', 'NOT EXISTS (SELECT 1 FROM core_mediaasset a WHERE a.id=NEW.asset_id AND a.organization_id=NEW.organization_id AND a.duration_ms=NEW.duration_ms)')],
        'ai_drafts_transcriptsegment': [('source', 'NOT EXISTS (SELECT 1 FROM ai_drafts_transcript t WHERE t.id=NEW.transcript_id AND t.organization_id=NEW.organization_id AND NEW.end_ms<=t.duration_ms)')],
        'ai_drafts_draftproposal': [('source', 'NOT EXISTS (SELECT 1 FROM ai_drafts_transcript t JOIN core_instruction i ON i.source_asset_id=t.asset_id WHERE t.id=NEW.transcript_id AND i.id=NEW.instruction_id AND t.organization_id=NEW.organization_id AND i.organization_id=NEW.organization_id)')],
    }
    for table, validations in checks.items():
        for label, condition in validations:
            for event in ['INSERT', 'UPDATE']:
                yield table, f'{table}_{label}_{event.lower()}', event, condition
    for table in ['ai_drafts_transcript', 'ai_drafts_transcriptsegment']:
        yield table, f'{table}_immutable', 'UPDATE', '1=1'
    immutable_proposal = ['organization_id', 'instruction_id', 'transcript_id', 'base_working_revision', 'provider', 'model', 'content', 'input_sha256', 'config_version']
    yield 'ai_drafts_draftproposal', 'ai_drafts_draftproposal_immutable_content', 'UPDATE', ' OR '.join(f'NEW.{column} != OLD.{column}' for column in immutable_proposal)


def forwards(apps, schema_editor):
    for table, name, event, condition in specifications():
        if schema_editor.connection.vendor == 'sqlite':
            schema_editor.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT, '{name}'); END")
        elif schema_editor.connection.vendor == 'postgresql':
            schema_editor.execute(f"CREATE FUNCTION {name}_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN RAISE EXCEPTION '{name}' USING ERRCODE = '23514'; END IF; RETURN NEW; END; $$")
            schema_editor.execute(f'CREATE TRIGGER {name} BEFORE {event} ON {table} FOR EACH ROW EXECUTE FUNCTION {name}_fn()')
        else:
            raise RuntimeError('AI guards support only SQLite and PostgreSQL.')


def backwards(apps, schema_editor):
    for table, name, _, _ in reversed(list(specifications())):
        if schema_editor.connection.vendor == 'sqlite':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')
        elif schema_editor.connection.vendor == 'postgresql':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {table}')
            schema_editor.execute(f'DROP FUNCTION IF EXISTS {name}_fn()')


class Migration(migrations.Migration):
    dependencies = [('ai_drafts', '0001_initial')]
    operations = [migrations.RunPython(forwards, backwards)]
