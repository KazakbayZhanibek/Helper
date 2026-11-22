"""Provision a private kitchen and its members from the server console."""
from getpass import getpass
import uuid

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction

from core.models import Membership, Organization, User


class Command(BaseCommand):
    help = 'Create a kitchen or add an account to an existing kitchen.'

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument('--new-kitchen', metavar='NAME')
        target.add_argument('--kitchen-id', type=uuid.UUID)
        parser.add_argument('--email', required=True)
        parser.add_argument('--name', required=True)
        parser.add_argument('--role', choices=Membership.ROLES, required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        email = options['email'].strip().lower()
        name = options['name'].strip()
        try:
            validate_email(email)
        except ValidationError as error:
            raise CommandError('Введите действительный email.') from error
        if not 1 <= len(name) <= 120 or len(email) > 254:
            raise CommandError('Проверьте имя и email участника.')
        if options['new_kitchen'] is not None:
            kitchen = options['new_kitchen'].strip()
            if not 1 <= len(kitchen) <= 120 or options['role'] != 'admin':
                raise CommandError('Укажите название кухни до 120 символов и роль admin для её создателя.')
            existing = Organization.objects.filter(name=kitchen).first()
            if existing:
                raise CommandError(f'Кухня уже существует. Используйте --kitchen-id {existing.pk}.')
            org = Organization.objects.create(name=kitchen)
        else:
            org = Organization.objects.select_for_update().filter(pk=options['kitchen_id'], status='active').first()
            if not org:
                raise CommandError('Действующая кухня с таким идентификатором не найдена.')
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            password = getpass('Пароль нового участника: ')
            if password != getpass('Повторите пароль: '):
                raise CommandError('Пароли не совпали.')
            try:
                validate_password(password, User(email=email, display_name=name))
            except ValidationError as error:
                raise CommandError(' '.join(error.messages)) from error
            user = User.objects.create_user(email, password, display_name=name)
        if not user.is_active:
            raise CommandError('Этот аккаунт отключён.')
        member, _ = Membership.objects.get_or_create(organization=org, user=user, defaults={'role': options['role']})
        if member.role != options['role'] or not member.is_active:
            raise CommandError('У аккаунта уже есть членство с другими правами или статусом.')
        self.stdout.write(self.style.SUCCESS(f'{org.name} ({org.pk}) · {user.email} · {member.role}'))
