from django.db import migrations


def specifications():
    checks = {
        'visual_drafts_visualsession': 'NOT EXISTS (SELECT 1 FROM core_instruction i WHERE i.id=NEW.instruction_id AND i.organization_id=NEW.organization_id) OR (NEW.asset_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM core_mediaasset a JOIN core_instruction i ON i.source_asset_id=a.id WHERE a.id=NEW.asset_id AND a.organization_id=NEW.organization_id AND i.id=NEW.instruction_id)) OR (NEW.mode=\'upload\' AND NEW.asset_id IS NULL) OR (NEW.mode=\'live\' AND NEW.asset_id IS NOT NULL)',
        'visual_drafts_frameevidence': 'NOT EXISTS (SELECT 1 FROM visual_drafts_visualsession s WHERE s.id=NEW.session_id AND s.organization_id=NEW.organization_id AND NEW.coverage_start_ms>=s.window_start_ms AND NEW.coverage_end_ms<=s.window_end_ms)',
        'visual_drafts_visualproposal': 'NOT EXISTS (SELECT 1 FROM visual_drafts_visualsession s WHERE s.id=NEW.session_id AND s.organization_id=NEW.organization_id)',
    }
    for table, condition in checks.items():
        for event in ['INSERT', 'UPDATE']:
            yield table, f'{table}_tenant_{event.lower()}', event, condition
    yield 'visual_drafts_frameevidence', 'visual_frames_immutable', 'UPDATE', '1=1'
    session_columns = ['organization_id', 'instruction_id', 'created_by_id', 'mode', 'base_working_revision', 'language', 'window_start_ms', 'window_end_ms', 'consent_images']
    yield 'visual_drafts_visualsession', 'visual_session_source_fixed', 'UPDATE', ' OR '.join(f'NEW.{column} != OLD.{column}' for column in session_columns)
    proposal_columns = ['organization_id', 'session_id', 'source_job_id', 'content', 'provider', 'model']
    yield 'visual_drafts_visualproposal', 'visual_proposal_content_fixed', 'UPDATE', ' OR '.join(f'NEW.{column} != OLD.{column}' for column in proposal_columns)


def forwards(apps, schema_editor):
    for table, name, event, condition in specifications():
        if schema_editor.connection.vendor == 'sqlite':
            schema_editor.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT, '{name}'); END")
        elif schema_editor.connection.vendor == 'postgresql':
            schema_editor.execute(f"CREATE FUNCTION {name}_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN RAISE EXCEPTION '{name}' USING ERRCODE = '23514'; END IF; RETURN NEW; END; $$")
            schema_editor.execute(f'CREATE TRIGGER {name} BEFORE {event} ON {table} FOR EACH ROW EXECUTE FUNCTION {name}_fn()')
        else:
            raise RuntimeError('Visual guards require PostgreSQL or SQLite.')


def backwards(apps, schema_editor):
    for table, name, _, _ in reversed(list(specifications())):
        if schema_editor.connection.vendor == 'sqlite':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')
        elif schema_editor.connection.vendor == 'postgresql':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {table}')
            schema_editor.execute(f'DROP FUNCTION IF EXISTS {name}_fn()')


class Migration(migrations.Migration):
    dependencies = [('visual_drafts', '0001_initial'), ('media_pipeline', '0003_live_visual_jobs')]
    operations = [migrations.RunPython(forwards, backwards)]
