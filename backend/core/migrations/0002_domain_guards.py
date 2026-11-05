"""DB-level tenant and immutable-publication invariants on SQLite and PostgreSQL.

SQLite is for local development only. Each guard validates raw SQL and bulk ORM
writes, not only API serializers. Organization assignment is immutable after insert.
"""
from django.db import migrations

CHECKS = {
    'core_instruction': [
        ('source_tenant', "NEW.source_asset_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM core_mediaasset a WHERE a.id=NEW.source_asset_id AND a.organization_id=NEW.organization_id)"),
        ('current_publication', "NEW.current_version_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM core_publishedversion v WHERE v.id=NEW.current_version_id AND v.organization_id=NEW.organization_id AND v.instruction_id=NEW.id)"),
    ],
    'core_workingcopy': [
        ('instruction_tenant', "NOT EXISTS (SELECT 1 FROM core_instruction i WHERE i.id=NEW.instruction_id AND i.organization_id=NEW.organization_id)"),
        ('base_version', "NEW.based_on_version_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM core_publishedversion v WHERE v.id=NEW.based_on_version_id AND v.organization_id=NEW.organization_id AND v.instruction_id=NEW.instruction_id)"),
    ],
    'core_workingstep': [
        ('copy_tenant', "NOT EXISTS (SELECT 1 FROM core_workingcopy w WHERE w.id=NEW.working_copy_id AND w.organization_id=NEW.organization_id)"),
    ],
    'core_publishedversion': [
        ('instruction_and_source', "NOT EXISTS (SELECT 1 FROM core_instruction i WHERE i.id=NEW.instruction_id AND i.organization_id=NEW.organization_id AND i.source_asset_id=NEW.source_asset_id)"),
        ('asset_tenant', "NOT EXISTS (SELECT 1 FROM core_mediaasset a WHERE a.id=NEW.source_asset_id AND a.organization_id=NEW.organization_id)"),
    ],
}
TENANTS = ['core_mediaasset', 'core_instruction', 'core_workingcopy', 'core_workingstep', 'core_publishedversion']
UPDATE_CHECKS = {
    'core_mediaasset': [('source_metadata_fixed', 'NEW.duration_ms != OLD.duration_ms OR NEW.storage_key != OLD.storage_key')],
    'core_instruction': [('source_not_replaced', 'OLD.source_asset_id IS NOT NULL AND (NEW.source_asset_id IS NULL OR NEW.source_asset_id != OLD.source_asset_id)')],
    'core_workingcopy': [('working_owner_fixed', 'NEW.instruction_id != OLD.instruction_id')],
    'core_publishedversion': [('snapshot_immutable', '1=1')],
}


def specifications():
    specs = []
    for table, checks in CHECKS.items():
        for label, condition in checks:
            for event in ['INSERT', 'UPDATE']:
                specs.append((table, f'{table}_{label}_{event.lower()}', event, condition))
    for table in TENANTS:
        specs.append((table, f'{table}_tenant_fixed', 'UPDATE', 'NEW.organization_id != OLD.organization_id'))
    for table, checks in UPDATE_CHECKS.items():
        for label, condition in checks:
            specs.append((table, f'{table}_{label}', 'UPDATE', condition))
    return specs


def forwards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    for table, name, event, condition in specifications():
        if vendor == 'sqlite':
            schema_editor.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT, '{name}'); END")
        elif vendor == 'postgresql':
            schema_editor.execute(f"CREATE FUNCTION {name}_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN RAISE EXCEPTION '{name}' USING ERRCODE = '23514'; END IF; RETURN NEW; END; $$")
            schema_editor.execute(f'CREATE TRIGGER {name} BEFORE {event} ON {table} FOR EACH ROW EXECUTE FUNCTION {name}_fn()')
        else:
            raise RuntimeError('Domain guards support only SQLite local dev and PostgreSQL.')


def backwards(apps, schema_editor):
    for table, name, _, _ in reversed(specifications()):
        if schema_editor.connection.vendor == 'sqlite':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')
        elif schema_editor.connection.vendor == 'postgresql':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {table}')
            schema_editor.execute(f'DROP FUNCTION IF EXISTS {name}_fn()')


class Migration(migrations.Migration):
    dependencies = [('core', '0001_initial')]
    operations = [migrations.RunPython(forwards, backwards)]
