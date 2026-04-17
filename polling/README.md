poll_from_codex로 1건 가져오고, 받은 id를 reply(reply_to=그 id)로 응답해줘.

poll_from_codex로 1건 가져오고, 건수가 없으면 무시하고, 있으면 받은 id를 reply(reply_to=그 id)로 응답해줘.

Use poll_from_codex (limit=1).
If none, do nothing.
If one exists, reply with reply(reply_to=<id>).
