# ProofDeploy seed for the linkding contract (D267, round 5, WO-R5-K).
#
# Run as `python manage.py shell < <this file>` inside the checkout, with the
# side's generated bootstrap values in the environment (LD_SUPERUSER_NAME and
# LD_API_TOKEN). The contract's `seed:` carries this text verbatim (the
# sandbox checkout holds no file from readiness/seeds/), and a test keeps the
# two identical.
#
# It creates exactly one superuser, through the app's own
# `create_initial_superuser` command (bookmarks/management/commands/
# create_initial_superuser.py, linkding 27b7303b), and exactly one API token
# for that user whose key is the generated LD_API_TOKEN value. Nothing else.
# No LD_SUPERUSER_PASSWORD is declared, so the command gives the user an
# unusable password: the token is the only credential.
import os

from bookmarks.models import ApiToken
from django.contrib.auth import get_user_model
from django.core.management import call_command

name = os.environ["LD_SUPERUSER_NAME"]
key = os.environ["LD_API_TOKEN"]
call_command("create_initial_superuser")
user = get_user_model().objects.get(username=name)
ApiToken.objects.create(user=user, name="ProofDeploy", key=key)
