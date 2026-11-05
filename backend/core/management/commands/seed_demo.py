"""Create synthetic-only local demo. Never alters an existing user's password."""
import os
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from core.models import User, Organization, Membership, WorkingStep
from core import services as s


class Command(BaseCommand):
    help = 'Seed synthetic demo content; requires DEMO_MODE and DEMO_PASSWORD.'
    def add_arguments(self, parser):
        parser.add_argument('--password', default=None)
    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEMO_MODE or not settings.DEBUG:
            raise CommandError('Demo seed is permitted only in local development.')
        password = options['password'] or os.getenv('DEMO_PASSWORD')
        if not password or len(password) < 12:
            raise CommandError('Set DEMO_PASSWORD to a password of at least 12 characters.')
        if not (settings.PRIVATE_MEDIA_ROOT / 'demo-process.mp4').is_file():
            raise CommandError('Generate private-media/demo-process.mp4 before seeding.')
        org, _ = Organization.objects.get_or_create(id='7ea2d071-f15f-4fe9-b440-f1622b3e17cf', defaults={'name': 'Учебная мастерская'})
        users = {}
        for role, name in [('admin', 'Айдар · администратор'), ('author', 'Алия · автор'), ('reviewer', 'Марат · мастер'), ('reader', 'Дана · сотрудник')]:
            email = f'{role}@demo.local'
            user = User.objects.filter(email=email).first()
            if user is None:
                user = User.objects.create_user(email, password, display_name=name)
            Membership.objects.get_or_create(organization=org, user=user, defaults={'role': role})
            users[role] = user
        author = Membership.objects.get(organization=org, user=users['author'])
        reviewer = Membership.objects.get(organization=org, user=users['reviewer'])
        if org.instruction_set.exists():
            self.stdout.write('Demo organization already contains instructions; preserved existing work.')
            return
        items = [
            ('Комплектация учебного набора', 'Собрать и проверить демонстрационный набор перед упаковкой.', True),
            ('Упаковка учебного набора', 'Уложить три учебных предмета в коробку и проверить комплектность.', False),
        ]
        steps = [
            ('Проверьте состав набора', 'Сверьте три учебных предмета с составом набора. Убедитесь, что все предметы на месте.', 0, 15000),
            ('Уложите предметы в коробку', 'Разместите три учебных предмета в коробке, как показано в демонстрационном фрагменте.', 15000, 30000),
            ('Добавьте карточку комплекта', 'Положите карточку комплекта поверх предметов. Ещё раз сверьте содержимое коробки с карточкой.', 30000, 45000),
            ('Закройте и отметьте комплект', 'Закройте коробку и отметьте завершение комплектации учебного набора.', 45000, 60000),
        ]
        for title, purpose, published in items:
            data, _ = s.create_instruction(author, {'title': title, 'purpose': purpose, 'language': 'ru', 'demo_source': True})
            saved = s.save_working(author, data['instruction_id'], {'title': title, 'purpose': purpose, 'language': 'ru', 'steps': [{'id': None, 'title': t, 'description': d, 'start_ms': a, 'end_ms': b, 'evidence_segment_ids': []} for t, d, a, b in steps]}, data['etag'])
            if published:
                reviewed = s.workflow_action(author, data['instruction_id'], 'submit-review', {}, saved['etag'])
                for step in reviewed['steps']:
                    reviewed = s.workflow_action(reviewer, data['instruction_id'], 'review/check-step', {'step_id': step['id'], 'checked': True}, reviewed['etag'])
                s.publish(reviewer, data['instruction_id'], {'confirmations': {key: True for key in s.CONFIRMATIONS}}, reviewed['etag'])
        self.stdout.write(self.style.SUCCESS('Demo created: admin/author/reviewer/reader@demo.local. Password comes from DEMO_PASSWORD.'))
