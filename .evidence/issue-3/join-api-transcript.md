# issue #3 — /join/api welcome-room flow transcript (evidence rig: repo landing nginx + repo approver.py @ ready-3 (PR for issue #3) + local continuwuity)

```
$ POST /join/api {"code":"dev-welcome-7447bc9a9531"}   # first call
{"room_alias": "#welcome-019e8c67:localhost:46167"}
$ POST /join/api (same code, second call)
{"room_alias": "#welcome-019e8c67:localhost:46167"}
$ POST /join/api {"code":"nope-nope-nope"}  # unknown
{"error": "invalid_code"} [http 403]
$ POST /join/api {"code":"dev-welcome-dead"}   # 0 uses
{"error": "code_exhausted"} [http 403]
$ POST /_matrix/client/v3/join/#welcome-019e8c67:localhost:46167  (as fresh user @welk_1788024311 — the Element Join step over HTTP)
{"room_id":"!YXNS65ggM9jaDmdvQh:localhost:46167"}

$ GET /sync (user) → invites + welcome-room timeline
invites: ["!WjYrN_R3naLqs--314mmRpDp9m3I4yhDFyxlKMcdk3M"]   # the space
room !YXNS65ggM9jaDmdvQh:localhost:46167 timeline:
  welcome — sit tight, you're about to be invited to the main space. join this room to get the invite.
  invite sent — accept it in Element and you're in.
```
