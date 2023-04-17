from django.db import models
from policyengine.models import CommunityPlatform, CommunityUser, TriggerAction
import logging

from django.db import models
import integrations.discord.utils as DiscordUtils
from policyengine.models import (
    CommunityPlatform,
    CommunityUser,
)
from policyengine.metagov_app import metagov

logger = logging.getLogger(__name__)


class AutonomousAgent(CommunityUser):
    """
    A subclass of CommunityUser, but is entirely autonomous. Used for simulations
    """

    def __init__(self):
        """
        Agents are special because they have pre-programmed behavior

        #TODO: what is a list of all the stuff an agent can do?
        #TODO: what are reasonable starting points for how frequently each acgent should do each action
        """
        self.actions = {
            "initiate_vote": 0.1, # this agent has a 10% chance to start a new vote
            "vote_yes_in_existing_stuff": 0.6 # this agent votes yes 60% of the time
        } # can be, "vote randomly", "propose new poll"

    def perform_actions(self):
        """
        Do all the things that the agent is programmed to do
        """





# # Maybe / spitballing
# class Enviroment(object):
#     """
#     Holds a set of agents... controls speed etc
#     """

#     def __init__(self):
#         pass


class AutonomousCommunity(CommunityPlatform):
    platform = "autonomouspk"

    team_id = models.CharField("team_id", max_length=150, unique=True)


    def set_speed(self, new_speed):
        """
        When a person wants to speed up or slow down a simulation, this variable is modified
        """
        self.speed = new_speed

    def take_turn(self):
        """
        all agents in the community take a turn...

        Must write a policy that includes community.take_turn() to trigger this...

        one way: every time someone types "take turn" in slack, call this function once
        OR

        if I type "start simulation 50 turns", then 
        for _ in range(50): take_turn()
        """

        # this is pseudo code it doesn't work yet
        for agent in all agents:
            agent.take_actions()



    def initiate_vote(self, proposal, users=None, post_type="channel", text=None, channel=None, options=None):
        # construct args

        # 

        args = DiscordUtils.construct_vote_params(proposal, users, post_type, text, channel, options)
        logger.debug(args)
        # get plugin instance
        plugin = metagov.get_community(self.community.metagov_slug).get_plugin("discord", self.team_id)
        # start process
        process = plugin.start_process("vote", **args)
        # save reference to process on the proposal, so we can link up the signals later
        proposal.governance_process = process
        proposal.vote_post_id = process.outcome["message_id"]
        logger.debug(f"Saving proposal with vote_post_id '{proposal.vote_post_id}'")
        proposal.save()

    def post_message(self, proposal, text, channel=None, message_id=None):
        """
        Post a message in a Discord channel.
        """
        channel = channel or DiscordUtils.infer_channel(proposal)
        if not channel:
            raise Exception("Failed to determine which channel to post in")
        optional_args = {}
        if message_id:
            optional_args["message_reference"] = {
                "message_id": message_id,
                "guild_id": self.team_id,
                "fail_if_not_exists": False,
            }
        return self.metagov_plugin.post_message(text=text, channel=int(channel), **optional_args)

    def _update_or_create_user(self, user_data):
        """
        Helper for creating/updating DiscordUsers. The 'username' field must be unique for Django,
        so it is a string concatenation of the user id and the guild id.

        user_data is a User object https://discord.com/developers/docs/resources/user#user-object

        https://discord.com/developers/docs/resources/guild#guild-member-object
        """
        user_id = user_data["id"]
        unique_username = f"{user_id}:{self.team_id}"
        user_fields = DiscordUtils.get_discord_user_fields(user_data)
        defaults = {k: v for k, v in user_fields.items() if v is not None}
        return DiscordUser.objects.update_or_create(username=unique_username, community=self, defaults=defaults)

    def _get_or_create_user(self, user_id):
        unique_username = f"{user_id}:{self.team_id}"
        return DiscordUser.objects.get_or_create(username=unique_username, community=self)

