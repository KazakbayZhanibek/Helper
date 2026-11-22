import unicodedata
from importlib import import_module
from itertools import islice

from django.db import migrations, models


previous = import_module('core.migrations.0006_workingcopy_recipe')


def search_text(title, purpose, recipe, steps=()):
    parts = [title, purpose, *(recipe or {}).get('ingredients', [])]
    for step in steps:
        parts.extend((step['title'], step['description']))
    return unicodedata.normalize('NFC', ' '.join(parts)).casefold().replace('ё', 'е')


def index_existing_documents(apps, schema_editor):
    alias = schema_editor.connection.alias
    copies = apps.get_model('core', 'WorkingCopy').objects.using(alias)
    rows = copies.only('id', 'title', 'purpose', 'recipe').order_by('pk').iterator(chunk_size=200)
    while batch := list(islice(rows, 200)):
        for row in batch:
            row.library_search_text = search_text(row.title, row.purpose, row.recipe)
        copies.bulk_update(batch, ['library_search_text'])

    index_publications(apps, schema_editor)


def index_publications(apps, schema_editor, *, legacy=False):
    instructions = apps.get_model('core', 'Instruction').objects.using(schema_editor.connection.alias)
    published = instructions.filter(current_version__isnull=False).select_related('current_version')
    rows = published.only('id', 'current_version__snapshot').order_by('pk').iterator(chunk_size=200)
    while batch := list(islice(rows, 200)):
        for row in batch:
            snapshot = row.current_version.snapshot
            if legacy:
                parts = [snapshot['title'], snapshot['purpose']]
                for step in snapshot['steps']:
                    parts.extend((step['title'], step['description']))
                row.published_search_text = ' '.join(parts)
            else:
                row.published_search_text = search_text(snapshot['title'], snapshot['purpose'],
                                                        snapshot.get('recipe'), snapshot['steps'])
        instructions.bulk_update(batch, ['published_search_text'])


def restore_publication_index(apps, schema_editor):
    index_publications(apps, schema_editor, legacy=True)


class Migration(migrations.Migration):
    dependencies = [('core', '0006_workingcopy_recipe')]
    operations = [
        migrations.RunPython(migrations.RunPython.noop, previous.restore_guards),
        migrations.AddField(
            model_name='instruction', name='content_kind',
            field=models.CharField(choices=[('recipe', 'Рецепт')], editable=False, max_length=12, null=True),
        ),
        migrations.AddField(
            model_name='workingcopy', name='library_search_text',
            field=models.TextField(editable=False, null=True),
        ),
        migrations.RunPython(index_existing_documents, restore_publication_index),
        migrations.RunPython(migrations.RunPython.noop, previous.drop_guards),
    ]
