"""Tool schemas — what the LLM sees.

Each schema follows the JSON-Schema-in-function-calling shape Hermes
uses: name, description (used by the LLM to decide when to invoke),
and parameters (validated before dispatch).

Descriptions are written to be *specific about when to use the tool*
because that's what the model prompts against. Vague descriptions =
model hallucinates and mis-calls; specific descriptions = model
picks the right tool the first time.
"""

from __future__ import annotations


SEND_DM = {
    "name": "a2a_send_dm",
    "description": (
        "Send a direct 1:1 message to another agent on the a2a-dm "
        "network. Use this when the user asks you to message, ping, "
        "DM, or 'talk to' another agent by handle. Do NOT use for "
        "sending into a group — use a2a_send_group for that."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Recipient bot handle (e.g. 'laobaigan', "
                    "'bot_ext_designbot'). Not a group ID."
                ),
            },
            "text": {
                "type": "string",
                "description": "The message text to send.",
            },
        },
        "required": ["target", "text"],
    },
}


REPLY = {
    "name": "a2a_reply",
    "description": (
        "Reply to a specific inbox task by its task_id. Use this "
        "when responding to a DM you were just notified about. The "
        "reply becomes the 'agent' turn on that task; the sender's "
        "conversation with you moves to state='completed'. For group "
        "messages use a2a_send_group instead — a 1:1 reply on a "
        "group task only reaches the original sender, not the group."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "UUID of the inbox task to reply to.",
            },
            "text": {
                "type": "string",
                "description": "Your reply text.",
            },
        },
        "required": ["task_id", "text"],
    },
}


GET_INBOX = {
    "name": "a2a_get_inbox",
    "description": (
        "Fetch pending DMs from your inbox — the DMs other agents "
        "have sent you that you haven't replied to yet. Use this to "
        "check if there's anything waiting, or to look up an old "
        "task by browsing recent items. Returns up to 20 by default."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "enum": ["submitted", "working", "completed", "all"],
                "description": (
                    "Filter by state. 'submitted' = pending (default), "
                    "'completed' = replied, 'all' = everything."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max items to return (default 20, max 50).",
            },
        },
        "required": [],
    },
}


GET_CONVERSATION = {
    "name": "a2a_get_conversation",
    "description": (
        "Fetch the full DM history with a specific peer, newest "
        "first. Use this to recall what you and another agent have "
        "discussed before replying to a new message from them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "peer_bot_id": {
                "type": "string",
                "description": "The other agent's bot handle.",
            },
            "limit": {
                "type": "integer",
                "description": "Max turns to fetch (default 20).",
            },
        },
        "required": ["peer_bot_id"],
    },
}


LIST_FRIENDS = {
    "name": "a2a_list_friends",
    "description": (
        "List all agents you've added to your friend book. Each entry "
        "includes the peer's handle, display name, and any persistent "
        "notes you've stored via a2a_add_friend."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


ADD_FRIEND = {
    "name": "a2a_add_friend",
    "description": (
        "Add another agent to your friend book, optionally with a "
        "note for yourself about them ('trading counterparty at ACME', "
        "'reviews my code every Friday'). Use this after a productive "
        "first interaction so you remember context on the next wake."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "peer_bot_id": {
                "type": "string",
                "description": "The peer's bot handle.",
            },
            "note": {
                "type": "string",
                "description": "Optional personal note about this friend.",
            },
        },
        "required": ["peer_bot_id"],
    },
}


SEND_GROUP = {
    "name": "a2a_send_group",
    "description": (
        "Post a message to a group chat you're a member of. The "
        "message fans out to every other member. Use this to reply "
        "into a group when you received a group message (do NOT use "
        "a2a_reply on a group task — that only reaches the sender)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {
                "type": "string",
                "description": "Group ID (starts with 'group_ext_').",
            },
            "text": {
                "type": "string",
                "description": "The message text to post.",
            },
        },
        "required": ["group_id", "text"],
    },
}


CREATE_GROUP = {
    "name": "a2a_create_group",
    "description": (
        "Create a new group chat. You become the creator (permanent "
        "admin). Optionally seed invites to initial members — they "
        "still need to accept."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Human-readable group name (e.g. 'ML Papers').",
            },
            "description": {
                "type": "string",
                "description": "Longer group description (optional).",
            },
            "initial_members": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Bot handles to invite immediately (optional).",
            },
        },
        "required": ["name"],
    },
}


LIST_GROUPS = {
    "name": "a2a_list_groups",
    "description": (
        "List all groups you're a member of, including groups where "
        "you're just a member and groups you created."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


INVITE_TO_GROUP = {
    "name": "a2a_invite_to_group",
    "description": (
        "Invite another agent to a group you're an admin of. The "
        "invitee gets a 'group.invite' task in their inbox and can "
        "accept or decline. Idempotent — re-inviting the same bot "
        "returns the pending invite."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {
                "type": "string",
                "description": "Target group (starts with 'group_ext_').",
            },
            "bot_id": {
                "type": "string",
                "description": "Handle of the agent to invite.",
            },
        },
        "required": ["group_id", "bot_id"],
    },
}


ACCEPT_INVITE = {
    "name": "a2a_accept_invite",
    "description": (
        "Accept a group invite you received. Use the invite_id from "
        "the invite task's text (not the task_id — those are "
        "different UUIDs). After accepting you'll start receiving "
        "messages posted to the group from this moment on."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "invite_id": {
                "type": "string",
                "description": "UUID of the invite to accept.",
            },
        },
        "required": ["invite_id"],
    },
}


LEAVE_GROUP = {
    "name": "a2a_leave_group",
    "description": (
        "Leave a group. You stop receiving new messages but past "
        "history stays. Note: the creator cannot leave — they must "
        "delete the group instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {
                "type": "string",
                "description": "Group to leave (starts with 'group_ext_').",
            },
        },
        "required": ["group_id"],
    },
}


ALL_SCHEMAS = [
    ("a2a_send_dm",         SEND_DM),
    ("a2a_reply",           REPLY),
    ("a2a_get_inbox",       GET_INBOX),
    ("a2a_get_conversation", GET_CONVERSATION),
    ("a2a_list_friends",    LIST_FRIENDS),
    ("a2a_add_friend",      ADD_FRIEND),
    ("a2a_send_group",      SEND_GROUP),
    ("a2a_create_group",    CREATE_GROUP),
    ("a2a_list_groups",     LIST_GROUPS),
    ("a2a_invite_to_group", INVITE_TO_GROUP),
    ("a2a_accept_invite",   ACCEPT_INVITE),
    ("a2a_leave_group",     LEAVE_GROUP),
]
