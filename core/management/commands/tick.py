from django.core.management.base import BaseCommand

from core.scheduler import tick


class Command(BaseCommand):
    help = "Run one sweep: expire grants, retry provisioning, sample router health."

    def handle(self, *args, **options):
        result = tick()
        self.stdout.write(
            f"expired={result['expired']} recovered={result['recovered']} routers={result['routers']}"
        )
