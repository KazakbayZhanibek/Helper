"""Install a reviewed local recipe with footage shipped with this checkout."""
import json
import os
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core import services as s
from core.models import Instruction, MediaAsset, Membership, Organization, User

DEMO_ORG = '7ea2d071-f15f-4fe9-b440-f1622b3e17cf'
DEMO_ASSET = '3fc24e73-92dd-47db-98c2-3ce0fe2695bc'


class Command(BaseCommand):
    help = 'Install the local kitchen, demo accounts and a reviewed video recipe.'

    def add_arguments(self, parser):
        parser.add_argument('--password', default=None)

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEMO_MODE or not settings.DEBUG:
            raise CommandError('Demo seed is permitted only in local development.')
        password = options['password'] or os.getenv('DEMO_PASSWORD')
        if not password or len(password) < 12:
            raise CommandError('Set DEMO_PASSWORD to a password of at least 12 characters.')
        org, _ = Organization.objects.get_or_create(id=DEMO_ORG, defaults={'name': 'Учебная кухня'})
        org = Organization.objects.select_for_update().get(pk=org.pk)
        if not User.objects.filter(email__iexact='operator@demo.local').exists():
            User.objects.create_superuser('operator@demo.local', password, display_name='Оператор сервиса')
        members = {}
        for role, name in [('admin', 'Администратор кухни'), ('author', 'Алия'), ('reviewer', 'Марат'), ('reader', 'Дана')]:
            email = f'{role}@demo.local'
            user = User.objects.filter(email=email).first()
            if user is None:
                user = User.objects.create_user(email, password, display_name=name)
            members[role], _ = Membership.objects.get_or_create(organization=org, user=user, defaults={'role': role})
        source_dir = Path(__file__).resolve().parents[4] / 'frontend' / 'public' / 'examples' / 'vegetables'
        media_dir = settings.PRIVATE_MEDIA_ROOT / 'recipe-demo-v1'
        media_dir.mkdir(parents=True, exist_ok=True)
        for name in ['vegetables.mp4', 'vegetables.jpg']:
            if not (media_dir / name).is_file():
                shutil.copyfile(source_dir / name, media_dir / name)
        if MediaAsset.objects.filter(pk=DEMO_ASSET, organization=org).exists():
            self.stdout.write('Учебная кухня готова.')
            return
        author, reviewer = members['author'], members['reviewer']
        example = json.loads((source_dir / 'recipe.json').read_text(encoding='utf-8'))
        data = {key: example[key] for key in ('title', 'purpose', 'language', 'recipe')}
        draft, _ = s.create_instruction(author, data)
        asset = MediaAsset.objects.create(id=DEMO_ASSET, organization=org, storage_key='recipe-demo-v1/vegetables.mp4',
                                          thumbnail_key='recipe-demo-v1/vegetables.jpg', is_demo=True,
                                          width=960, height=540, has_audio=False, duration_ms=example['duration_ms'])
        pk = draft['instruction_id']
        Instruction.objects.filter(pk=pk, organization=org).update(source_asset=asset)
        steps = [{**step, 'id': None, 'evidence_segment_ids': []} for step in example['steps']]
        saved = s.save_working(author, pk, {**data, 'steps': steps}, draft['etag'])
        reviewed = s.workflow_action(author, pk, 'submit-review', {}, saved['etag'])
        for step in reviewed['steps']:
            reviewed = s.workflow_action(reviewer, pk, 'review/check-step', {'step_id': step['id'], 'checked': True}, reviewed['etag'])
        s.publish(reviewer, pk, {'confirmations': {key: True for key in s.CONFIRMATIONS}}, reviewed['etag'])
        self.stdout.write(self.style.SUCCESS('Опубликован учебный рецепт овощной нарезки.'))
