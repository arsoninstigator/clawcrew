# toolbox

things crittertown **calls**. someone else's daemon, over its own api, with your key: nothing
here is copied into the platform, and nothing here runs inside it.

the difference from `ingredients/` is ownership, not size. an ingredient is started by the
platform and its state is the platform's. a toolbox entry is your deployment:

```
toolbox/workflows/   the two workflow engines, and their clients
toolbox/firecrawl/   the worked example: a crawl tool an agent calls
```

adding one: a `status()` that reports whether it is configured, one function per call that
returns a plain dict, `ProviderFailure` on anything that is not 2xx, and no import of the
project's own code. if it needs a licence review, `crittertown/thirdparty.md` carries the row.
