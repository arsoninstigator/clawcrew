# get

one folder per way in. each one is the only place a command lives, so there is no
second copy to go stale.

| folder | what it gets you | status |
| --- | --- | --- |
| [`https`](https) | the server itself: uvicorn, docker compose, celery | works |
| [`api`](api) | the http api over curl, plus the openapi ui | works |
| [`sdk`](sdk) | the python package, embedded in your own process | works |
| [`npm`](npm) | a js client for the http api | not built |
| [`mcp`](mcp) | an mcp server so other agents can call a crew | not built |

two of the five are honest placeholders. they say what they will be, what exists
instead today, and the shape they will expose, because a folder that pretends is worse
than a folder that says "not yet".
