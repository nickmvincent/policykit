import json
import logging

from django.contrib.auth.models import Permission
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseServerError,
    HttpResponseNotFound,
)
from django.views.decorators.csrf import csrf_exempt
from integrations.metagov.models import MetagovAction, MetagovUser
from policyengine.models import Community, CommunityPlatform, CommunityRole, Proposal
from integrations.slack.models import SlackCommunity
from integrations.github.models import GithubCommunity

logger = logging.getLogger(__name__)


# INTERNAL ENDPOINT, no auth
@csrf_exempt
def internal_receive_outcome(request, id):
    if request.method != "POST" or not request.body:
        return HttpResponseBadRequest()
    try:
        body = json.loads(request.body)
    except ValueError:
        return HttpResponseBadRequest("unable to decode body")

    process_name = body["name"]
    logger.info(f"Received {process_name} metagov process update: {body}")

    try:
        proposal = Proposal.objects.get(pk=id)
    except Proposal.DoesNotExist:
        return HttpResponseNotFound()

    # Save the raw data on the Proposal
    proposal.governance_process_json = json.dumps(body)

    # For platforms that support creating BooleanVotes to track votes
    if process_name.startswith("slack."):
        community = SlackCommunity.objects.get(community__metagov_slug=body["community"])
        community.handle_metagov_process(proposal, body)
    elif process_name.startswith("github."):
        community = GithubCommunity.objects.get(community__metagov_slug=body["community"])
        community.handle_metagov_process(proposal, body)

    proposal.save()
    return HttpResponse()


# INTERNAL ENDPOINT, no auth
@csrf_exempt
def internal_receive_action(request):
    """
    Receive event from Metagov
    """

    if request.method != "POST" or not request.body:
        return HttpResponseBadRequest()
    try:
        body = json.loads(request.body)
    except ValueError:
        return HttpResponseBadRequest("unable to decode body")
    logger.info(f"Received metagov action: {body}")

    metagov_community_slug = body.get("community")

    try:
        community = Community.objects.get(metagov_slug=metagov_community_slug)
    except Community.DoesNotExist:
        logger.error(f"Received event for community {metagov_community_slug} which doesn't exist in PolicyKit")
        return HttpResponseBadRequest("Community does not exist")

    # Special cases for receiving events from "governable platforms" that have fully featured integrations

    if body.get("source") == "slack":
        # Route Slack event to the correct SlackCommunity handler
        slack_community = SlackCommunity.objects.filter(community=community).first()
        if slack_community is None:
            return HttpResponseBadRequest(f"no slack community exists for {metagov_community_slug}")
        slack_community.handle_metagov_event(body)
        return HttpResponse()

    # For all other sources, create generic MetagovActions.

    platform_community = CommunityPlatform.objects.filter(community=community).first()
    if platform_community is None:
        logger.error(f"No platforms exist for community '{community}'")
        return HttpResponse()

    # Get or create a MetagovUser that's tied to the PlatformCommunity, and give them permission to propose MetagovActions

    # Hack so MetagovUser username doesn't clash with usernames from other communities (django User requires unique username).
    # TODO(#299): make the CommunityUser model unique on community+username, not just username.
    initiator = body["initiator"]
    prefixed_username = f"{initiator['provider']}.{initiator['user_id']}"
    metagov_user, _ = MetagovUser.objects.get_or_create(
        username=prefixed_username, provider=initiator["provider"], community=platform_community
    )

    # Give this user permission to propose any MetagovAction
    user_group, usergroup_created = CommunityRole.objects.get_or_create(
        role_name="Base User", name=f"Metagov: {metagov_community_slug}: Base User"
    )
    if usergroup_created:
        user_group.community = platform_community
        permission = Permission.objects.get(codename="add_metagovaction")
        user_group.permissions.add(permission)
        user_group.save()
    user_group.user_set.add(metagov_user)

    # Create MetagovAction
    new_api_action = MetagovAction()
    new_api_action.community = platform_community
    new_api_action.initiator = metagov_user
    new_api_action.event_type = f"{body['source']}.{body['event_type']}"
    new_api_action.json_data = json.dumps(body["data"])

    new_api_action.save()
    if not new_api_action.pk:
        return HttpResponseServerError()

    logger.info(f"Created new MetagovAction with pk {new_api_action.pk}")
    return HttpResponse()
