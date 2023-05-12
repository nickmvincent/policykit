import html
import json
import logging
import os

from actstream.models import Action
from django.conf import settings
from django.contrib.auth import authenticate, get_user, login
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import Permission
from django.forms import modelform_factory
from django.http import (Http404, HttpResponse, HttpResponseBadRequest,
                         JsonResponse)
from django.http.response import HttpResponseServerError
from django.shortcuts import redirect, render

import policyengine.utils as Utils
from policyengine.integration_data import integration_data
from policyengine.linter import _lint_check
from policyengine.metagov_app import metagov, metagov_handler
from policyengine.utils import INTEGRATION_ADMIN_ROLE_NAME

logger = logging.getLogger(__name__)

DASHBOARD_MAX_USERS = 50
DASHBOARD_MAX_ACTIONS = 20


def homepage(request):
    """PolicyKit splash page"""
    return render(request, 'home.html', {})

def authorize_platform(request):
    """
    Authorize endpoint for installing & logging into Metagov-backed platforms.
    The "type" parameter indicates whether it is a user login or an installation.
    """
    platform = request.GET.get('platform')
    req_type = request.GET.get('type', 'app')
    redirect_uri = request.GET.get('redirect_uri')

    # By default, user login redirects to `/authenticate_user` endpoint for django authentication.
    # By default, app installtion redirects to `/<platform>/install` endpoint for install completion (e.g. creating the SlackCommunity).
    if redirect_uri is None:
        redirect_uri = f"{settings.SERVER_URL}/authenticate_user" if req_type == "user" else f"{settings.SERVER_URL}/{platform}/install"

    # This returns a redirect to the platform's oauth server (e.g.  https://slack.com/oauth/v2/authorize)
    # which will prompt the user to confirm. After that, it will navigate to the specified redirect_uri.
    return metagov_handler.handle_oauth_authorize(
        request,
        plugin_name=platform,
        redirect_uri=redirect_uri,
        type=req_type
    )

def authenticate_user(request):
    """
    Django authentication endpoint. This gets invoked after the platform oauth flow has successfully completed.
    """
    # Django chooses which auth backend to use (SlackBackend, DiscordBackend, etc)
    user = authenticate(request)
    if user:
        login(request, user)
        return redirect("/main")

    # TODO: better error messages
    return redirect("/login?error=login_failed")

def logout(request):
    from django.contrib.auth import logout
    logout(request)
    return redirect('/login')

def initialize_starterkit(request):
    """
    Set up starterkit policies and roles for a new community. Gets called when a user selects a starterkit on the init_startkit page
    """
    from policyengine.models import Community

    starterkit_id = request.GET.get("kit")
    community_id = request.session["starterkit_init_community_id"]
    creator_username = request.session["starterkit_init_creator_username"]
    if not community_id:
        raise Http404
    del request.session["starterkit_init_community_id"]
    del request.session["starterkit_init_creator_username"]

    community = Community.objects.get(pk=community_id)

    logger.debug(f'Initializing community {community} with starter kit {starterkit_id}...')
    cur_path = os.path.abspath(os.path.dirname(__file__))
    starter_kit_path = os.path.join(cur_path, f'../starterkits/{starterkit_id}.json')
    f = open(starter_kit_path)
    kit_data = json.loads(f.read())
    f.close()

    try:
        Utils.initialize_starterkit_inner(community, kit_data, creator_username=creator_username)
    except Exception as e:
        logger.error(f"Initializing kit {starterkit_id} raised exception {type(e).__name__} {e}")
        return redirect("/login?error=starterkit_init_failed")

    return redirect("/login?success=true")

@login_required
def dashboard(request):
    from policyengine.models import CommunityPlatform, CommunityUser, Proposal
    user = get_user(request)
    community = user.community.community

    # List all CommunityUsers across all platforms connected to this community
    users = CommunityUser.objects.filter(community__community=community)[:DASHBOARD_MAX_USERS]

    # List recent actions across all CommunityPlatforms connected to this community
    platform_communities = CommunityPlatform.objects.filter(community=community)
    action_log = Action.objects.filter(data__community_id__in=[cp.pk for cp in platform_communities])[:DASHBOARD_MAX_ACTIONS]

    # List pending proposals for all Policies connected to this community
    pending_proposals = Proposal.objects.filter(
        policy__community=community,
        status=Proposal.PROPOSED
    ).order_by("-proposal_time")

    return render(request, 'policyadmin/dashboard/index.html', {
        'user': user,
        'users': users,
        'roles': community.get_roles(),
        'docs': community.get_documents(),
        'platform_policies': community.get_platform_policies(),
        'constitution_policies': community.get_constitution_policies(),
        'trigger_policies': community.get_trigger_policies(),
        'action_log': action_log,
        'pending_proposals': pending_proposals
    })


@login_required
def settings_page(request):
    """
    Settings page for enabling/disabling platform integrations.
    """
    user = get_user(request)
    community = user.community

    context = {
        "user": user,
        "enabled_integrations": [],
        "disabled_integrations": []
    }

    if community.metagov_slug:
        enabled_integrations = {}
        # Iterate through all Metagov Plugins enabled for this community
        for plugin in metagov.get_community(community.metagov_slug).plugins.all():
            integration = plugin.name
            if integration not in integration_data.keys():
                logger.warn(f"unsupported integration {integration} is enabled for community {community}")
                continue

            # Only include configs if user has permission, since they may contain API Keys
            config_tuples = []
            if user.has_role(INTEGRATION_ADMIN_ROLE_NAME):
                for (k,v) in plugin.config.items():
                    readable_key = k.replace("_", " ").replace("-", " ").capitalize()
                    config_tuples.append((readable_key, v))

            # Add additional data about the integration, like description and webhook URL
            additional_data = integration_data[integration]
            if additional_data.get("webhook_instructions"):
                additional_data["webhook_url"] = f"{settings.SERVER_URL}/api/hooks/{plugin.name}/{plugin.community.slug}"

            enabled_integrations[integration] = {**plugin.serialize(), **additional_data, "config": config_tuples}


        context["enabled_integrations"] = enabled_integrations.items()
        context["disabled_integrations"] = [(k, v) for (k,v) in integration_data.items() if k not in enabled_integrations.keys()]

    return render(request, 'policyadmin/dashboard/settings.html', context)

@login_required
def add_integration(request):
    """
    This view renders a form for enabling an integration, OR initiates an oauth install flow.
    """
    integration = request.GET.get("integration")
    user = get_user(request)
    community = user.community

    metadata = metagov.get_plugin_metadata(integration)

    if metadata["auth_type"] == "oauth":
        return metagov_handler.handle_oauth_authorize(
            request,
            plugin_name=integration,
            redirect_uri=f"{settings.SERVER_URL}/{integration}/install",
            community_slug=community.metagov_slug,
        )


    context = {
        "integration": integration,
        "metadata": metadata,
        "metadata_string": json.dumps(metadata),
        "additional_data": integration_data[integration]
    }
    return render(request, 'policyadmin/dashboard/enable_integration_form.html', context)


@login_required
@permission_required("constitution.can_add_integration", raise_exception=True)
def enable_integration(request, integration):
    """
    API Endpoint to enable a Metagov Plugin. This gets called on config form submission from JS.
    This is the default implementation; platforms with PolicyKit integrations may override it.
    """
    user = get_user(request)
    community = user.community.community

    config = json.loads(request.body)
    logger.debug(f"Enabling {integration} with config {config} for {community}")
    plugin = metagov.get_community(community.metagov_slug).enable_plugin(integration, config)

    # Create the corresponding CommunityPlatform instance
    from django.apps import apps
    cls =  apps.get_app_config(integration).get_model(f"{integration}community")
    cp,created = cls.objects.get_or_create(
        community=community,
        team_id=plugin.community_platform_id,
        defaults={"community_name": plugin.community_platform_id}
    )
    logger.debug(f"CommunityPlatform '{cp.platform} {cp}' {'created' if created else 'already exists'}")

    return HttpResponse()


@login_required
@permission_required("constitution.can_remove_integration", raise_exception=True)
def disable_integration(request, integration):
    """
    API Endpoint to disable a Metagov plugin (navigated to from Settings page).
    This is the default implementation; platforms with PolicyKit integrations may override it.
    """
    id = int(request.GET.get("id")) # id of the plugin
    user = get_user(request)
    community = user.community.community
    logger.debug(f"Deleting plugin {integration} {id} for community {community}")

    # Delete the Metagov Plugin
    metagov.get_community(community.metagov_slug).disable_plugin(integration, id=id)

    # Delete the PlatformCommunity
    community_platform = community.get_platform_community(name=integration)
    if community_platform:
        community_platform.delete()

    return redirect("/main/settings")

@login_required
def editor(request):
    kind = request.GET.get('type', "platform").lower()
    operation = request.GET.get('operation', "Add")
    policy_id = request.GET.get('policy')

    user = get_user(request)
    community = user.community.community

    from policyengine.models import Policy, PolicyActionKind
    if kind not in [PolicyActionKind.PLATFORM, PolicyActionKind.CONSTITUTION, PolicyActionKind.TRIGGER]:
        raise Http404("Policy does not exist")

    policy = None
    if policy_id:
        try:
            policy = Policy.objects.get(id=policy_id, community=community)
        except Policy.DoesNotExist:
            raise Http404("Policy does not exist")

    # which action types to show in the dropdown
    actions = Utils.get_action_types(community, kinds=[kind])

    # list of autocomplete strings
    action_types = [a.codename for a in policy.action_types.all()] if policy else None
    autocompletes = Utils.get_autocompletes(community, action_types=action_types, policy=policy)

    data = {
        'user': get_user(request),
        'type': kind.capitalize(),
        'operation': operation,
        'actions': actions.items(),
        'autocompletes': json.dumps(autocompletes)
    }

    if policy:
        data['policy'] = policy_id
        data['name'] = policy.name
        data['description'] = policy.description
        data['filter'] = policy.filter
        data['initialize'] = policy.initialize
        data['check'] = policy.check
        data['notify'] = policy.notify
        data['success'] = policy.success
        data['fail'] = policy.fail
        data['action_types'] = list(policy.action_types.all().values_list('codename', flat=True))
        data['variables'] = policy.variables.all()

    return render(request, 'policyadmin/dashboard/editor.html', data)

@login_required
def selectrole(request):
    from policyengine.models import CommunityRole

    user = get_user(request)
    operation = request.GET.get('operation')

    roles = user.community.community.get_roles()

    return render(request, 'policyadmin/dashboard/role_select.html', {
        'user': user,
        'roles': roles,
        'operation': operation
    })

@login_required
def roleusers(request):
    from policyengine.models import CommunityRole, CommunityUser

    user = get_user(request)
    operation = request.GET.get('operation')

    community = user.community.community
    roles = community.get_roles()
    users = {}
    for cp in community.get_platform_communities():
        users[cp.platform] = CommunityUser.objects.filter(community=cp).order_by('readable_name', 'username')

    return render(request, 'policyadmin/dashboard/role_users.html', {
        'roles': roles,
        'users': users.items(),
        'operation': operation
    })

@login_required
def roleeditor(request):
    from policyengine.models import CommunityPlatform, CommunityRole

    user = get_user(request)
    operation = request.GET.get('operation')
    role_pk = request.GET.get('role')

    # List permissions for all CommunityPlatforms connected to this community
    platforms = [c.platform for c in CommunityPlatform.objects.filter(community=user.community.community)]
    permissions = Utils.get_all_permissions(platforms).values_list('name', flat=True)

    data = {
        'user': user,
        'permissions': list(sorted(permissions)),
        'operation': operation
    }

    if role_pk:
        try:
            role = CommunityRole.objects.get(pk=role_pk, community=user.community.community)
        except CommunityRole.DoesNotExist:
            raise Http404("Role does not exist")
        data['role_name'] = role.role_name
        data['name'] = role.name
        data['description'] = role.description
        currentPermissions = list(role.permissions.filter(name__in=permissions).values_list('name', flat=True))
        data['currentPermissions'] = currentPermissions

    return render(request, 'policyadmin/dashboard/role_editor.html', data)

@login_required
def selectpolicy(request):
    user = get_user(request)
    policies = None
    type = request.GET.get('type')
    operation = request.GET.get('operation')

    show_active_policies = True
    if operation == 'Recover':
        show_active_policies = False

    if type == 'Platform':
        policies = user.community.community.get_platform_policies(is_active=show_active_policies)
    elif type == 'Constitution':
        policies = user.community.community.get_constitution_policies(is_active=show_active_policies)
    elif type == 'Trigger':
        policies = user.community.community.get_trigger_policies(is_active=show_active_policies)
    else:
        return HttpResponseBadRequest()

    return render(request, 'policyadmin/dashboard/policy_select.html', {
        'user': get_user(request),
        'policies': policies,
        'type': type,
        'operation': operation
    })

@login_required
def selectdocument(request):
    user = get_user(request)
    operation = request.GET.get('operation')

    show_active_documents = True
    if operation == 'Recover':
        show_active_documents = False

    documents = user.community.community.get_documents(is_active=show_active_documents)

    return render(request, 'policyadmin/dashboard/document_select.html', {
        'user': get_user(request),
        'documents': documents,
        'operation': operation
    })

@login_required
def documenteditor(request):
    from policyengine.models import CommunityDoc

    user = get_user(request)
    operation = request.GET.get('operation')
    doc_id = request.GET.get('doc')

    data = {
        'user': user,
        'operation': operation
    }

    if doc_id:
        try:
            doc = CommunityDoc.objects.get(id=doc_id, community=user.community.community)
        except CommunityDoc.DoesNotExist:
            raise Http404("Document does not exist")

        data['name'] = doc.name
        data['text'] = doc.text

    return render(request, 'policyadmin/dashboard/document_editor.html', data)

@login_required
def actions(request):
    user = get_user(request)
    community = user.community.community

    from policyengine.models import PolicyActionKind
    actions = Utils.get_action_types(community, kinds=[PolicyActionKind.PLATFORM])
    return render(request, 'policyadmin/dashboard/actions.html', {
        'user': get_user(request),
        'actions': actions.items()
    })

@login_required
def propose_action(request, app_name, codename):
    cls = Utils.find_action_cls(codename, app_name)
    if not cls:
        return HttpResponseBadRequest()

    from policyengine.models import GovernableActionForm, Proposal

    ActionForm = modelform_factory(
        cls,
        form=GovernableActionForm,
        fields=getattr(cls, "EXECUTE_PARAMETERS", "__all__"),
        localized_fields="__all__"
    )

    new_action = None
    proposal = None
    if request.method == 'POST':
        form = ActionForm(request.POST, request.FILES)
        if form.is_valid():
            new_action = form.save(commit=False)
            if request.user.community.platform == app_name:
                # user is logged in with the same platform that this action is for
                new_action.initiator = request.user
                new_action.community = request.user.community
            else:
                # user is logged in with a different platform. no initiator.
                new_action.community = request.user.community.community.get_platform_community(app_name)
            new_action.save()
            proposal = Proposal.objects.filter(action=new_action).first()
    else:
        form = ActionForm()
    return render(
        request,
        "policyadmin/dashboard/action_proposer.html",
        {
            "user": get_user(request),
            "form": form,
            "app_name": app_name,
            "codename": codename,
            "verbose_name": cls._meta.verbose_name.title(),
            "action": new_action,
            "proposal": proposal,
        },
    )

@login_required
def get_autocompletes(request):
    user = request.user
    community = user.community.community
    action_types = request.GET.get("action_types").split(",")
    if not action_types or len(action_types) == 1 and not action_types[0]:
        action_types = None
    autocompletes = Utils.get_autocompletes(community, action_types=action_types)
    return JsonResponse({'autocompletes': autocompletes})

@login_required
def error_check(request):
    """
    Takes a request object containing Python code data. Calls _lint_check(code)
    to check provided Python code for errors.
    Returns a JSON response containing the output and errors from linting.
    """
    data = json.loads(request.body)
    code = data['code']
    function_name = data['function_name']
    errors = _lint_check(code, function_name)
    return JsonResponse({'errors': errors})

@login_required
def policy_action_save(request):
    from constitution.models import (ActionType, PolicyActionKind,
                                     PolicykitAddConstitutionPolicy,
                                     PolicykitAddPlatformPolicy,
                                     PolicykitAddTriggerPolicy,
                                     PolicykitChangeConstitutionPolicy,
                                     PolicykitChangePlatformPolicy,
                                     PolicykitChangeTriggerPolicy)

    from policyengine.models import Policy, PolicyVariable

    data = json.loads(request.body)
    user = get_user(request)

    action = None
    operation = data['operation']
    kind = data['type'].lower()

    if kind not in [PolicyActionKind.PLATFORM, PolicyActionKind.CONSTITUTION, PolicyActionKind.TRIGGER]:
        raise Http404("Policy does not exist")

    if operation == "Add":
        if kind == PolicyActionKind.CONSTITUTION:
            action = PolicykitAddConstitutionPolicy()
        elif kind == PolicyActionKind.PLATFORM:
            action = PolicykitAddPlatformPolicy()
        elif kind == PolicyActionKind.TRIGGER:
            action = PolicykitAddTriggerPolicy()
        # action.is_bundled = data.get('is_bundled', False)

    elif operation == "Change":
        if kind == PolicyActionKind.CONSTITUTION:
            action = PolicykitChangeConstitutionPolicy()
        elif kind == PolicyActionKind.PLATFORM:
            action = PolicykitChangePlatformPolicy()
        elif kind == PolicyActionKind.TRIGGER:
            action = PolicykitChangeTriggerPolicy()

        community = user.community.community

        try:
            action.policy = Policy.objects.get(pk=data['policy'], community=community)
        except Policy.DoesNotExist:
            raise Http404("Policy does not exist")

    else:
        raise Http404("Policy does not exist")


    action.community = user.constitution_community
    action.initiator = user
    action.name = data['name']
    action.description = data.get('description', None)
    action.filter = data['filter']
    action.initialize = data['initialize']
    action.check = data['check']
    action.notify = data['notify']
    action.success = data['success']
    action.fail = data['fail']

    if not data["name"]:
        return HttpResponseBadRequest("Enter a name.")
    if len(data["action_types"]) < 1:
        if action and hasattr(action, "policy") and action.policy.action_types.count() == 0 and kind != PolicyActionKind.TRIGGER:
            pass # the policy already had no action types, so it's a base policy. ignore
        else:
            return HttpResponseBadRequest("Select one or more action types.")

    try:
        action.save(evaluate_action=False)
    except Exception as e:
        logger.error(f"Error saving policy: {e}")
        return HttpResponseServerError()

    action_types = [ActionType.objects.get_or_create(codename=codename)[0] for codename in data["action_types"]]
    action.action_types.set(action_types)

    if "variables" in data:
        action.variables = data["variables"]

        try:
            action.parse_policy_variables(validate=True, save=False)
        except Exception as e:
            return HttpResponseBadRequest(e)

    try:
        action.save(evaluate_action=True)
    except Exception as e:
        logger.error(f"Error evaluating policy: {e}")
        return HttpResponseServerError()

    return HttpResponse()

@login_required
def policy_action_remove(request):
    from constitution.models import (PolicykitRemoveConstitutionPolicy,
                                     PolicykitRemovePlatformPolicy,
                                     PolicykitRemoveTriggerPolicy)

    from policyengine.models import Policy

    data = json.loads(request.body)
    user = get_user(request)

    action = None
    try:
        policy = Policy.objects.get(pk=data['policy'], community=user.community.community)
    except Policy.DoesNotExist:
        raise Http404("Policy does not exist")

    if policy.kind == Policy.CONSTITUTION:
        action = PolicykitRemoveConstitutionPolicy()
    elif policy.kind == Policy.PLATFORM:
        action = PolicykitRemovePlatformPolicy()
    elif policy.kind == Policy.TRIGGER:
        action = PolicykitRemoveTriggerPolicy()
    else:
        return HttpResponseBadRequest()

    action.policy = policy
    action.community = user.constitution_community
    action.initiator = user
    action.save()

    return HttpResponse()

@login_required
def policy_action_recover(request):
    from constitution.models import (PolicykitRecoverConstitutionPolicy,
                                     PolicykitRecoverPlatformPolicy,
                                     PolicykitRecoverTriggerPolicy)

    from policyengine.models import Policy

    data = json.loads(request.body)
    user = get_user(request)

    action = None
    try:
        policy = Policy.objects.get(pk=data['policy'], community=user.community.community)
    except Policy.DoesNotExist:
        raise Http404("Policy does not exist")

    if policy.kind == Policy.CONSTITUTION:
        action = PolicykitRecoverConstitutionPolicy()
    elif policy.kind == Policy.PLATFORM:
        action = PolicykitRecoverPlatformPolicy()
    elif policy.kind == Policy.TRIGGER:
        action = PolicykitRecoverTriggerPolicy()
    else:
        return HttpResponseBadRequest(f"Unrecognized policy kind: {policy.kind}")

    action.policy = policy
    action.community = user.constitution_community
    action.initiator = user
    action.save()

    return HttpResponse()

@login_required
def role_action_save(request):
    from constitution.models import PolicykitAddRole, PolicykitEditRole

    from policyengine.models import CommunityRole

    data = json.loads(request.body)
    user = get_user(request)

    action = None
    if data['operation'] == 'Add':
        action = PolicykitAddRole()
    elif data['operation'] == 'Change':
        action = PolicykitEditRole()
        action.role = CommunityRole.objects.filter(name=html.unescape(data['name']))[0]
    else:
        return HttpResponseBadRequest()

    action.community = user.constitution_community
    action.initiator = user
    action.name = data['role_name']
    action.description = data['description']
    action.save(evaluate_action=False)
    action.permissions.set(Permission.objects.filter(name__in=data['permissions']))
    action.save(evaluate_action=True)

    return HttpResponse()

@login_required
def role_action_users(request):
    from constitution.models import (PolicykitAddUserRole,
                                     PolicykitRemoveUserRole)

    from policyengine.models import CommunityRole, CommunityUser

    data = json.loads(request.body)
    user = get_user(request)

    action = None
    if data['operation'] == 'Add':
        action = PolicykitAddUserRole()
    elif data['operation'] == 'Remove':
        action = PolicykitRemoveUserRole()
    else:
        return HttpResponseBadRequest()

    action.community = user.constitution_community
    action.initiator = user
    action.role = CommunityRole.objects.filter(name=data['role'])[0]
    action.save(evaluate_action=False)
    action.users.set(CommunityUser.objects.filter(username=data['user']))
    action.save(evaluate_action=True)

    return HttpResponse()

@login_required
def role_action_remove(request):
    from constitution.models import PolicykitDeleteRole

    from policyengine.models import CommunityRole

    data = json.loads(request.body)
    user = get_user(request)

    action = PolicykitDeleteRole()
    action.community = user.constitution_community
    action.initiator = user
    try:
        action.role = CommunityRole.objects.get(pk=data['role'], community=user.community.community)
    except CommunityRole.DoesNotExist:
        raise Http404("Role does not exist")
    action.save()

    return HttpResponse()

@login_required
def document_action_save(request):
    from constitution.models import (PolicykitAddCommunityDoc,
                                     PolicykitChangeCommunityDoc)

    from policyengine.models import CommunityDoc

    data = json.loads(request.body)
    user = get_user(request)

    action = None
    if data['operation'] == 'Add':
        action = PolicykitAddCommunityDoc()
    elif data['operation'] == 'Change':
        action = PolicykitChangeCommunityDoc()
        try:
            action.doc = CommunityDoc.objects.get(id=data['doc'], community=user.community.community)
        except CommunityDoc.DoesNotExist:
            raise Http404("Document does not exist")
    else:
        return HttpResponseBadRequest()

    action.community = user.constitution_community
    action.initiator = user
    action.name = data['name']
    action.text = data['text']
    action.save()

    return HttpResponse()

@login_required
def document_action_remove(request):
    from constitution.models import PolicykitDeleteCommunityDoc

    from policyengine.models import CommunityDoc

    data = json.loads(request.body)
    user = get_user(request)

    action = PolicykitDeleteCommunityDoc()
    action.community = user.constitution_community
    action.initiator = user
    try:
        action.doc = CommunityDoc.objects.get(id=data['doc'], community=user.community.community)
    except CommunityDoc.DoesNotExist:
        raise Http404("Document does not exist")
    action.save()

    return HttpResponse()

@login_required
def document_action_recover(request):
    from constitution.models import PolicykitRecoverCommunityDoc

    from policyengine.models import CommunityDoc

    data = json.loads(request.body)
    user = get_user(request)

    action = PolicykitRecoverCommunityDoc()
    action.community = user.constitution_community
    action.initiator = user
    try:
        action.doc = CommunityDoc.objects.get(id=data['doc'], community=user.community.community)
    except CommunityDoc.DoesNotExist:
        raise Http404("Document does not exist")
    action.save()

    return HttpResponse()

def policy_from_request(request, key_name = 'policy'):
    policy_id = request.GET.get(key_name)
    from policyengine.models import Policy
    try:
        return Policy.objects.get(pk=policy_id)
    except Policy.DoesNotExist:
        raise Http404("Policy does not exist")


#===
# COLLECTIVE VOICE code below
# Eventually, this should be in a separate "apps" folder
# But probably not a separate "django app".
# ===

def get_channel_options(community_id):
    """
    Get channel ids and names from Slack
    """
    channel_options = []
    from integrations.slack.models import SlackCommunity
    slack_community = SlackCommunity.objects.get(community_id=community_id)
    for channel in slack_community.get_conversations()['channels']:
        if channel['is_channel']: # get only the "channels" (as opposed to group, im, mpim, private)
            channel_options.append(
                {'name': channel['name'], 'channel_id': channel['id']}
            )
    return channel_options

def create_blank_policytemplate(community):
    from policyengine.models import PolicyTemplate
    policytemplate = PolicyTemplate.objects.create(
        kind="trigger",
        name=f"collectivevoice_{community.id}",
    )
    return policytemplate

# def create_cv_policytemplate(community):
#     from policyengine.models import PolicyTemplate, PolicyVariable, ActionType
#     source = PolicyTemplate.objects.get(name="collectivevoicebase")

#     policy = PolicyTemplate.objects.create(
#         kind="trigger",
#         name="collectivevoice",
#         community=community,

#         filter=source.filter,
#         initialize=source.initialize,
#         check=source.check,
#         notify=source.notify,
#         success=source.success,
#         fail=source.fail,
#         description=source.description
#     )

#     action_type, _ = ActionType.objects.get_or_create(codename="expensecreated")
#     policy.action_types.add(action_type)

#     vars = source.variables.all()

#     for variable in vars:
#         PolicyVariable.objects.create(
#             name=variable.name, label=variable.label, default_value=variable.default_value, is_required=True,
#             prompt=variable.prompt, type=variable.type)

#     channel_options = []
#     # get channel names from Slack API if needed
#     if any(['channel' in x.name for x in vars]):
#         channel_options = get_channel_options(community.id)

#     return policy, channel_options


def get_collectivevoice_policytemplate_from_request(request):
    from policyengine.models import PolicyTemplate
    user = get_user(request)
    community = user.community.community

    try:
        policytemplate = PolicyTemplate.objects.get(name=f"collectivevoice_{community.id}")
    except:
        policytemplate = create_blank_policytemplate(community=community)
    return policytemplate

def collectivevoice_summary_data(pt):
    """
    Given a PolicyTemplate (`pt`) created with collective voice, summarize the 
    "Expenses", "Voting Type", and "Follow up actions"
    """
    pass


@login_required
def collectivevoice_home(request):
    """
    Show the home screen for CV
    If they've already gone through the flow, show policy details.
    Otherwise, show buttons to edit EXPENSES, VOTING TEMPLATE, and FOLLOW UP

    Operates on three main objects:
        - a CustomAction (with FilterModules),
        - a Procedure,
        - and extra_executions attribute of the PolicyTemplate
    """
    from policyengine.models import PolicyTemplate
    from django.core import serializers

    # Utils.load_templates("Procedure")
    # Utils.load_templates("CheckModule")
    # Utils.load_templates("FilterModule")

    # pt = get_collectivevoice_policytemplate_from_request(request)
    policy_id = request.GET.get("policy_id")
    try:
        pt = PolicyTemplate.objects.get(pk=policy_id)
    except:
        pt = PolicyTemplate.objects.create(name="collectivevoice_tmp")



    expenses_set = False
    filter_names = []
    if len(pt.custom_actions.all()) > 0:
        expenses_set = True
    #     for action in pt.custom_actions.all():
    #         filter_names.append(action.name)
    # filter_names_str = ",".join(filter_names)


    voting_set = False
    procedure_name = None
    if pt.procedure is not None:
        voting_set = True
        procedure_name = pt.procedure.name        

    followup_set = False
    if len(pt.extra_executions) > 2: # default is '{}'
        followup_set = True

    pt_data = serializers.serialize('json', [pt,])
    
    return render(request, "collectivevoice/home.html", {
        'policytemplate': pt_data,
        'policy_id': pt.id,
        'expenses_set': expenses_set,
        'voting_set': voting_set,
        'followup_set': followup_set,
        'procedure_name': procedure_name,
        # 'filter_names_str': filter_names_str
    })

@login_required
def collectivevoice_edit_expenses(request):
    """
    User can select which expenses will trigger votes
    """
    from policyengine.models import PolicyActionKind, FilterModule
    policy_id = request.GET.get("policy_id")

    filter_parameters = {}
    new_actions = {}

    user = get_user(request)
    # only get Trigger actions for OpenCollective
    actions = Utils.get_action_types(user.community.community, kinds=[PolicyActionKind.TRIGGER])
    app_name = "opencollective"
    action_list = actions[app_name]
    new_action_list = []
    for action_code, verbose_name in action_list:
        parameter = Utils.get_filter_parameters(app_name, action_code)
        # only show actions that have filter parameters
        if parameter:
            filter_parameters[action_code] = parameter
            new_action_list.append((action_code, verbose_name))
    # only show apps that have at least one action with filter parameters
    if new_action_list:
        new_actions[app_name] = new_action_list

    filter_modules = {}
    for app_name in new_actions:
        filter_modules[app_name] = {}
        filters_per_app = FilterModule.objects.filter(platform__in=[app_name, "All"])
        # get distinct filter kinds for each app
        filter_kinds = list(filters_per_app.values_list('kind', flat=True).distinct())
        for kind in filter_kinds:
            filter_modules[app_name][kind] = []
            for filter in filters_per_app.filter(kind=kind):
                filter_modules[app_name][kind].append({
                    "pk": filter.pk, 
                    "name": filter.name,
                    "description": filter.description, 
                    "variables": filter.loads("variables")
                })

    entities = Utils.load_entities(user.community)
    return render(request, "collectivevoice/edit_expenses.html", {
        "trigger": True,
        "actions": new_actions, # this variable is only used in html template and therefore no dump is needed
        "filter_parameters": json.dumps(filter_parameters), # this variable is used in javascript and therefore needs to be dumped
        "filter_modules": json.dumps(filter_modules),
        "entities": json.dumps(entities),
        "policy_id": policy_id,
    })

@login_required
def collectivevoice_edit_voting(request):
    """
    c.f. design_procedures
    """
    from policyengine.models import Procedure      

    procedure_objects = Procedure.objects.all()
    procedures = []
    procedure_details = []
    # keep variables in a different dict simply to avoid escaping problems of nested quotes
    # the first is to use directly in template rendering, while the second is to use in javascript
    for template in procedure_objects:
        procedures.append({
            "name": template.name, 
            "pk": template.pk, 
            "platform": template.platform,     
            "description": template.description

        })
            
        procedure_details.append({
            "name": template.name, 
            "pk": template.pk, 
            "variables": template.loads("variables"),
        })
    
    # Only Slack for v0.1 of CollectiveVoice
    platform_names = ["slack"]

    user = get_user(request)
    # # platforms = user.community.community.get_platform_communities()
    # # platform_names = [platform.platform for platform in platforms]

    trigger = request.GET.get("trigger", "false")
    policy_id = request.GET.get("policy_id")
    entities = Utils.load_entities(user.community)
    return render(request, "collectivevoice/edit_voting.html", {
        "procedures": json.dumps(procedures),
        "procedure_details": json.dumps(procedure_details),
        "platforms": platform_names,
        "trigger": trigger,
        "policy_id": policy_id,
        "entities": json.dumps(entities)
    })

@login_required
def collectivevoice_edit_followup(request):
    """cf no-code design_executions"""
    policy_id = request.GET.get("policy_id", None)
    # "success" or "fail"

    if policy_id:
        user = get_user(request)
        executable_actions, execution_variables = Utils.extract_executable_actions(user.community.community)
        entities = Utils.load_entities(user.community)
        return render(request, "collectivevoice/edit_followup.html", {
            "policy_id": policy_id,
            "executions": executable_actions,
            "execution_variables": json.dumps(execution_variables),
            "entities": json.dumps(entities)
        })



@login_required
def create_custom_action(request):
    """see no-code create_custom_action for latest approach"""
    from policyengine.models import CustomAction, ActionType, PolicyTemplate, FilterModule

    data = json.loads(request.body)
    filters = data.get("filters", None)

    pt = PolicyTemplate.objects.get(id=data.get("policy_id"))
    
    # pt = get_collectivevoice_policytemplate_from_request(request)
    # pt.action_types.clear()
    pt.custom_actions.all().delete()

    # only create a new PolicyTemplate instance when there is at least one filter specified
    if filters and len(filters) > 0:
        is_trigger = True
        for filter in filters:
            action_type = filter.get("action_type")
            action_type = ActionType.objects.filter(codename=action_type).first()
            
            action_specs = filter.get("filter")
            '''
                check whether the value of each action_specs is an empty string
                create a new CustomAction instance for each selected action that has specified filter parameters
                and only search the action_type for any selected action without specified filter parameters
                an example of a action_specs:
                    {
                        "initiator":{"filter_pk":"72", "platform": "slack", "variables":{"role":"test"}},
                        "text":{}
                    }
                    
            '''
            empty_filter = not any(["filter_pk" in value for value in list(action_specs.values()) ])
            filter_JSON = {}
            if empty_filter:
                pt.action_types.add(action_type)
            else:
                custom_action = CustomAction.objects.create(
                    action_type=action_type, is_trigger=is_trigger
                )
                for field, filter_info in action_specs.items():
                    if not filter_info:
                        filter_JSON[field] = None
                    else:
                        filter_module = FilterModule.objects.filter(pk=int(filter_info["filter_pk"])).first()
                        # create a filter JSON object with the actual value specified for each variable
                        filter_JSON[field] = filter_module.to_json(filter_info["variables"])
                        # to faciliate the generation of codes for custom actions, we store the platform of each filter
                        filter_JSON[field]["platform"] = filter_info["platform"]
                custom_action.dumps("filter", filter_JSON)
                custom_action.save()
                pt.custom_actions.add(custom_action)                

        pt.save()
        return JsonResponse({"policy_id": pt.pk, "status": "success"})
    else:
        return JsonResponse({"status": "fail"})
    
@login_required  
def create_procedure(request):
    '''
        Create the procedure field of a PolicyTemplate instance based on the request body.
        We also add variables defined in the selected procedure to the new policytemplate instance

        Parameters:
            request.body: 
                A Json object in the shape of
                {  
                    "procedure_index": an integer, which represents the primary key of the selected procedure;
                    "policy_id": an integer, which represents the primary key of the policy that we are creating
                    "procedure_variables": a dict of variable names and their values
                }
    '''
    from policyengine.models import Procedure, PolicyTemplate

    data = json.loads(request.body)
    procedure_index = data.get("procedure_index", None)
    policy_id = data.get("policy_id", None)
    if procedure_index and policy_id:
        # why first?
        procedure = Procedure.objects.filter(pk=procedure_index).first()
        pt = PolicyTemplate.objects.filter(pk=policy_id).first()
        if pt and procedure:
            logger.debug("clearing old variables in case user is changing the voting rule")
            pt.variables = "[]"
            pt.data = "[]"

            logger.debug("creating variables for the new policy")
            pt.procedure = procedure
            pt.add_variables(procedure.loads("variables"), data.get("procedure_variables", {}))
            pt.add_descriptive_data(procedure.loads("data"))
            pt.save()

            return JsonResponse({"status": "success", "policy_id": pt.pk})
    return JsonResponse({"status": "fail"})


@login_required
def customize_procedure(request):
    """
        Help render the customize procedure page
    """
    from policyengine.models import CheckModule, PolicyTemplate
    
    # prepare information about module templates
    checkmodules_objects = CheckModule.objects.all()
    checkmodules = []
    checkmodules_details = []
    for template in checkmodules_objects:
        checkmodules.append((template.pk, template.name))
        checkmodules_details.append({
            "name": template.name, 
            "pk": template.pk, 
            "variables": template.loads("variables")
        })

    # prepare information about extra executions that are supported
    user = get_user(request)
    executable_actions, execution_variables = Utils.extract_executable_actions(user.community.community)

    
    trigger = request.GET.get("trigger", "false")
    policy_id = request.GET.get("policy_id")
    entities = Utils.load_entities(user.community)
    data = {
            "checkmodules": checkmodules,
            "checkmodules_details": json.dumps(checkmodules_details),
            "executions": executable_actions,
            "execution_variables": json.dumps(execution_variables),
            "trigger": trigger,
            "policy_id": policy_id,
            "entities": json.dumps(entities)
        }

    now_policy = PolicyTemplate.objects.filter(pk=policy_id).first()
    data["policy_variables"] = json.dumps(
            now_policy.loads("variables") if now_policy else {}
        )
    return render(request, "no-code/customize_procedure.html", data)

@login_required
def create_customization(request):
    """
        Add extra check modules and extra actions to the policy template

        parameters:
            request.body: e.g.,
                {
                    "policy_id": 1,
                    
                    "module_index": 1,
                    "module_data": {
                        "duration": ...
                    }

                    "action_data": {
                        "check"/"notify": {
                            "action": "slackpostmessage",
                            "channel": ...,
                            "text": ...
                        }
                    }
                }
    """
    from policyengine.models import CheckModule, PolicyTemplate

    data = json.loads(request.body)
    
    policy_id = data.get("policy_id", None)
    new_policy = PolicyTemplate.objects.filter(pk=policy_id).first()
    if new_policy:
        module_index = data.get("module_index", None)
        module_template = CheckModule.objects.filter(pk=module_index).first()
        if module_template:
            new_policy.add_check_module(module_template)
            new_policy.add_variables(module_template.loads("variables"), data.get("module_data", {}))
            new_policy.add_descriptive_data(module_template.loads("data"))
        action_data = data.get("action_data", None)
        if action_data:
            new_policy.add_extra_actions(action_data)
        new_policy.save()
        return JsonResponse({"status": "success", "policy_id": new_policy.pk})
    return JsonResponse({"status": "fail"})


    
def create_execution(request):
    """
        Add executions to success or fail blocks of the policytemplate instance

        parameters:
            request.body: e.g.,
                "action_data":  
                    {
                        "success"/"fail": {
                            "action": "slackpostmessage",
                            "channel": ...,
                            "text": ...
                        }
                    }
    """
    from policyengine.models import PolicyTemplate

    data = json.loads(request.body)
    policy_id = data.get("policy_id", None)
    pt = PolicyTemplate.objects.filter(pk=policy_id).first()
    if pt:
        action_data = data.get("action_data", {})
        if action_data:
            pt.add_extra_actions(action_data)
            pt.save()
        return JsonResponse({"status": "success", "policy_id": pt.pk})
    return JsonResponse({"status": "fail"})

@login_required 
def policy_overview(request):
    """
        help render the policy overview page where users can fill in the policy name and description,
        and also see the policy template in json format
    """
    from policyengine.models import PolicyTemplate

    policy_id = request.GET.get("policy_id", None)
    created_policy = PolicyTemplate.objects.filter(pk=policy_id).first()
    if created_policy:
        created_policy_json = created_policy.to_json()
        return render(request, "collectivevoice/policy_overview.html", {
            "policy": json.dumps(created_policy_json),
            "policy_id": policy_id,
        })

@login_required  
def create_overview(request):
    """
        Add policy name and description to the policy template instance

        parameters:
            request.body: 
                {
                    "policy_id": 1,
                    data: {
                        "name": "policy name",
                        "description": "policy description"
                    }
                }
    """
    from policyengine.models import PolicyTemplate
    request_body = json.loads(request.body)
    policy_id = int(request_body.get("policy_id", -1))
    policy_template = PolicyTemplate.objects.filter(pk=int(policy_id)).first()
    if policy_template :
        data = request_body.get("data")
        policy_template.name = data.get("name", "")
        policy_template.description = data.get("description", "")
        # NMV: hard-code for CollectiveVoice
        policy_template.kind = "trigger"
        policy_template.save()

        user = get_user(request)
        new_policy = policy_template.create_policy(user.community.community, policy_template)
        return JsonResponse({"policy_id": new_policy.pk, "policy_type": (new_policy.kind).capitalize(), "status": "success"})
    else:
        return JsonResponse({"status": "fail"})


# @login_required
# def embed_select_template(request):
#     """
#     Select a template for Collective Voice

#     DB must be populated with `is_template=True` Policies. If not, hit
#     the populate_templates endpoint to populate.
#     """
#     from policyengine.models import Policy
#     import policyengine.utils as Utils

#     user = get_user(request)
#     reload = request.GET.get('reload', False)
#     if reload:
#         Utils.create_policy_from_json(user.community.community)

#     template_policies = Policy.objects.filter(is_template=True)

#     return render(request, "collectivevoice/select_template.html", {
#         'template_policies': template_policies
#     })

# def embed_initial(request):
#     user = get_user(request)
#     community = user.community.community
#     policy_source = policy_from_request(request, key_name="source")

#     # Variables without a default value or value are set in the first step of the flow
#     initial_variables = policy_source.variables.filter(default_value__exact="", value__exact="")

#     channel_options = []
#     # only get channel names from Slack API if needed
#     if any(['channel' in x.name for x in initial_variables]):
#         channel_options = get_channel_options(community.id)

#     # Variables are ordered with initial variables first
#     all_variables = policy_source.variables.order_by("default_value")

#     return render(request, "collectivevoice/initial.html", {
#         "policy": policy_source,
#         "initial_variables": initial_variables,
#         "channel_options": channel_options,
#         "all_variables": all_variables
#     })

# def embed_setup(request):
#     """
#     This view is hit when the user clicks "Continue" from initial.html (embed_initial view)
#     This gets the current user and community, then makes a copy of the "starter" template policy
#     and adds it the user's community
    
#     Returns JSON with the the copied policy's id.
#     """
#     user = get_user(request)
#     community = user.community.community

#     # Make a copy of the policy
#     data = json.loads(request.body)
#     policy_source = policy_from_request(request)
#     new_policy = policy_source.copy_to_community(community=community, variable_data=data["variables"])
#     new_policy.name = new_policy.name.replace('_starter', '_edited')
#     new_policy.save()

    # return JsonResponse({ "policy": new_policy.id })

# def embed_summary(request):
#     """
#     Shows the summary page with info about the edited policy.
#     """
#     policy = policy_from_request(request)

#     variables = policy.variables.all()

#     return render(request, "collectivevoice/summary.html", {
#         "policy": policy,
#         "all_variables": variables
#     })

# def embed_update(request):
#     """
#     Updates the PolicyVariable objects associated with
#     the policy being edited in the no-code flow.
#     """
#     policy = policy_from_request(request)

#     # Update policy variables
#     data = json.loads(request.body)
#     policy.update_variables(data["variables"])

#    return JsonResponse({ "policy": policy.id })

# def embed_edit(request):
#     """
#     Shows the page for editing PolicyVariable objects
#     associated with the policy being edited in the no-code flow.
#     """
#     policy = policy_from_request(request)
#     user = get_user(request)
#     community = user.community.community
#     variables = policy.variables.all()

#     channel_options = []
#     # only get channel names from Slack API if needed
#     if any(['channel' in x.name for x in variables]):
#         channel_options = get_channel_options(community.id)

#     return render(request, "collectivevoice/edit.html", {
#         "policy": policy,
#         "all_variables": variables,
#         "channel_options": channel_options
#     })

def collectivevoice_success(request):
    """Shows success page when no-code editing is finished."""
    policy = policy_from_request(request)
    return render(request, "collectivevoice/success.html", {
        "policy": policy
    })
