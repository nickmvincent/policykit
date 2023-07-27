# In procedures.json, we store some python code blocks
# (e.g., notify, check)
# as a string entries in a json object

# the purpose of this file is to duplicate that code in a .py file 
# so we can benefit from code linting and formatting

# if developers are regularly editing procedure template code in the json file,
# we might expect the rate of typos and bugs to be quite high


# we'll use inspect.getsource to print out each function's code
import inspect


# we'll define a bunch of null variables to suppress error messages (so we can catch misspelled variables, etc)
proposal = None
logger = None
PASSED = None
FAILED = None
PROPOSED = None
variables = {}
slack = None
action = None
import datetime


# However, this won't help us catch missing keys in variables!

def peer_approval__check():
    if not proposal.vote_post_id:
        return None

    yes_votes = proposal.get_yes_votes().count()
    no_votes = proposal.get_no_votes().count()
    proposal.data.set("yes_votes_num", yes_votes)
    proposal.data.set("no_votes_num", no_votes)
    logger.debug(f"{yes_votes} for, {no_votes} against")

    now = datetime.datetime.now().timestamp()
    reminder_delta = now - float(proposal.data.get('reminder_sent'))

    if datetime.timedelta(seconds=int(reminder_delta)) > datetime.timedelta(minutes=5):
        proposal.data.set('reminder_sent', now)

    already_voted = proposal.get_all_boolean_votes().values_list("user__username")
    already_voted_list = [voter[0] for voter in already_voted]
    nonvoter_list = [f"<@{uid}>" for uid in variables.users if uid not in already_voted_list]
    nonvoter_string = ", ".join(nonvoter_list)
    voter_msg = f"Eligible voters: {variables.users}; Already voted: {already_voted}; Nonvoter list: {nonvoter_list}"
    slack.post_message(text=voter_msg, channel=variables.vote_channel, thread_ts=proposal.vote_post_id)

    reminder_msg = f"Please vote on this proposal. {nonvoter_string} have not voted yet."
    slack.post_message(text=reminder_msg, channel=variables.vote_channel, thread_ts=proposal.vote_post_id)

    slack.post_message(text="Ran a check", channel=variables.vote_channel, thread_ts=proposal.vote_post_id)

    if yes_votes >= 1:
        return PASSED
    elif no_votes >= variables.num_no_votes_to_reject:
        return FAILED

    return PROPOSED


def majority_vote__check():
    if not proposal.vote_post_id:
        return None
    yes_votes = proposal.get_yes_votes().count()
    no_votes = proposal.get_no_votes().count()
    proposal.data.set('yes_votes_num', yes_votes)
    proposal.data.set('no_votes_num', no_votes)
    logger.debug(f'{yes_votes} for, {no_votes} against')
    if yes_votes >= len(variables.users) / 2:
      return PASSED
    elif no_votes >= len(variables.users) / 2:
      return FAILED
    
    return PROPOSED
    

def print_for_json(f):
    ret = inspect.getsource(f)
    ret = ret.replace('\n    ', '\\n')
    ret = ret.split("():")[1]
    print(ret)
    return ret

print_for_json(peer_approval__check)

# print_for_json(majority_vote__check)

