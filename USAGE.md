# Using the agent

Once it's deployed, you just WhatsApp it. There are no commands to learn: the system prompt in `harness.json` sorts every message into one of six intents and runs the matching flow. This page covers what each intent looks like in practice, and what happens behind the scenes.

## What a single message actually does

When you text *"in Shoreditch tonight for 2, veggie friendly"*, this all happens in roughly 15 to 20 seconds:

1. Meta POSTs the message to the webhook Lambda, which checks Meta's signature
2. The webhook calls `invoke_harness`, and the model classifies the message as `rec_request`
3. The harness pauses with a tool call: `list_by_area(name='recs', area='Shoreditch')`
4. The webhook invokes the `restaurant-read` Lambda and gets back, say, Gloria, Dishoom and Smoking Goat
5. The webhook calls the harness again with the result
6. More pauses follow: `walking_distance` for each candidate (Google Distance Matrix), then `check_availability` for the best few (mocked JSON)
7. The model writes the reply and calls `send_whatsapp`
8. The webhook invokes `whatsapp-send`, which posts it to your phone via the Meta Graph API
9. The harness returns `stopReason: end_turn` and the loop exits

Every hop is a separate Lambda invocation, so each tool call shows up on its own in CloudWatch. The loop is capped at 10 turns.

## The six intents

### `rec_request`: "where should I eat?"

> **You:** anywhere good in Soho tonight for 3?
>
> **Agent:** Berenjak's your best bet, Persian, 6 min walk, they've got 18:30 for 4. Backup: Bao, also on your recs.

The agent searches your recs first, falls back to your want-to-go list, drops anything more than a 20-minute walk away, and never claims a table is free unless `check_availability` said so.

### `add_place`: saving somewhere new

> **You:** add Mambow in Clapton to my want-to-go list
>
> **Agent:** Added Mambow, Clapton to want-to-go.

It defaults to want-to-go unless you say you've already been.

### `log_visit`: telling it where you went

> **You:** went to Gloria last night with Priya, loved it
>
> **Agent:** Logged Gloria for last night with Priya. Kept on your recs.

If the place was on your want-to-go list and you liked it, it moves to recs automatically:

> **You:** finally did Akub yesterday, was excellent
>
> **Agent:** Logged Akub for yesterday. Moved it from want-to-go to recs.

A "skip" verdict never removes anything on its own. The agent asks first.

### `list_query`: asking about your lists

> **You:** where did I go with Priya last month?
>
> **Agent:** With Priya in the last 30 days: Gloria (28 Jun) and Dishoom Shoreditch (12 Jun).

### `preference_update`: things to remember

> **You:** remember Ravi is vegetarian now
>
> **Agent:** Noted, I'll keep Ravi's recs veggie-friendly.

These are stored in AgentCore memory (semantic and user-preference strategies), keyed by the sender's phone number.

### `unknown`

If it can't tell what you mean, it asks a clarifying question and stops.

## Owners and friends

Anyone who can message the WhatsApp number can ask for recommendations and query the lists. Only the number set as `OWNER_WHATSAPP_NUMBER` can change them.

That check lives in the `add_place` and `log_visit` Lambdas, not in the prompt. If a friend asks the agent to add a place, the model may well try, but the Lambda refuses and the agent tells them it couldn't. The rule of thumb: never rely on the model alone to protect state changes.

## What it won't do

- **Book a table.** There's no booking tool, and the availability data is mocked anyway.
- **Invent restaurants.** If nothing on your lists matches, it says so and offers to add somewhere new.
- **Delete or change a verdict without asking.**
- **Message anyone other than the sender.** The webhook pins every reply to whoever sent the message.
