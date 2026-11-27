When an AI integration calls a tool that requires confirmation, the tool is not
executed. Instead:

1. An AI tool request is created. It freezes the call: the arguments, the
   record and the tool (function, model and kind). It also keeps the
   requester, the approver, the origin and the expiration date.
2. The caller (the LLM, the MCP client) receives a
   `{"status": "pending_confirmation", ...}` result instead of the tool
   output, so it can tell the user.
3. The approver gets a *Confirm AI Action* activity. From the request they
   can **Approve** it, and the tool runs with their access rights, or
   **Reject** it. Either way, the answer is logged on the chatter of the
   record of the request.

**Who approves.** Whoever executes the tool: the user it runs as. When no person
is behind (code running it as OdooBot: scheduled actions, job queues, webhooks),
the responsible user of the record (its `user_id` field), or else the fallback
approver.

**Where the requests are.** In *Settings > Technical > AI > AI Tool Requests*.
A scheduled action marks the requests past their expiration date as expired.

**For integrations.** Through the context of the tool call they can set:

- `ai_tool_confirm_expiration_hours`: the expiration of this request, instead
  of the instance default.
- `ai_tool_origin`: a label for the integration that made the call.

**Security notes.**

- The approver is always decided by the server, from the identity the tool
  runs with and the configuration: never by the AI (the arguments of the call)
  nor by the caller (the context). Otherwise a prompt injection could send the
  request to whoever suits it, and the action would run with their rights.
  Integrations acting on behalf of someone must execute the tool as that user.
- Only internal users can request AI actions: portal and public users cannot.
- The tool runs with the access rights of the approver, never as superuser.
- Requests can only be answered by their approver, from an Odoo web session:
  not through XML-RPC/JSON-RPC (API keys, scripts, agents holding the user's
  credentials) and never from inside an AI tool, so an AI cannot approve its
  own actions.
- Nobody can edit a request once made. If a module update changes what the tool
  executes, the request fails instead of running something else.
  Administrators can only change the *Human Confirmation* of a tool, not what
  the tool executes.
- The record is not frozen: it may change between the request and its
  approval, and the arguments were proposed for its state at request time.
  Check the record before approving a request that has waited for a while.
- Integrations must execute tools through `_execute_tool` (as `ai_connection`
  and MCP servers do), and must never let an LLM call methods of its choice:
  both would skip the confirmation.
