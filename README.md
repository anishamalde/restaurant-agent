# Restaurant Agent: a personal AI agent on WhatsApp, built on Amazon Bedrock AgentCore

A restaurant-recommendation agent that lives in WhatsApp. You text it something like *"in Shoreditch tonight for 2, veggie friendly"* and it searches your saved restaurant lists, checks walking distance with Google Maps, checks (mocked) availability, and replies like a friend would. You can also keep your lists up to date just by texting it: *"went to Gloria last night, loved it"* or *"add Mambow in Clapton to want-to-go"*.

I built it as the demo for a talk on **harness engineering**: what it actually takes to get one agent doing a real, personal task in production. It's a single agent with real data, a real side effect (it sends messages) and a real deployment. It's also an honest record of the rough edges I hit along the way.

> **Heads up:** this is a reference project, not a product. It's single-user, London-centric, and the reservation availability is fake (consumer booking APIs like OpenTable and Resy aren't publicly available). The seed data in `data/` is my own Google Maps saved list, so swap in yours.

## Why this is interesting

With an **AgentCore harness**, you don't run the agent loop yourself. You hand AWS a config file (`harness.json`) that declares the model, system prompt, tools and memory, and AWS runs the loop. There's no Strands process, Python server or model hosting on your side.

That's the theory. In practice, the bit worth learning from is the tool dispatch:

- **10 tools sit behind an AgentCore Gateway** (managed MCP over Lambda). In the AWS Console playground, Gateway calls the Lambdas server-side and everything just works.
- **The WhatsApp path calls `invoke_harness` directly.** That lower-level API *always* hands `tool_use` pauses back to the caller, even for Gateway-backed tools. So the webhook Lambda still runs its own tool-use loop, routing Gateway-prefixed names (`restaurantRead___list_by_area`) to the same Lambdas.

Same harness, same tools, two different dispatch behaviours depending on which API you call. "Server-side tool dispatch" turned out to be a property of the entry point, not the config. If you're building on AgentCore, that's the gotcha worth knowing about up front.

## What it can do

| You text | What happens |
|---|---|
| *"anywhere good in Soho tonight for 3?"* | Searches your lists by area, checks walking distance and availability, replies with a top pick plus a backup |
| *"add Akub in Notting Hill to want-to-go"* | Looks it up on Google Places and saves it to your wishlist |
| *"went to Gloria last night with Priya, loved it"* | Logs the visit, and moves the place from want-to-go to recs if it was on the wishlist |
| *"where did I go with Priya last month?"* | Queries the visit log |
| *"remember Ravi is vegetarian"* | Saved to AgentCore memory for future recs |

Friends can ask for recommendations too, but only the owner's number can change the lists. See [USAGE.md](USAGE.md) for the full vocabulary and example conversations.

## Architecture

```
  WhatsApp path                               Playground path
  Meta Cloud API -> webhook Lambda            AWS Console playground
        |  (verifies Meta signature)                 |
        |  invoke_harness                            |  higher-level API,
        |  (returns tool_use pauses)                 |  auto-dispatches
        v                                            v
  +--------------------------------------------------------------+
  |  AgentCore Harness  (Claude Sonnet 4.6 via Bedrock)          |
  |  - agentcore_gateway tool  -> 10 tools on a Gateway          |
  |  - inline send_whatsapp    -> handled by the webhook only    |
  |  - code interpreter                                          |
  |  - memory (semantic + user preferences, keyed by phone no.)  |
  +--------------------------------------------------------------+
        |  webhook dispatches                        |  Gateway dispatches
        v                                            v
  +--------------------+----------------+------------------+
  | restaurant-read    | add-place      | log-visit        |   + whatsapp-send
  | (8 read tools)     | (owner only)   | (owner only)     |     (Meta Graph API)
  +--------------------+----------------+------------------+
        |                     |                |
        v                     v                v
  S3: recs.json, want-to-go.json, visit-log.json, availability.json
  + Google Places API (New) and Distance Matrix
```

### The tools

Gateway exposes each tool as `<target>___<tool>` (three underscores).

| Where | Target | Tool | What it does |
|---|---|---|---|
| Gateway | `restaurantRead` | `list_by_area` | Filter a saved list by neighbourhood |
| Gateway | `restaurantRead` | `find_place` | Fuzzy-match a place name across both lists |
| Gateway | `restaurantRead` | `walking_distance` | Google Distance Matrix, walking mode |
| Gateway | `restaurantRead` | `check_availability` | **Mocked** reservation slots from `availability.json` |
| Gateway | `restaurantRead` | `recent_visits` | Visits in the last N days |
| Gateway | `restaurantRead` | `visits_by_companion` | Visits with a named person |
| Gateway | `restaurantRead` | `get_list` | Dump a whole list |
| Gateway | `restaurantRead` | `search_google_places` | Google Places (New) text search |
| Gateway | `addPlace` | `add_place` | Append to `recs.json` or `want-to-go.json` |
| Gateway | `logVisit` | `log_visit` | Append to `visit-log.json`, may promote a place to recs |
| Inline | | `send_whatsapp` | Send the reply via Meta's Cloud API |
| Built-in | | `compute` | AgentCore code interpreter |

`send_whatsapp` is deliberately *not* on the Gateway. Sending a message is a real-world side effect, so the webhook owns it rather than letting it be auto-dispatched.

## Repo layout

```
├── harness.json            AgentCore harness config: model, prompt, tools, memory
├── USAGE.md                What you can say to the agent, with examples
├── .env.example            Template for the local scripts' config
├── data/                   Seed JSON, uploaded to S3
├── lambda/                 Five Lambda handlers + SAM template (Gateway, S3, HTTP API)
├── restaurantAgent/        AgentCore CLI project (scaffolded by `agentcore create`);
│                           app/restaurantAgent/harness.json symlinks to ../../../harness.json
└── scripts/
    ├── chat.py             Terminal REPL against the deployed harness (WhatsApp send intercepted)
    ├── local_run.py        Run the webhook handler locally against deployed Lambdas
    └── tools/              One-off scripts that turned a Google Maps list into the seed data
```

## Deploying it yourself

### What you'll need

- An AWS account with Amazon Bedrock model access for Claude Sonnet 4.6 and AgentCore available in your region (I used `eu-west-2`)
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) and the [AgentCore CLI](https://github.com/aws/agentcore-cli) (Node.js 20+)
- Python 3.12
- A Google Cloud API key with **Places API (New)** and **Distance Matrix API** enabled
- A [Meta developer app](https://developers.facebook.com/docs/whatsapp/cloud-api/get-started) with the WhatsApp product added

### Steps

Order matters: the harness references the Gateway ARN, and the webhook references the harness ARN.

```bash
# 1. Deploy the Lambdas, S3 bucket, HTTP API and Gateway.
#    Leave HarnessArn empty for now. Set MetaAppSecret to your Meta app secret.
cd lambda
sam build && sam deploy --guided
# Note GatewayArn, DataBucket and WebhookUrl from the outputs.

# 2. Point harness.json at your Gateway: replace the placeholder `gatewayArn` with the
#    GatewayArn output. (It contains your account ID, so think twice before committing it.)
#    Skip this and step 3 fails with "GatewayArn ... does not match pattern".

# 3. Deploy the harness and its memory (npm AgentCore CLI, not the older Python starter toolkit).
cd ../restaurantAgent
npx @aws/agentcore deploy
# Note the harness ARN.

# 4. Redeploy the stack with the harness ARN so the webhook can call it.
#    Guided mode remembers your other answers; just fill in HarnessArn.
cd ../lambda
sam deploy --guided

# 5. Seed your data (edit data/*.json first if you want your own lists).
cd ..
for f in recs want-to-go visit-log availability; do
  aws s3 cp data/$f.json s3://<DataBucket>/
done
```

Then wire up WhatsApp in the Meta dashboard:

1. **Webhook**: set the callback URL to `WebhookUrl`, use the same verify token you passed as `MetaVerifyToken`, and subscribe to `messages`.
2. **Token**: generate a long-lived **system user token** (Business Settings → System Users) with `whatsapp_business_messaging` and `whatsapp_business_management`. The 24-hour token from the API Setup page will quietly expire on you.
3. **Recipients**: while your app is in development mode, add your phone number to the allowed recipients list, or Meta returns `(#131030) Recipient phone number not in allowed list`.
4. **Subscribe the app to your WhatsApp Business Account.** This one is easy to miss. Without it, Meta shows inbound messages in its test panel but never forwards them to your webhook:
   ```bash
   curl -X POST "https://graph.facebook.com/v21.0/<WABA_ID>/subscribed_apps" \
     -H "Authorization: Bearer $SYSTEM_USER_TOKEN"
   ```

### Trying it without WhatsApp

```bash
cp .env.example .env    # fill in your deployed values (local_run.py also needs META_APP_SECRET)

python3 scripts/chat.py                                   # interactive REPL
python3 scripts/chat.py "in Shoreditch tonight for 2" --verbose
python3 scripts/local_run.py "went to Gloria last night" --dry-run
```

Both scripts talk to your real deployed harness and Lambdas; they just intercept the WhatsApp send. Note that `chat.py` and `local_run.py` can write to your lists if you set the sender to the owner's number.

## Security model

- **Inbound webhooks are signature-checked.** Every POST must carry a valid `X-Hub-Signature-256` (HMAC-SHA256 of the body using your Meta app secret). If `MetaAppSecret` isn't set, the webhook rejects everything.
- **Writes are owner-only.** On the WhatsApp path, `add_place` and `log_visit` check the sender's number against `OWNER_WHATSAPP_NUMBER` in the Lambda itself. The system prompt is not the security boundary.
- **Replies only go back to the sender.** The webhook overrides the `to` field on every `send_whatsapp` call, so a prompt-injected "message this other number" goes nowhere.
- **The Gateway uses IAM auth.** Only principals you grant `bedrock-agentcore` invoke rights can reach the tools. Calls through the Gateway (for example, from the playground) skip the owner check, because there's no WhatsApp sender to check against. Keep that in mind before granting access to anyone else.
- **Secrets stay out of git.** They live in `samconfig.toml` and `.env`, which are both gitignored. The S3 bucket blocks all public access.

## Things I learned the hard way

- **`invoke_harness` never auto-dispatches tools**, whatever type they are. Only the playground's higher-level API does. Plan for a client-side tool loop if you call the harness directly.
- **Gateway changes the Lambda event shape.** Arguments arrive flat in `event`, and the tool name is in `context.client_context.custom["bedrockAgentCoreToolName"]`. The Lambdas here handle both shapes.
- **CloudFormation's `GatewayTarget` `InputSchema` isn't plain JSON Schema.** Keys are capitalised (`Type`, `Properties`, `Required`), and `enum`, `minimum` and `maximum` aren't supported, so allowed values go in the descriptions instead.
- **The playground can't complete inline function tools.** That's what forced the move to Gateway in the first place.
- **The default Lambda boto3 is too old for `bedrock-agentcore`.** It's pinned in `lambda/requirements.txt`; don't remove the pin.
- **Keep `harness.json` to fields the AgentCore CLI knows about.** Extras like `_comment` get silently dropped. `agentcore add` is the safest way to change tools.
- **Memory actor IDs can't start with `+`.** E.164 numbers fail AgentCore memory's actor ID pattern, so the webhook passes the harness bare digits. Nothing complains until memory is switched on.
- **There are two tools called `agentcore`.** The older Python starter toolkit (`pip install bedrock-agentcore-starter-toolkit`) and the current npm CLI (`@aws/agentcore`) have different commands and flags. This project uses the npm one.
- **`agentcore dev` runs in AWS, not on your laptop**, so a local MCP server on `127.0.0.1` isn't reachable from it (look for `Errno 111`).

## Limitations

- Availability is mocked. Update the dates in `data/availability.json` before you demo it.
- The Google Places search and postcode mapping assume London.
- It's single-owner: one phone number can write.
- Message types other than text (images, voice notes, locations) are ignored.

Questions, ideas, or a better way to handle the dispatch split? Open an issue. I'd love to hear how others are approaching it.
