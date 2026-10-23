# Lambda handlers

Five Python files, deployed via **AWS SAM** (`template.yaml`).

| File | Trigger | Purpose |
|---|---|---|
| `whatsapp_webhook.py` | HTTP API (Meta → HTTPS) | Verifies Meta's signature, invokes harness, runs the tool-use loop, dispatches to the right tool Lambda |
| `whatsapp_send.py` | Inline function `send_whatsapp` | POSTs the agent's reply to Meta's Cloud API |
| `restaurant_read.py` | Gateway target `restaurantRead` (8 tools) | One Lambda serving all reads: `list_by_area`, `find_place`, `walking_distance`, `check_availability`, `recent_visits`, `visits_by_companion`, `get_list`, `search_google_places` (dispatched on the tool name) |
| `add_place.py` | Gateway target `addPlace` | Owner-only. Appends to `recs.json` or `want-to-go.json` in S3. |
| `log_visit.py` | Gateway target `logVisit` | Owner-only. Appends visit; may move place between lists. |

## Prerequisites

- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- AWS creds with rights to create Lambda + S3 + API Gateway + IAM
- The harness ARN for the second deploy (see the deploy order in the top-level README)
- A Google Places API key (Places API New + Distance Matrix enabled)
- Meta WhatsApp Cloud API credentials (system user token + phone-number ID)

## Deploy

Guided the first time: SAM prompts for each parameter and saves answers to `samconfig.toml` (gitignored):

```bash
cd lambda
sam build
sam deploy --guided
```

Subsequent deploys:

```bash
sam deploy
```

Parameters:

| Parameter | What to put |
|---|---|
| `HarnessArn` | Empty on the first deploy; then the harness ARN from `agentcore deploy` |
| `DataBucketName` | Globally unique, e.g. `my-restaurant-agent-data` |
| `OwnerWhatsAppNumber` | Your E.164 number, e.g. `+447700900000` |
| `GoogleMapsApiKey` | Google API key with Places API (New) + Distance Matrix |
| `MetaToken` | Meta system user token, not the 24-hour temporary token |
| `MetaPhoneId` | Sender phone-number ID from Meta (numeric string) |
| `MetaVerifyToken` | Any string you pick; you paste the same one into Meta's dashboard |
| `MetaAppSecret` | Meta app dashboard → App settings → Basic → App secret. Without it the webhook rejects every POST. |

## After the stack deploys

SAM prints outputs:

- **`WebhookUrl`**: paste into Meta's app dashboard webhook config
- **`DataBucket`**: upload the seed JSON:
  ```bash
  cd ../data
  aws s3 cp recs.json         s3://<bucket>/
  aws s3 cp want-to-go.json   s3://<bucket>/
  aws s3 cp visit-log.json    s3://<bucket>/
  aws s3 cp availability.json s3://<bucket>/
  ```

The webhook maps tool names (bare and Gateway-prefixed) to Lambda function names via `DEFAULT_TOOL_MAP` in `whatsapp_webhook.py`; override with a `TOOL_LAMBDA_MAP` env var if you rename the functions.

## The auth model

- The webhook verifies `X-Hub-Signature-256` on every POST using `META_APP_SECRET`, so the sender number in the payload can be trusted.
- Both write Lambdas (`add_place`, `log_visit`) check that `session.actor_id` matches `OWNER_WHATSAPP_NUMBER`. Even if the model triggers a write on a friend's session, the Lambda rejects it.
- The webhook forces `send_whatsapp`'s `to` to the original sender.
- Gateway invocations (IAM-authenticated) skip the owner check, since there's no WhatsApp sender to compare.

## The tool-use loop (in `whatsapp_webhook.py`)

Meta hits the webhook → we check the signature → we call `bedrock-agentcore:invoke_harness` and stream the response → if `stopReason == "tool_use"`, we invoke the mapped Lambda for each tool call, then call the harness again with the tool results. Repeat until `end_turn`, capped at `MAX_TOOL_TURNS = 10`.

`send_whatsapp` is itself a tool call: the harness fires it as the final action of most flows. If the model forgets but did write a reply, the webhook sends that text itself.

## Non-obvious bits

- **`boto3==1.43.40` pinned in `requirements.txt`.** The Lambda runtime's default boto3 doesn't know about `bedrock-agentcore` until 1.36+. Do not remove the pin.
- **`Architectures: [arm64]` is in Globals, not per-function.** Duplicating it produces `list has non-unique elements` at deploy time.
- **`samconfig.toml` is gitignored** because it contains real secrets (Meta token, Google API key, your phone number).

## Testing against the deployed stack

Both scripts run locally but call your real deployed harness and Lambdas:

```bash
# From the repo root:
python3 scripts/chat.py                        # REPL against deployed harness
python3 scripts/local_run.py "test message"    # invokes webhook handler locally
```

For pure Lambda-level testing (no harness), invoke a deployed tool directly:

```bash
aws lambda invoke --function-name agent-fc-restaurant-read \
  --payload '{"tool_name":"list_by_area","tool_input":{"name":"recs","area":"Shoreditch"}}' \
  --cli-binary-format raw-in-base64-out /dev/stdout
```

## Region

Pick the region during `sam deploy --guided` (I used `eu-west-2`, London). Bedrock model access and AgentCore need to be available there, and the harness must be deployed to the same region.
