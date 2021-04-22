from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.conf import settings
from django.contrib.auth import login, authenticate
from django.views.decorators.csrf import csrf_exempt
from policykit.settings import SERVER_URL, DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_BOT_TOKEN
from policyengine.models import *
from integrations.discord.models import DiscordCommunity, DiscordUser, DiscordPostMessage, DiscordStarterKit
from urllib import parse
import urllib.request
import json
import base64
import logging
import websocket
import threading
import datetime
import time

logger = logging.getLogger(__name__)

websocket.enableTrace(True)

# Used for Boolean voting
EMOJI_LIKE = '%F0%9F%91%8D'
EMOJI_DISLIKE = '%F0%9F%91%8E'

GATEWAY_VERSION = 8

session_id = None
heartbeat_interval = None
ack_received = True
sequence_number = None

def get_gateway_uri():
    req = urllib.request.Request('https://discordapp.com/api/gateway')
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", "Mozilla/5.0") # yes, this is strange. discord requires it when using urllib for some weird reason
    resp = urllib.request.urlopen(req)
    res = json.loads(resp.read().decode('utf-8'))
    return res['url']

def on_open(wsapp):
    def run(*args):
        global heartbeat_interval, ack_received, sequence_number
        while True:
            if heartbeat_interval:
                time.sleep(heartbeat_interval / 1000)

                # Verify that client received heartbeat ack between attempts at sending heartbeats
                #if ack_received == False:
                #    wsapp.close(status=1002)

                payload = json.dumps({
                    'op': 1,
                    'd': sequence_number
                })
                wsapp.send(payload)

    rt = threading.Thread(target=run)
    rt.daemon = True
    rt.start()

def should_create_action(community, call_type, message):
    # If message already has an object, don't create a new object for it.
    if DiscordPostMessage.objects.filter(guild_id=message['guild_id'], channel_id=message['channel_id'], message_id=message['id']):
        return False

    created_at = message['timestamp'] # ISO8601 timestamp
    created_at = datetime.datetime.fromisoformat(created_at)

    now = datetime.datetime.now()
    now = now.replace(tzinfo=datetime.timezone.utc) # Makes the datetime object timezone-aware

    # If message is more than twice the Celery beat frequency seconds old,
    # don't create an object for it. This way, we only create objects for
    # messages created after PolicyKit has been installed to the community.
    recent_time = 2 * settings.CELERY_BEAT_FREQUENCY
    if now - created_at > datetime.timedelta(seconds=recent_time):
        return False

    return True

def handle_ready_event(data):
    global session_id
    session_id = data['session_id']

def handle_message_create_event(data):
    logger.info(f'[discord] Received new message in channel {data["channel_id"]}')
    community = DiscordCommunity.objects.filter(team_id=data['guild_id'])[0]
    call_type = ('channels/%s/messages' % data['channel_id'])

    if should_create_action(community, call_type, data):
        action = DiscordPostMessage()
        action.community = community
        action.text = data['content']
        action.guild_id = data['guild_id']
        action.channel_id = data['channel_id']
        action.message_id = data['message_id']
        u,_ = DiscordUser.objects.get_or_create(username=data['author']['id'],
                                                community=community)
        action.initiator = u

        return action

def handle_event(name, data):
    if name == 'READY':
        handle_ready_event(data)
    else:
        action = None

        if name == 'MESSAGE_CREATE':
            action = handle_message_create_event(data)

        if action is not None:
            action.community_origin = True
            action.is_bundled = False
            action.save() # save triggers policy evaluation

        if name == 'MESSAGE_REACTION_ADD':
            action_res = PlatformAction.objects.filter(community_post=data['message_id'])
            if action_res.exists():
                if reaction in [EMOJI_LIKE, EMOJI_DISLIKE]:
                    val = (reaction == EMOJI_LIKE)

                    user,_ = DiscordUser.objects.get_or_create(username=data['user_id'],
                                                               community=action.community)
                    vote = BooleanVote.objects.filter(proposal=action.proposal, user=user)

                    if vote.exists():
                        vote = vote[0]
                        vote.boolean_value = value
                        vote.save()
                    else:
                        vote = BooleanVote.objects.create(proposal=action.proposal,
                                                          user=user,
                                                          boolean_value=val)

def on_message(wsapp, message):
    global heartbeat_interval, sequence_number, ack_received
    payload = json.loads(message)
    op = payload['op']
    if op == 0: # Opcode 0 Dispatch
        logger.info(f'[discord] Received event named {payload["t"]}')
        sequence_number = payload['s']
        handle_event(payload['t'], payload['d'])
    elif op == 10: # Opcode 10 Hello
        # Receive heartbeat interval
        heartbeat_interval = payload['d']['heartbeat_interval']
        logger.info(f'[discord] Received heartbeat of {heartbeat_interval} ms from the Discord gateway')

        # Send an Opcode 2 Identify
        payload = json.dumps({
            'op': 2,
            'd': {
                'token': DISCORD_BOT_TOKEN,
                'intents': 1543,
                'properties': {
                    '$os': 'linux', # TODO: Replace with system operating system
                    '$browser': 'disco',
                    '$device': 'disco'
                },
                'compress': False
            }
        })
        wsapp.send(payload)
        logger.info('[discord] Sent an Opcode 2 Identify to the Discord gateway')
    elif op == 11: # Opcode 11 Heartbeat ACK
        ack_received = True

def on_error(wsapp, error):
    logger.error(error)

def on_close(wsapp, code, reason):
    logger.error(f'[discord] Connection to Discord gateway closed with error code {code}')

# Open gateway connection
def connect_gateway():
    wsapp = websocket.WebSocketApp(f'{get_gateway_uri()}?v={GATEWAY_VERSION}&encoding=json',
        on_message=on_message,
        on_error=on_error,
        on_close=on_close)
    wsapp.on_open = on_open
    wst = threading.Thread(target=wsapp.run_forever)
    wst.daemon = True
    wst.start()

connect_gateway()

def oauth(request):
    logger.info(request)

    state = request.GET.get('state')
    code = request.GET.get('code')
    guild_id = request.GET.get('guild_id')
    error = request.GET.get('error')

    if error == 'access_denied':
        return redirect('/login?error=sign_in_failed')

    data = parse.urlencode({
        'client_id': DISCORD_CLIENT_ID,
        'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': SERVER_URL + '/discord/oauth'
    }).encode()

    req = urllib.request.Request('https://discordapp.com/api/oauth2/token', data=data)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", "Mozilla/5.0") # yes, this is strange. discord requires it when using urllib for some weird reason
    resp = urllib.request.urlopen(req)
    res = json.loads(resp.read().decode('utf-8'))

    if state == 'policykit_discord_user_login':
        user = authenticate(request, oauth=res, platform='discord')
        if user:
            login(request, user)
            return redirect('/main')
        else:
            return redirect('/login?error=invalid_login')

    elif state == 'policykit_discord_mod_install':
        req = urllib.request.Request('https://discordapp.com/api/guilds/%s' % guild_id)
        req.add_header("Content-Type", "application/json")
        req.add_header('Authorization', 'Bot %s' % DISCORD_BOT_TOKEN)
        req.add_header("User-Agent", "Mozilla/5.0") # yes, this is strange. discord requires it when using urllib for some weird reason
        resp = urllib.request.urlopen(req)
        guild_info = json.loads(resp.read().decode('utf-8'))

        s = DiscordCommunity.objects.filter(team_id=guild_id)
        community = None
        user_group,_ = CommunityRole.objects.get_or_create(role_name="Base User", name="Discord: " + guild_info['name'] + ": Base User")

        if not s.exists():
            community = DiscordCommunity.objects.create(
                community_name=guild_info['name'],
                team_id=guild_id,
                access_token=res['access_token'],
                refresh_token=res['refresh_token'],
                base_role=user_group
            )
            user_group.community = community
            user_group.save()

            # Get the list of users and create a DiscordUser object for each user
            req = urllib.request.Request('https://discordapp.com/api/guilds/%s/members?limit=1000' % guild_id) # NOTE: Can only have up to 1000 members per server!
            req.add_header("Content-Type", "application/json")
            req.add_header('Authorization', 'Bot %s' % DISCORD_BOT_TOKEN)
            req.add_header("User-Agent", "Mozilla/5.0") # yes, this is strange. discord requires it when using urllib for some weird reason
            resp = urllib.request.urlopen(req)
            guild_members = json.loads(resp.read().decode('utf-8'))
            owner_id = res['guild']['owner_id']
            for member in guild_members:
                user, _ = DiscordUser.objects.get_or_create(
                    username=member['user']['id'],
                    readable_name=member['user']['username'],
                    avatar = member['user']['avatar'],
                    community=community,
                    is_community_admin=(member['user']['id'] == owner_id)
                )
                user.save()
        else:
            community = s[0]
            community.community_name = guild_info['name']
            community.team_id = guild_id
            community.access_token = res['access_token']
            community.refresh_token = res['refresh_token']
            community.save()

            return redirect('/login?success=true')

        context = {
            "starterkits": [kit.name for kit in DiscordStarterKit.objects.all()],
            "community_name": community.community_name,
            "platform": "discord"
        }
        return render(request, "policyadmin/init_starterkit.html", context)

    return redirect('/login?error=no_owned_guilds_found')

@csrf_exempt
def action(request):
    json_data = json.loads(request.body)
    logger.info('RECEIVED ACTION')
    logger.info(json_data)

def post_policy(policy, action, users=None, template=None, channel=None):
    from policyengine.models import LogAPICall

    policy_message = "This action is governed by the following policy: " + policy.name
    if template:
        policy_message = template

    data = {
        'content': policy_message
    }

    call = ('channels/%s/messages' % channel)

    res = policy.community.make_call(call, values=data)
    data['id'] = res['id']
    _ = LogAPICall.objects.create(community=policy.community,
                                  call_type=call,
                                  extra_info=json.dumps(data))

    if action.action_type == "PlatformAction":
        action.community_post = res['id']
        action.save()
