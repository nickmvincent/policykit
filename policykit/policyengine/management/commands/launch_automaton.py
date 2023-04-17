from django.core.management.base import BaseCommand
from policyengine.models import Policy, PolicyVariable, ActionType


class Command(BaseCommand):
    help = "..."

    def handle(self, *args, **options):
        pass
