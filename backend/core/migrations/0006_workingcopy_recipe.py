from importlib import import_module

from django.db import migrations, models


guards = import_module('core.migrations.0003_real_media_and_evidence')


def rollback_rebuilds_table(schema_editor):
    connection = schema_editor.connection
    return connection.vendor == 'sqlite' and not connection.features.can_alter_table_drop_column


def drop_guards(apps, schema_editor):
    if rollback_rebuilds_table(schema_editor):
        guards.previous_guards.backwards(apps, schema_editor)


def restore_guards(apps, schema_editor):
    if rollback_rebuilds_table(schema_editor):
        guards.media_guards(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [('core', '0005_workingstep_photo_demonstration')]

    operations = [
        # A nullable column can be added in place. Older SQLite versions rebuild
        # the table only on rollback, when guards must be removed and restored.
        migrations.RunPython(migrations.RunPython.noop, restore_guards),
        migrations.AddField(model_name='workingcopy', name='recipe', field=models.JSONField(blank=True, null=True)),
        migrations.RunPython(migrations.RunPython.noop, drop_guards),
    ]
