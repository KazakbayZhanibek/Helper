"""Operator opt-in to external processing; never accepts or prints credentials."""
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import AuditEvent, Organization


class Command(BaseCommand):
    help = 'Allow or disable external AI for one organization. No API key is read or printed.'

    def add_arguments(self, parser):
        parser.add_argument('--organization', required=True, type=uuid.UUID)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--allow-external', action='store_true')
        group.add_argument('--disable-external', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        organization = Organization.objects.select_for_update().filter(id=options['organization'], status='active').first()
        if not organization:
            raise CommandError('Active organization not found.')
        allowed = options['allow_external']
        if organization.external_ai_enabled != allowed:
            organization.external_ai_enabled = allowed
            organization.save(update_fields=['external_ai_enabled'])
            AuditEvent.objects.create(organization=organization, action='organization.external_ai_enabled' if allowed else 'organization.external_ai_disabled', object_id=organization.id)
        self.stdout.write(f'External AI {"allowed" if allowed else "disabled"} for organization {organization.id}. Server configuration and an explicit generation request are still required.')
