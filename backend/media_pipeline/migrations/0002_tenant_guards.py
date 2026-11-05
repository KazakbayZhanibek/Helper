from django.db import migrations


def specifications():
    checks = {
        'media_pipeline_processingjob': [
            ('instruction_asset', 'NOT EXISTS (SELECT 1 FROM core_instruction i WHERE i.id=NEW.instruction_id AND i.organization_id=NEW.organization_id AND i.source_asset_id=NEW.asset_id)'),
            ('requester', 'NOT EXISTS (SELECT 1 FROM core_membership m WHERE m.organization_id=NEW.organization_id AND m.user_id=NEW.requested_by_id)'),
        ],
        'media_pipeline_uploadsession': [
            ('instruction', 'NOT EXISTS (SELECT 1 FROM core_instruction i WHERE i.id=NEW.instruction_id AND i.organization_id=NEW.organization_id)'),
            ('asset', 'NEW.asset_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM core_mediaasset a WHERE a.id=NEW.asset_id AND a.organization_id=NEW.organization_id)'),
            ('uploader', 'NOT EXISTS (SELECT 1 FROM core_membership m WHERE m.organization_id=NEW.organization_id AND m.user_id=NEW.uploader_id)'),
        ],
    }
    result = []
    for table, rules in checks.items():
        for label, condition in rules:
            for event in ('INSERT', 'UPDATE'):
                # Names stay below PostgreSQL's 63-character identifier limit.
                name = f'{table}_{label}_{event.lower()}'
                result.append((table, name, event, condition))
        result.append((table, table + '_ownership_fixed', 'UPDATE', 'NEW.organization_id != OLD.organization_id OR NEW.instruction_id != OLD.instruction_id'))
    result.append(('media_pipeline_processingjob', 'media_job_input_fixed', 'UPDATE',
                   'NEW.asset_id != OLD.asset_id OR NEW.requested_by_id != OLD.requested_by_id OR NEW.kind != OLD.kind OR NEW.input_hash != OLD.input_hash'))
    result.append(('media_pipeline_uploadsession', 'media_upload_input_fixed', 'UPDATE',
                   'NEW.uploader_id != OLD.uploader_id OR NEW.expected_size_bytes != OLD.expected_size_bytes OR (OLD.asset_id IS NOT NULL AND (NEW.asset_id IS NULL OR NEW.asset_id != OLD.asset_id))'))
    return result


def forwards(apps, schema_editor):
    for table, name, event, condition in specifications():
        if schema_editor.connection.vendor == 'sqlite':
            schema_editor.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT, '{name}'); END")
        elif schema_editor.connection.vendor == 'postgresql':
            schema_editor.execute(f"CREATE FUNCTION {name}_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN RAISE EXCEPTION '{name}' USING ERRCODE = '23514'; END IF; RETURN NEW; END; $$")
            schema_editor.execute(f'CREATE TRIGGER {name} BEFORE {event} ON {table} FOR EACH ROW EXECUTE FUNCTION {name}_fn()')
        else:
            raise RuntimeError('Media guards support SQLite and PostgreSQL only.')


def backwards(apps, schema_editor):
    for table, name, _, _ in reversed(specifications()):
        if schema_editor.connection.vendor == 'sqlite':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')
        elif schema_editor.connection.vendor == 'postgresql':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {table}')
            schema_editor.execute(f'DROP FUNCTION IF EXISTS {name}_fn()')


class Migration(migrations.Migration):
    dependencies = [('media_pipeline', '0001_initial')]
    operations = [migrations.RunPython(forwards, backwards)]
