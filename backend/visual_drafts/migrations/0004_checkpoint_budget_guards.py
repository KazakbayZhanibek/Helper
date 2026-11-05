from django.db import migrations


def forwards(apps, schema_editor):
    immutable = [
        ('visual_window_completed_fixed', 'visual_drafts_visualanalysiswindow', "OLD.state='completed'"),
        ('visual_analysis_budget_fixed', 'visual_drafts_visualsession', 'NEW.analysis_scope != OLD.analysis_scope OR NEW.analysis_budget != OLD.analysis_budget'),
    ]
    for name, table, condition in immutable:
        if schema_editor.connection.vendor == 'sqlite':
            schema_editor.execute(f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT, '{name}'); END")
        else:
            schema_editor.execute(f"CREATE FUNCTION {name}_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN RAISE EXCEPTION '{name}' USING ERRCODE = '23514'; END IF; RETURN NEW; END; $$")
            schema_editor.execute(f'CREATE TRIGGER {name} BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION {name}_fn()')


def backwards(apps, schema_editor):
    for name, table in [('visual_window_completed_fixed', 'visual_drafts_visualanalysiswindow'), ('visual_analysis_budget_fixed', 'visual_drafts_visualsession')]:
        if schema_editor.connection.vendor == 'sqlite':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')
        else:
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {table}')
            schema_editor.execute(f'DROP FUNCTION IF EXISTS {name}_fn()')


class Migration(migrations.Migration):
    dependencies = [('visual_drafts', '0003_full_video_windows')]
    operations = [migrations.RunPython(forwards, backwards)]
